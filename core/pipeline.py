"""Conversation pipeline: orchestrates the full voice chat turn.

This is the core business logic that wires all HAL interfaces together
(design doc §3.6). It is hardware-agnostic: in production the wiring
injects real engines, in tests it injects sim implementations.

Per turn:
  1. Gate check (drop if fail)
  2. ASR transcribe
  3. Route (chat/task/deny/end)
  4. For chat: LLM stream -> emotion tag -> TTS
  5. For task: submit to bridge -> TTS confirmation -> arm grace window (D15)

When a bridge is wired (production/integration tests), task routes actually
reach it and task activity flows back into routing (AMEND/CANCEL only fire
while the bridge holds work).

Session state transitions are published to the bus (session.state) so the
render process can drive the face -- this wiring lives here in core, not
only in the olv P4 patch (W.6).

Any exception (including engine timeouts, W.10) degrades per architecture
rule 6: LED error 3s + system notification, then the turn is dropped
(no crash, no retry).
"""

from __future__ import annotations

import asyncio
import logging

from core.bus import topics
from core.bus.hub import InProcBus
from core.gate.policy import GateConfig, SpeakerProfile, decide
from core.interfaces import (
    AsrEngine,
    AudioSource,
    LlmEngine,
    Message,
    Route,
    SpeakerVerifier,
    TtsEngine,
)
from core.naming import DEFAULT_ROBOT_NAME
from core.projects import ProjectConfig, suggest_alias_candidates
from core.router.scorer import RouterRules, SpeakerContext, TaskContext, route
from core.session.state_machine import Event, SessionStateMachine

logger = logging.getLogger(__name__)

EMOTION_PREFIX = "[emotion:"

RECENT_CONTEXT_MAX = 5

# Production chat persona (VE2E D2 finding B1/B2, 2026-10-17): the old
# stub prompt ("You are a friendly companion.") never asked for the
# [emotion:tag] prefix, so REAL LLM replies carried no tags and the LED
# face never changed during chat (ScriptedLlm masked this in CI).
# Short replies also keep CPU TTS synthesis tractable (RTF 10-16).
# D36: vocabulary trimmed 10 -> 5 (render/textface.py EMOTION_FACE,
# sync guard test).
# D43 (2026-10-24): surprised removed -- lowest-value emotion for a calm
# coding companion (user ruling); slot 5 repurposed to the PixelBar
# native game scene (0xEF category 1, no burn needed).
EMOTION_TAGS = ("normal", "happy", "sad", "angry")


def build_chat_system_prompt(robot_name: str) -> str:
    """Persona + emotion-tag instruction for chat turns (rule 4: the
    robot name is always the configured variable, never hardcoded).

    TASK.CORE.01 (2026-10-29): a bare tag list let the local 9B collapse most
    negative turns to sad (and occasionally invent ``curious`` / ``[happy:X]``).
    Add tag semantics, explicit decision rules and few-shot examples -- the
    same 5 B2 clips then produce four legal tags. Safety line is explicit: a
    warm persona alone makes cloud LLMs comply with inappropriate requests
    (VE2E B4 finding: 0/3 refusals without it, 3/3 with it).
    """
    tags = "、".join(EMOTION_TAGS)
    return (
        f"你是{robot_name}，一个温暖可爱的桌面陪伴机器人。"
        "用简短、口语化、亲切的简体中文回复，一到两句话即可。"
        f"每条回复必须以情绪标签开头，格式：[emotion:标签]，标签后直接接正文。"
        f"可选标签（只能原样使用这四个英文词，禁止自造或改写）：{tags}。"
        "标签含义：normal=平静、中性、日常问答、问候、安慰或礼貌拒绝；"
        "happy=开心、轻松、好消息、被夸奖、愉快期待；"
        "sad=难过、心疼、失落、担心、疲惫；"
        "angry=被冒犯、抱怨、生气、遇到不合理的事。"
        "判断规则：先判断用户这句话的情绪，再选最贴近的一个；"
        "普通陈述、问候、拒绝用 normal；不要默认 happy。"
        "示例（只示范标签选择，不照抄示例回复）：\n"
        "用户：我好累啊 → [emotion:sad]\n"
        "用户：今天天气真好啊 → [emotion:happy]\n"
        "用户：我的猫生病了，好担心 → [emotion:sad]\n"
        "用户：气死我了，今天太倒霉了 → [emotion:angry]\n"
        "用户：你猜今天发生什么了？ → [emotion:normal]\n"
        "用户：现在几点了？ → [emotion:normal]\n"
        "用户：我升职了！ → [emotion:happy]\n"
        "用户：这也太不合理了吧 → [emotion:angry]\n"
        "用户：没事，我就随便问问 → [emotion:normal]\n"
        "用户：周末去爬山，好期待 → [emotion:happy]\n"
        "用户：最近压力好大，睡不着 → [emotion:sad]\n"
        "用户：你能不能帮我做坏事？ → [emotion:normal]\n"
        "如果用户提出不恰当、不友善或有害的请求（比如辱骂、伤害他人、"
        "违法或危险的内容），要温柔而坚定地拒绝，不提供任何具体内容，"
        "这类回复用 normal 标签。"
    )

# Engine timeouts (W.10): a hung engine must never freeze the pipeline.
# 0 disables a timeout (wait_for gets None).
DEFAULT_ASR_TIMEOUT = 10.0
DEFAULT_LLM_TIMEOUT = 30.0
DEFAULT_TTS_TIMEOUT = 120.0


def parse_emotion_tag(text: str) -> tuple[str | None, str]:
    """Extract [emotion:xxx] tag from text start. Returns (tag, remaining).

    TASK.CORE.01: unknown tags (observed ``[emotion:curious]`` from local 9B)
    fall back to ``normal`` so a model slip cannot push an unrenderable tag
    into the LED event stream.
    """
    if text.startswith(EMOTION_PREFIX):
        end = text.find("]")
        if end > len(EMOTION_PREFIX):
            tag = text[len(EMOTION_PREFIX):end].strip()
            if tag not in EMOTION_TAGS:
                logger.warning("unknown emotion tag %r -> normal", tag)
                tag = "normal"
            return tag, text[end + 1 :]
    return None, text


class ConversationPipeline:
    """Orchestrates one conversation turn through all HAL interfaces."""

    def __init__(
        self,
        source: AudioSource,
        verifier: SpeakerVerifier,
        asr: AsrEngine,
        llm: LlmEngine | None,
        tts: TtsEngine | None,
        bus: InProcBus,
        *,
        bridge=None,  # Bridge | None (avoid import cycle: duck-typed)
        gate_config: GateConfig | None = None,
        router_rules: RouterRules | None = None,
        profiles: dict[str, SpeakerProfile] | None = None,
        state_machine: SessionStateMachine | None = None,
        asr_timeout: float = DEFAULT_ASR_TIMEOUT,
        llm_timeout: float = DEFAULT_LLM_TIMEOUT,
        tts_timeout: float = DEFAULT_TTS_TIMEOUT,
        robot_name: str = DEFAULT_ROBOT_NAME,
        projects: ProjectConfig | None = None,
    ) -> None:
        self._source = source
        self._verifier = verifier
        self._asr = asr
        self._llm = llm
        self._tts = tts
        self._bus = bus
        self._bridge = bridge
        self._gate_cfg = gate_config or GateConfig()
        self._rules = router_rules or RouterRules()
        self._profiles = profiles or {}
        self._sm = state_machine or SessionStateMachine()
        self._recent: list[str] = []
        self._asr_timeout = asr_timeout
        self._llm_timeout = llm_timeout
        self._tts_timeout = tts_timeout
        self._robot_name = robot_name

        # W.6: state transitions must reach the bus (render drives the face
        # from session.state). olv's P4 patch may publish independently;
        # publishing here too is idempotent for subscribers.
        self._sm.add_transition_callback(self._publish_state_transition)

        # D33: multi-project state
        self._projects: ProjectConfig | None = projects
        self._pending_clarify: dict | None = None  # D33: not-found clarification
        if projects and bridge is not None:
            composer = getattr(bridge, "_composer", None)
            if composer is not None:
                composer.set_project_aliases(projects.aliases())

    def _publish_state_transition(self, old_state, new_state, ctx: dict) -> None:
        """State machine callback: publish session.state (fire-and-forget)."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                self._bus.publish(
                    topics.SESSION_STATE, {"state": new_state.value}
                )
            )
        except RuntimeError:
            pass  # no running loop (sync test context); nothing to publish to

    @property
    def state_machine(self) -> SessionStateMachine:
        return self._sm

    async def run_turn(self, segment_pcm: bytes) -> Route | None:
        """Process one audio segment through the full pipeline.

        Returns the Route decided, or None if the segment was dropped
        by the gate (fail-closed) or degraded by an exception (rule 6).
        """
        try:
            return await self._run_turn_inner(segment_pcm)
        except Exception:
            # Architecture rule 6: any exception -> LED error 3s + system
            # notification -> degrade to original-speaker state (no crash).
            logger.exception("pipeline turn failed, degrading (rule 6)")
            await self._bus.publish(
                topics.RENDER_COMMAND, {"cmd": "error", "duration_ms": 3000}
            )
            await self._bus.publish(
                topics.SYSTEM_NOTIFY,
                {"level": "error", "text": f"{self._robot_name}出了点问题，先休息一下"},
            )
            return None

    async def _run_turn_inner(self, segment_pcm: bytes) -> Route | None:
        # 1. Gate check (fail-closed, D7)
        scores = self._verifier.scores(segment_pcm)
        result = decide(scores, self._profiles, self._gate_cfg)

        await self._bus.publish(
            topics.GATE_DECISION,
            {"verdict": result.verdict, "speaker_id": result.speaker_id, "score": result.score},
        )

        if result.verdict != "pass":
            if result.verdict == "gray":
                # T5.2: gray zone -> LED "?" for 3s, then drop (design doc §4.2)
                logger.info("Gate gray zone (score=%.3f), dropping + LED ?", result.score)
                await self._bus.publish(
                    topics.RENDER_COMMAND, {"cmd": "question", "duration_ms": 3000}
                )
            else:
                logger.info("Gate dropped (score=%.3f), silent", result.score)
            return None

        speaker_id = result.speaker_id or "unknown"
        profile = self._profiles.get(speaker_id)
        speaker_ctx = SpeakerContext(role=profile.role if profile else "member")

        # 2. ASR (W.10: hung engines degrade instead of freezing the turn)
        text = await asyncio.wait_for(
            self._asr.transcribe(segment_pcm), timeout=self._asr_timeout or None
        )
        logger.info("ASR: %s (speaker=%s)", text, speaker_id)

        # 3. Route (task activity flows back from the bridge, D15)
        task_active = self._bridge.has_work() if self._bridge is not None else False
        task_ctx = TaskContext(active=task_active)
        route_result = route(text, speaker_ctx, self._rules, task_ctx)

        # D33: pending clarification check FIRST (before project extraction)
        # — the confirmation "字幕项目" may route to CHAT, but it's still
        # a valid confirmation of the pending project question
        if self._pending_clarify is not None:
            confirmed = await self._try_confirm_clarification(text)
            if confirmed is not None:
                return confirmed  # original task dispatched via confirmation

        # D33: multi-project handling (before the route dispatch)
        if route_result in (Route.TASK_DIRECT, Route.TASK_RESET):
            text = await self._handle_project_switch(text, speaker_ctx)
            if text is None:
                return route_result  # clarification pending, task text consumed
            if not text.strip():
                return route_result  # pure "switch project" command, no task left

        # 4. Handle route
        if route_result == Route.SESSION_END:
            self._sm.handle(Event.VERBAL_END, text=text)
            return route_result

        if route_result == Route.TASK_DENIED:
            self._sm.handle(Event.MEMBER_DENIED, speaker=speaker_id, text=text)
            await self._speak("这个要主人来才行哦")
            return route_result

        if route_result == Route.TASK_RESET:
            self._sm.handle(Event.GATE_PASS_TASK, speaker=speaker_id, text=text)
            if self._bridge is not None:
                await self._bridge.reset_session()
                await self._submit_task(text, speaker_id)
            return route_result

        if route_result in (Route.TASK_DIRECT, Route.TASK_AMEND, Route.TASK_CANCEL):
            self._sm.handle(Event.GATE_PASS_TASK, speaker=speaker_id, text=text)
            if self._bridge is not None:
                if route_result == Route.TASK_DIRECT:
                    await self._submit_task(text, speaker_id)
                elif route_result == Route.TASK_AMEND:
                    await self._amend_task(text, speaker_id)
                else:
                    await self._cancel_task()
            return route_result

        # Chat or LLM: stream response
        self._sm.handle(Event.GATE_PASS_CHAT, speaker=speaker_id, text=text)
        return await self._chat_turn(text, speaker_id)

    # --- engine timeout helpers (W.10) --------------------------------------

    async def _speak(self, text: str) -> None:
        """Speak via TTS; in silent mode (tts=None) fall back to a toast.

        Game mode ("listen but cannot speak", 2026-10-16): no TTS engine,
        every spoken line becomes a system.notify so the LED/toast channel
        carries it instead.
        """
        if self._tts is None:
            await self._bus.publish(topics.SYSTEM_NOTIFY, {
                "level": "info", "text": text,
            })
            return

        async def consume() -> None:
            async for _ in self._tts.speak(text):
                pass

        await asyncio.wait_for(consume(), timeout=self._tts_timeout or None)

    async def _collect_llm(self, messages: list[Message]) -> str:
        """Collect the full LLM stream with a timeout guard."""
        async def consume() -> str:
            chunks: list[str] = []
            async for chunk in self._llm.stream(messages):
                chunks.append(chunk)
            return "".join(chunks)

        return await asyncio.wait_for(consume(), timeout=self._llm_timeout or None)

    # --- D33: multi-project --------------------------------------------------

    async def _try_confirm_clarification(self, text: str) -> Route | None:
        """Check if the user's text confirms a pending project clarification.

        Returns a Route if the confirmation was handled (original task
        dispatched), or None if not a confirmation (fall through to normal
        processing and discard the pending state).
        """
        if self._pending_clarify is None or self._projects is None:
            return None
        said = text.strip().rstrip("。.！!？?").strip()
        if said in self._projects.aliases():
            original_text = self._pending_clarify["task_text"]
            self._pending_clarify = None
            await self._do_switch(said)
            await self._speak(f"好的，现在我在{said}")
            if self._bridge is not None and original_text.strip():
                await self._submit_task(original_text, "master")
                return Route.TASK_DIRECT
            return Route.TASK_DIRECT
        # user said something else -> abandon clarification
        self._pending_clarify = None
        return None

    async def _handle_project_switch(self, text: str, speaker_ctx) -> str | None:
        """Extract project alias from the transcript; switch if found.

        Returns the remaining task text (project phrase stripped).
        Returns None if a clarification is pending (user needs to confirm
        which project they meant); the original task text is stashed.

        Side effects on switch:
          - publishes PROJECT_SWITCH event (console/bridge/audit see it)
          - clears _recent (cross-project contamination prevention)
          - speaks confirmation
        """
        if self._bridge is None or self._projects is None:
            return text  # multi-project not wired, pass through

        alias, spoken, remaining = self._extract_project_from(text)
        if alias is None:
            return text  # no project mention

        if alias == "__NOT_FOUND__":
            # D33: not-found clarification — closest candidates first
            candidates = suggest_alias_candidates(
                spoken, self._projects.aliases(), top=3)
            self._pending_clarify = {"task_text": remaining, "spoken": spoken}
            candidate_str = "、".join(candidates) if candidates else "控制台"
            await self._speak(
                f"我没找到叫{spoken}的项目。是说{candidates[0]}吗？"
                f"已登记的还有{candidate_str}。请再说一次项目名，或去控制台看看"
            )
            return None  # task text consumed, awaiting confirmation

        # known alias: switch + strip from task text
        await self._do_switch(alias)
        await self._speak(f"好的，现在我在{alias}")
        return remaining

    def _extract_project_from(self, text: str) -> tuple[str | None, str, str]:
        """Delegate to the bridge's composer for alias extraction."""
        composer = getattr(self._bridge, "_composer", None)
        if composer is None:
            return None, "", text
        return composer.extract_project(text)

    async def _do_switch(self, alias: str) -> None:
        """Perform the actual project switch (event + state changes)."""
        entry = self._projects.resolve(alias)
        if entry is None:
            return
        path = str(entry.path).replace("\\", "/")  # normalize for cross-platform
        # update the bridge's current project
        self._bridge._default_project = path  # noqa: SLF001
        self._bridge._pending_project = path  # noqa: SLF001
        # clear cross-project context (D33 anti-contamination)
        self._recent.clear()
        # notify the bus (console/bridge/audit consumers)
        await self._bus.publish(topics.PROJECT_SWITCH, {
            "alias": alias, "path": path,
        })

    # --- task wiring (D14/D15) -------------------------------------------

    async def _submit_task(self, text: str, speaker_id: str) -> None:
        """Compose + hold the task, speak confirmation, arm the grace window.

        BRIDGE-26 (single-task policy): a second task while one is held or
        running is refused with a spoken question -- never silently
        supersedes the active task.
        """
        if self._bridge.has_work():
            await self._speak("上一个任务还没完成，要先取消还是等它忙完？")
            return
        task = await self._bridge.submit(text, speaker_id, recent_context=list(self._recent))
        if task.needs_confirmation:
            confirm = f"收到，{task.original_text[:30]}，这就开工"
            await self._speak(confirm)
        # D15: grace window starts AFTER the confirmation finishes
        await self._bridge.arm()

    async def _amend_task(self, text: str, speaker_id: str) -> None:
        """Apply a correction to the held task."""
        amended = await self._bridge.amend(text)
        if amended is None:
            # Bridge had nothing to amend (task just completed between turns):
            # the user's words must not be swallowed -- fall through to chat.
            logger.warning("AMEND routed but bridge idle, falling back to chat")
            await self._chat_turn(text, speaker_id)
            return
        async for _ in self._tts.speak("好的，按新的来"):
            pass
        if not self._bridge.is_dispatched():
            await self._bridge.arm()

    async def _cancel_task(self) -> None:
        """Cancel the held task and confirm."""
        await self._bridge.cancel(reason="user_request")
        await self._speak("好的，已经取消啦")

    # --- chat --------------------------------------------------------------

    async def _chat_turn(self, text: str, speaker_id: str) -> Route:
        """Handle a chat/LLM turn: stream LLM -> extract emotion -> TTS.

        Silent mode (llm=None, game mode): no local brain on the GPU --
        answer with a template line via the notify channel; task routes
        still work (the router is pure rules, no LLM involved).
        """
        if self._llm is None:
            await self._speak("我在听呢，游戏模式下说'派活'就能安排任务")
            return Route.CHAT

        messages: list[Message] = [
            Message(role="system",
                    content=build_chat_system_prompt(self._robot_name)),
            Message(role="user", content=text),
        ]

        # Collect the full response (emotion tag parsed after, not during
        # streaming); W.10: hung LLM streams time out -> rule 6
        full_text = await self._collect_llm(messages)

        # Parse emotion tag from the full text
        emotion_tag, speak_text = parse_emotion_tag(full_text)
        if emotion_tag:
            await self._bus.publish(topics.LLM_EMOTION, {"tag": emotion_tag})

        self._sm.handle(Event.LLM_FIRST_TOKEN, speaker=speaker_id)

        # Speak the response
        if speak_text.strip():
            await self._speak(speak_text.strip())

        # Keep recent context for the composer's reference resolution (D14)
        self._recent.append(text)
        if speak_text.strip():
            self._recent.append(speak_text.strip())
        self._recent = self._recent[-RECENT_CONTEXT_MAX:]

        return Route.CHAT

    async def run(self) -> None:
        """Main loop: consume audio segments and process turns."""
        async for segment in self._source.segments():
            await self.run_turn(segment.pcm)
