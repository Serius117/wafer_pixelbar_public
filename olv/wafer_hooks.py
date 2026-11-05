"""wafer-hooks: the OLV-side entry points for wafer-core (TASK.OLVCORE.01).

Loaded by the P1/P2 fork patches. Owns the wafer-core lifecycle inside
the OLV process:

  P1 gate (01a): CAM++ voiceprint gate, pre-ASR
  P2 agent (01b/c): WaferAgent -- routing + chat LLM + bridge tasks
  P4 states (01d): session.state events to hub

Design notes:
  - Lazy singletons; nothing heavy at import time
  - If config/model missing, gate degrades to pass-through
  - The WaferAgent is registered by the P2 patch in the OLV agent factory
  - Bridge tasks go over the hub via TASK_REQUEST (D28, same as RemoteExecutor)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logger: Any = logging.getLogger(__name__)

# D48: dual wake word -- the work wake word (chat wake keeps D31 defaults)
WORK_WAKE_KEYWORD = "小饼干活"


class _PctLogger:
    """loguru adapter accepting %-style formatting (all call sites use it).

    loguru only formats ``{}`` templates; passing args to a ``%s`` message
    silently prints the literal ``%s`` (observed on gate/wake logs, which made
    R.20b blind). This shim pre-formats with ``%`` when args are given.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @staticmethod
    def _fmt(msg: str, args: tuple) -> str:
        if not args:
            return msg
        try:
            return msg % args
        except Exception:  # noqa: BLE001 -- never break logging
            return msg

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._inner.debug(self._fmt(msg, args), **kwargs)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._inner.info(self._fmt(msg, args), **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._inner.warning(self._fmt(msg, args), **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._inner.error(self._fmt(msg, args), **kwargs)

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._inner.exception(self._fmt(msg, args), **kwargs)


# Inside OLV the app uses loguru (so our lines land in logs/olv.log); plain
# pytest runs fall back to stdlib logging.
try:  # noqa: SIM105 -- explicit fallback is intentional
    from loguru import logger as _loguru_logger

    logger = _PctLogger(_loguru_logger)
except ImportError:  # pragma: no cover -- main venv tests use stdlib logging
    pass

HUB_HOST = "127.0.0.1"
HUB_PORT = 8765
GRACE_SECONDS = 5.0
RECENT_MAX = 5

# ---------------------------------------------------------------------------
# P1: Voiceprint gate (01a)
# ---------------------------------------------------------------------------

from core.bus import topics  # noqa: E402
from core.bus.hub_client import HubClient  # noqa: E402
from core.gate.policy import (  # noqa: E402
    decide,  # noqa: E402
)
from core.gate.profiles import GateSetup, load_gate_setup  # noqa: E402
from engines.voiceprint import EnrollmentStore, SherpaCampplusVerifier  # noqa: E402


def _speakers_config_path() -> Path:
    """Gate config path, overridable for hermetic test runs (TASK.RELEASE.03).

    Production uses config/speakers.yaml (real home enrollment). verify.py
    sets WAFER_SPEAKERS_CONFIG to a corpus-derived store so smoke/OLVCORE do
    not depend on whoever enrolled on this machine.
    """
    raw = os.environ.get("WAFER_SPEAKERS_CONFIG")
    if not raw:
        return _ROOT / "config" / "speakers.yaml"
    path = Path(raw)
    return path if path.is_absolute() else _ROOT / path


class _GateState:
    """Lazy singleton for the gate chain + hub client."""

    def __init__(self) -> None:
        self.ready = False
        self.failed = False
        self.setup: GateSetup | None = None
        self.verifier: SherpaCampplusVerifier | None = None
        self.hub: HubClient | None = None
        self._hub_connected = False

    async def _ensure(self) -> bool:
        if self.ready or self.failed:
            return self.ready
        try:
            if self.setup is None:
                self.setup = load_gate_setup(_speakers_config_path())
                if not self.setup.voiceprint_model:
                    raise FileNotFoundError("voiceprint.model not set")
                model_path = _ROOT / self.setup.voiceprint_model
                if not model_path.exists():
                    raise FileNotFoundError(
                        f"CAM++ model not found: {model_path}")
                store = EnrollmentStore(_ROOT / self.setup.embedding_dir)
                self.verifier = SherpaCampplusVerifier(store, model_path)
            model_path = _ROOT / str(self.setup.voiceprint_model)
            self.hub = HubClient(HUB_HOST, HUB_PORT)
            try:
                await self.hub.connect()
                self._hub_connected = True
            except Exception as hub_exc:  # noqa: BLE001
                logger.warning("hub connect failed (events won't publish): %s",
                               hub_exc)
            self.ready = True
            logger.info("wafer gate ready (profiles=%d, model=%s, hub=%s)",
                        len(self.setup.profiles), model_path.name,
                        self._hub_connected)
        except Exception as exc:  # noqa: BLE001
            self.failed = True
            logger.warning("wafer gate degraded (pass-through): %s", exc)
        return self.ready


_state = _GateState()
# P1→P2 handoff: current speaker for this conversation turn
_current_speaker: dict = {"id": None, "role": None}


async def _publish(topic: str, payload: dict) -> None:
    if _state.hub is None:
        return
    try:
        await _state.hub.publish(topic, payload)
    except Exception:  # noqa: BLE001
        logger.warning("hub publish %s failed", topic)


async def gate_check(audio: Any) -> str | None:
    """P1 voiceprint gate: called by OLV before ASR.

    OLVCORE.02: standby KWS mode checks the wake word before ASR. In
    v0.1 ASR wake mode the segment goes through the gate and ASR; the
    transcript is matched in WaferAgent.chat().
    """
    # P5 wake word check (standby → active)
    if _session.is_standby():
        if _wake_mode() == "kws":
            woke = await wake_check(audio)
            if not woke:
                samples = getattr(audio, "size", 0)
                logger.info("standby: no wake word, dropping segment "
                            f"({samples} samples)")
                _dump_standby(audio)
                return None  # standby: silent, no ASR
            # wake word hit (or KWS degraded) → fall through to gate
        else:
            logger.info("asr-wake: standby segment → gate → ASR")

    if not await _state._ensure():
        _current_speaker.update(id="unknown", role="member")
        return "unknown"

    import numpy as np

    if not isinstance(audio, np.ndarray) or audio.size == 0:
        return None

    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
    scores = _state.verifier.scores(pcm)
    result = decide(scores, _state.setup.profiles, _state.setup.config)
    logger.info(f"gate: verdict={result.verdict} "
                f"speaker={result.speaker_id} "
                f"score={result.score:.3f} samples={audio.size}")
    await _publish(topics.GATE_DECISION, {
        "verdict": result.verdict,
        "speaker_id": result.speaker_id,
        "score": round(result.score, 4),
    })

    if result.verdict == "pass":
        role = get_speaker_role(result.speaker_id)
        _current_speaker.update(id=result.speaker_id, role=role)
        _session.keep_alive()  # OLVCORE.02: active speech resets timeout
        return result.speaker_id
    # R.21/D46: a drop/gray must not leave the UI stuck at "thinking" -- the
    # chain returns early and would otherwise never publish another state.
    await _publish(topics.SESSION_STATE, {"state": "listening"})
    return None


def get_speaker_role(speaker_id: str | None) -> str:
    if _state.setup is None or speaker_id is None:
        return "member"
    profile = _state.setup.profiles.get(speaker_id)
    return profile.role if profile else "member"


async def get_hub() -> HubClient | None:
    await _state._ensure()
    return _state.hub if _state.ready else None


# ---------------------------------------------------------------------------
# P2: Routing + WaferAgent (01b/c)
# ---------------------------------------------------------------------------

from core.interfaces import Route  # noqa: E402
from core.naming import DEFAULT_ROBOT_NAME  # noqa: E402
from core.pipeline import (  # noqa: E402
    build_chat_system_prompt,
    parse_emotion_tag,  # noqa: E402
)

# Multi-project (D33)
from core.projects import ProjectConfig, suggest_alias_candidates  # noqa: E402
from core.router.scorer import (  # noqa: E402
    RouterRules,
    SpeakerContext,
    TaskContext,
)
from core.router.scorer import route as route_text  # noqa: E402

_RULES = RouterRules()  # includes D35 "查"

# D51/TASK.SERVE.02: voice approve/deny phrases (short answers only --
# matched against the WHOLE utterance so "批准" fires but "批准了吗" chats)
_APPROVE_CUES: frozenset[str] = frozenset(
    {"批准", "同意", "可以", "通过", "准了", "允许", "行", "好"})
_DENY_CUES: frozenset[str] = frozenset(
    {"拒绝", "不行", "别改", "不要", "不准", "驳回", "算了"})

# TASK.SERVE.04: LLM semantic approval (natural language). The LLM only
# CLASSIFIES the utterance; the decision itself stays deterministic
# (TASK_APPROVE -> serve reply) -- never give the model the button.
_APPROVAL_PROMPT = (
    "你是意图分类器。家里的编程助手请求执行一个操作，主人正在口头答复。\n"
    "操作内容：{question}\n"
    "主人说的话：{utterance}\n\n"
    "判断主人是同意还是拒绝。只输出 [approve]（同意）、[deny]（拒绝）、"
    "[unclear]（疑问/跑题/犹豫未决，无法确定）三选一，不要输出其他内容。")
_ANNOUNCE_PROMPT = (
    "你是{robot_name}，陪伴家人的语音管家。家里的编程助手想执行一个操作，"
    "请用一两句口语讲给主人听：说清楚要动什么、大概怎么改，最后问主人"
    "同不同意。不要编造细节。操作内容：{detail}")
_LLM_TIMEOUT_S = 12.0


def _route(text: str, role: str, task_active: bool) -> Route:
    speaker = SpeakerContext(role=role)
    task_ctx = TaskContext(active=task_active)
    r = route_text(text, speaker, _RULES, task_ctx)
    # Map scorer Route to the same values (they share the StrEnum)
    return Route(str(r))


def split_sentences(text: str) -> list[str]:
    """Split Chinese text into sentences for TTS."""
    import re
    parts = re.split(r"([。！？!?\n]+)", text)
    sentences = []
    for i in range(0, len(parts) - 1, 2):
        s = parts[i] + parts[i + 1]
        if s.strip():
            sentences.append(s.strip())
    if len(parts) % 2 == 1 and parts[-1].strip():
        sentences.append(parts[-1].strip())
    return sentences if sentences else ([text.strip()] if text.strip() else [])


class _BridgeClient:
    """Lightweight bridge over the hub: submit/amend/cancel + grace window."""

    def __init__(self, hub: HubClient) -> None:
        self._hub = hub
        self._active = False  # has held/running task
        self._current_task_id: str | None = None
        self._pending_task_text: str | None = None
        self._flush_task: asyncio.Task | None = None
        self._projects: ProjectConfig | None = None
        self._default_project = "./workspace"
        self._current_project = self._default_project
        self._recent: list[str] = []

    def _ensure_projects(self) -> None:
        if self._projects is None:
            try:
                from core.projects import load_projects

                self._projects = load_projects(
                    _ROOT / "config" / "projects.yaml")
                # Use the configured default (the sandbox path used to be
                # hardcoded, so a registered default was ignored and every
                # task targeted ./workspace -- R.20c finding).
                default_entry = self._projects.resolve(None)
                if default_entry is not None:
                    self._default_project = str(default_entry.path)
                    self._current_project = self._default_project
                    logger.info("projects default -> %s (%s)",
                                default_entry.alias, self._default_project)
            except Exception as exc:  # noqa: BLE001
                logger.warning("projects.yaml not loaded: %s", exc)

    def has_work(self) -> bool:
        return self._active

    def finish(self, task_id: str) -> None:
        """Clear the single active task only when its result arrives."""
        if task_id and self._current_task_id == task_id:
            self._active = False
            self._current_task_id = None

    def _extract_project(self, text: str) -> tuple[str | None, str, str]:
        """Extract project alias from text.

        Returns (alias, spoken, remaining). alias=None + spoken="" means
        no project mention. alias="__NOT_FOUND__" + spoken=X means the
        user said "在X项目" but X isn't registered (D33 clarify path).
        """
        import re

        self._ensure_projects()
        if self._projects is None:
            return None, "", text
        # exact match: handle aliases that already contain "项目"
        for alias in self._projects.aliases():
            suffix = "" if alias.endswith("项目") else "项目"
            for prefix in ("在", "去", "回", "切到"):
                pattern = f"{prefix}{alias}{suffix}"
                idx = text.find(pattern)
                if idx >= 0:
                    remaining = (text[:idx] + text[idx + len(pattern):]).strip()
                    return alias, alias, remaining
                # also try without suffix (e.g. "在音乐项目里" not "在音乐项目项目里")
                if suffix:
                    bare = f"{prefix}{alias}"
                    idx = text.find(bare)
                    if idx >= 0:
                        remaining = (text[:idx] + text[idx + len(bare):]).strip()
                        # strip common trailing particles
                        remaining = re.sub(r"^[里上中]", "", remaining).strip()
                        return alias, alias, remaining
        # fuzzy: "在X项目" where X is 2-6 chars but not a known alias (D33)
        m = re.search(r"[在去回切到]{1,2}(.{2,6}?)项目", text)
        if m:
            spoken = m.group(1)
            remaining = text[:m.start()] + text[m.end():]
            return "__NOT_FOUND__", spoken, remaining.strip()
        return None, "", text

    async def _do_switch(self, alias: str) -> str:
        """Switch project and return the path."""
        self._ensure_projects()
        if self._projects is None:
            return self._default_project
        entry = self._projects.resolve(alias)
        if entry is None:
            return self._default_project
        path = str(entry.path).replace("\\", "/")
        self._current_project = path
        # Clear cross-project context (D33 anti-contamination)
        self._recent.clear()
        await _publish(topics.PROJECT_SWITCH,
                       {"alias": alias, "path": path})
        return path

    async def submit(self, text: str, speaker_id: str) -> str:
        """Compose and hold exactly one task at a time."""
        if self._active:
            return "上一个任务还在跑，请先等它完成或说取消"
        task_id = f"t-wafer-{uuid.uuid4().hex[:12]}"
        await _publish(topics.TASK_REQUEST, {
            "op": "execute",
            "task": {"prompt": text, "original_text": text,
                     "speaker_id": speaker_id, "task_id": task_id},
            "project": self._current_project,
        })
        self._active = True
        self._current_task_id = task_id
        return f"收到，{text[:30]}，这就开工"

    async def amend(self, text: str) -> str | None:
        """Amend the active task."""
        if not self._active:
            return None
        await _publish(topics.TASK_REQUEST, {
            "op": "execute",
            "task": {"prompt": text, "original_text": text,
                     "speaker_id": _current_speaker.get("id", "master"),
                     "task_id": f"t-wafer-amend-{id(self)}"},
            "project": self._current_project,
        })
        return "好的，按新的来"

    async def cancel(self) -> str:
        """Cancel the active task."""
        task_id = self._current_task_id
        self._active = False
        self._current_task_id = None
        await _publish(topics.TASK_CANCEL, {
            "task_id": task_id, "reason": "user_request"})
        return "好的，已经取消啦"


# ---------------------------------------------------------------------------
# WaferAgent: the P2 OLV agent (01b + 01c)
# ---------------------------------------------------------------------------

# TASK.RELEASE.08: OLV websocket layer registers a sender after connecting.
# The sender is deliberately synchronous: it only enqueues the announcement,
# so the hub callback never blocks on TTS or a user turn.
_announce_sender: Any = None


def set_announce_sender(sender: Any) -> None:
    global _announce_sender
    _announce_sender = sender


def clean_result_summary(summary: Any, limit: int = 120) -> str:
    """Make a task result safe for speech: no Markdown/table noise."""
    import re

    text = str(summary or "")
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"[*_`#>|]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip("，,；;:：") + "…"
    return text


class WaferAgent:
    """P2: wafer's agent for OLV -- routing + chat LLM + bridge tasks.

    Registered by the P2 patch in the OLV agent factory as "wafer_agent".
    Not an AgentInterface subclass (we avoid importing OLV ABCs in our
    module); the P2 patch wraps it in a thin adapter.
    """

    def __init__(self, llm: Any, robot_name: str = DEFAULT_ROBOT_NAME) -> None:
        self._llm = llm  # OLV's StatelessLLMInterface
        self._robot_name = robot_name
        self._bridge: _BridgeClient | None = None
        self._pending_clarify: dict | None = None
        # D51: pending permission.asked (set by the notification pump)
        self._pending_permission: dict | None = None
        self._memory: list[dict] = []  # simple context (v1 no rolling summary)
        self._announce: list[str] = []  # fallback results waiting to be spoken
        self._announced_ids: set[str] = set()
        self._announced_permissions: set[str] = set()
        self._summary_inflight: set[str] = set()
        self._pump_started = False

    def _ensure_bridge(self) -> _BridgeClient:
        if self._bridge is None:
            hub = _state.hub
            if hub is None and not _state.failed:
                # _ensure is async; if not yet initialized, use FakeHub
                # (the gate_check will have initialized it by now in practice)
                hub = _state.hub if _state.ready else None
            self._bridge = _BridgeClient(hub) if hub else _BridgeClient(
                _FakeHub())
        return self._bridge

    def _ensure_pump(self) -> None:
        """D51/TASK.SERVE.02: subscribe to task.permission/result once.

        - permission.asked -> _pending_permission (approve/deny routing)
        - task.result -> _announce queue, spoken at the next chat turn
          (OLV has no proactive TTS push; wake-and-speak is OLVCORE.02)
        """
        if self._pump_started:
            return
        hub = _state.hub if _state.ready else None
        if hub is None:
            return
        self._pump_started = True

        async def _on_permission(topic: str, payload: dict) -> None:
            question = payload.get("question") or (
                f"想{payload.get('kind', '操作')} "
                f"{str(payload.get('target', ''))[-40:]}"
            )
            key = ":".join([
                str(payload.get("task_id") or ""),
                str(payload.get("kind") or ""),
                str(payload.get("target") or ""),
            ])
            self._pending_permission = {"question": question, "key": key}
            logger.info("pending permission: %s", question)
            self._announce_permission(key, question)

        async def _on_result(topic: str, payload: dict) -> None:
            logger.info("task.result received: %s", payload.get("task_id"))
            await self._handle_task_result_async(payload)

        hub.subscribe(topics.TASK_PERMISSION, _on_permission)
        hub.subscribe(topics.TASK_RESULT, _on_result)
        logger.info("notification pump on (task.permission/result)")

    def _announce_permission(self, key: str, question: str) -> None:
        """Speak a pending permission immediately, once per request."""
        if key in self._announced_permissions:
            return
        sender = _announce_sender
        if sender is None:
            return  # next-turn fallback in chat()
        text = (f"主人，opencode {question}。"
                "请说“小饼，批准”或“小饼，拒绝”。")
        try:
            sender(f"permission:{key}", text)
        except Exception as exc:  # noqa: BLE001 -- next-turn fallback
            logger.warning("proactive permission announce failed: %s", exc)
            return
        self._announced_permissions.add(key)
        self._announced_permissions = set(
            list(self._announced_permissions)[-50:])

    @staticmethod
    def _norm_result(text: str) -> str:
        return re.sub(r"[\s，。！？、,.!?：:；;“”\"'`]+", "", text or "")

    def _looks_like_echo(self, prompt: str, result: str) -> bool:
        """True when opencode returned the user prompt instead of an answer."""
        p = self._norm_result(prompt)
        r = self._norm_result(result)
        if not p or not r:
            return False
        return r == p or (p in r and len(r) <= max(len(p) + 8, 40))

    async def _summarize_result(self, prompt: str, raw: str,
                                ok: bool) -> str:
        """Use the 9B model to turn raw output into one spoken sentence."""
        raw = clean_result_summary(raw)
        if not raw:
            return "任务结束，但没有拿到有效结果"
        if self._looks_like_echo(prompt, raw):
            return "任务结束，但没有拿到有效结果"
        if self._llm is None:
            return raw
        instruction = (
            "你是小饼。根据用户任务和 opencode 的原始输出，用一句中文"
            "向主人汇报结论。不要复述用户问题，不要寒暄，不要输出"
            "Markdown，不超过 80 字。若原始输出没有有效信息，就说"
            "“任务结束，但没有拿到有效结果”。"
        )
        body = (f"用户任务：{prompt or '（未提供）'}\n"
                f"执行结果（ok={ok}）：{raw}")
        reply = await self._llm_complete([
            {"role": "system", "content": instruction},
            {"role": "user", "content": body},
        ])
        reply = clean_result_summary(reply.strip().strip("[]"))
        if (3 <= len(reply) <= 160
                and not self._looks_like_echo(prompt, reply)):
            return reply
        return raw

    async def _handle_task_result_async(self, payload: dict) -> None:
        """Deduplicate, summarize with 9B, then enqueue the announcement."""
        task_id = str(payload.get("task_id") or "")
        if task_id and (task_id in self._announced_ids
                        or task_id in self._summary_inflight):
            return
        if task_id:
            self._summary_inflight.add(task_id)
        try:
            raw = clean_result_summary(payload.get("summary"))
            summary = await self._summarize_result(
                str(payload.get("prompt") or ""), raw,
                payload.get("ok") is not False)
            self._handle_task_result({**payload, "summary": summary,
                                      "_summarized": True})
        finally:
            self._summary_inflight.discard(task_id)

    def _handle_task_result(self, payload: dict) -> None:
        """Clean/dedupe a result and hand it to the proactive sender."""
        task_id = str(payload.get("task_id") or "")
        if task_id and task_id in self._announced_ids:
            return
        if task_id:
            self._announced_ids.add(task_id)
            self._announced_ids = set(list(self._announced_ids)[-50:])
        bridge = self._ensure_bridge()
        finish = getattr(bridge, "finish", None)
        if finish is not None:
            finish(task_id)
        summary = clean_result_summary(payload.get("summary"))
        logger.info("proactive result task=%s summary=%r", task_id, summary)
        if not summary:
            return
        ok = payload.get("ok")
        if payload.get("_summarized"):
            text = summary if ok is not False else f"没弄成，{summary}"
        else:
            prefix = "搞定" if ok is not False else "没弄成"
            text = f"{prefix}，{summary}"
        sender = _announce_sender
        logger.info("proactive sender present=%s", sender is not None)
        if sender is not None:
            try:
                sender(task_id, text)
                return
            except Exception as exc:  # noqa: BLE001 -- fall back to next turn
                logger.warning("proactive announce enqueue failed: %s", exc)
        self._announce.append(text)
        # cap the fallback queue so a long soak cannot grow it forever
        self._announce = self._announce[-3:]

    # --- TASK.SERVE.04: LLM semantic approval helpers ------------------

    async def _llm_complete(self, messages: list[dict]) -> str:
        """Accumulate the full chat-LLM reply (non-streaming use)."""
        async def _collect() -> str:
            full = ""
            async for chunk in self._llm.chat_completion(messages):
                if isinstance(chunk, str):
                    full += chunk
            return full.strip()

        try:
            return await asyncio.wait_for(_collect(), timeout=_LLM_TIMEOUT_S)
        except TimeoutError:
            logger.warning("approval LLM timed out")
            return ""
        except Exception:  # noqa: BLE001 -- LLM down/absent: fail-safe ""
            logger.warning("approval LLM unavailable")
            return ""

    async def _do_approval(self, allow: bool) -> None:
        self._pending_permission = None
        await _publish(topics.TASK_APPROVE,
                       {"task_id": "current", "allow": allow})

    async def _classify_approval(self, said: str) -> str | None:
        """Two-tier L2: classify a natural-language answer via the LLM.

        Returns "approve" | "deny" | None (unclear or LLM unavailable).
        The model NEVER decides the action: we only read its tag and the
        deterministic TASK_APPROVE path executes.
        """
        question = (self._pending_permission or {}).get(
            "question", "一个代码操作")
        reply = await self._llm_complete([
            {"role": "user",
             "content": _APPROVAL_PROMPT.format(
                 question=question, utterance=said)},
        ])
        low = reply.lower()
        if "[approve]" in low and "[deny]" not in low:
            return "approve"
        if "[deny]" in low and "[approve]" not in low:
            return "deny"
        if "[unclear]" in low:
            return None
        # no valid tag at all: treat as unclear, never guess
        if reply:
            logger.warning("approval classifier returned no tag: %r",
                           reply[:80])
        return None

    async def _permission_question(self) -> str:
        """LLM-summarized spoken question; template fallback.

        Cached on the pending dict so repeated announce turns don't
        re-call the model.
        """
        pending = self._pending_permission or {}
        cached = pending.get("spoken")
        if cached:
            return cached
        template = pending.get("question", "有确认在等")
        reply = await self._llm_complete([
            {"role": "user",
             "content": _ANNOUNCE_PROMPT.format(
                 robot_name=self._robot_name, detail=template)},
        ])
        reply = reply.strip().strip("[]")
        if 3 < len(reply) <= 120 and "[" not in reply:
            pending["spoken"] = reply
            return reply
        if reply:
            logger.warning("announce LLM reply unusable: %r", reply[:80])
        return template

    async def chat(self, text: str, metadata: dict | None = None
                   ) -> AsyncIterator[tuple[str, str]]:
        """Main entry: route and handle. Yields (display_text, tts_text) pairs.

        The P2 patch adapter converts these to SentenceOutput for OLV.
        TASK.RELEASE.08: a proactive announcement is spoken verbatim and
        never enters routing/LLM.
        """
        speaker_id = _current_speaker.get("id", "unknown")
        role = _current_speaker.get("role", "member")
        self._ensure_pump()
        if metadata and metadata.get("wafer_announcement"):
            text = (text or "").strip()
            if text:
                yield (text, text)
            return
        # v0.1 ASR wake: standby turns reach the agent after gate + ASR.
        # Only a transcript containing the wake phrase opens the session;
        # non-wake chatter is silent and never calls the LLM/bridge.
        if _wake_mode() == "asr" and _session.is_standby():
            if not _has_asr_wake(text):
                logger.info("asr-wake: no wake phrase, silent drop (%r)",
                            (text or "")[:40])
                return
            _session.wake()
            logger.info("asr-wake: wake phrase matched (%r)",
                        (text or "")[:40])
            await _publish(topics.WAKE_WORD, {"keyword": "asr"})
        bridge = self._ensure_bridge()

        # --- TASK.KWS.03: two-level wake verification ---
        # A low-threshold KWS hit opened this turn; the transcript must
        # actually contain the wake word or we drop it SILENTLY (no reply,
        # no state change) -- that is the price of the lower threshold.
        if _session.wake_needs_verify:
            _session.wake_needs_verify = False
            if "小饼小饼" not in text and WORK_WAKE_KEYWORD not in text:
                logger.info("two-level wake: transcript lacks the wake "
                            "word, silently dropping (%r)", text[:40])
                return

        # --- D51: flush queued result announcements (task results arrive
        # asynchronously; OLV has no proactive TTS so we speak on the next
        # turn -- "怎么样了" or any utterance hears them first)
        if self._announce and role == "admin":
            for msg in self._announce:
                yield (msg, msg)
            self._announce = []

        # --- D51/TASK.SERVE.02+04: pending permission voice answer ---
        # While opencode waits on a permission.asked, approve/deny routes
        # to TASK_APPROVE instead of chat (pending_clarify pattern).
        # Two tiers: exact cue match (0ms), then LLM classification of
        # natural language; unclear/LLM-down falls back to announcing.
        if self._pending_permission is not None and role == "admin":
            # strip wake-word residues first: after standby re-entry the
            # transcript is "小饼小饼，批准" (D48: residue stays) -- the
            # exact-match below must see the bare cue
            said = text
            for residue in ("小饼小饼", WORK_WAKE_KEYWORD):
                said = said.replace(residue, "")
            said = said.strip("。.！!？?，, 、 \t")  # noqa: B005 (char set)
            if said in _APPROVE_CUES or said in _DENY_CUES:
                allow = said in _APPROVE_CUES
                await self._do_approval(allow)
                msg = "好的，批准了" if allow else "好的，已拒绝"
                yield (msg, msg)
                return
            if said:
                verdict = await self._classify_approval(said)
                if verdict == "approve":
                    await self._do_approval(True)
                    yield ("好的，批准了", "好的，批准了")
                    return
                if verdict == "deny":
                    await self._do_approval(False)
                    yield ("好的，已拒绝", "好的，已拒绝")
                    return
            # unclear / LLM down / empty: announce what's pending and stop.
            # Do NOT fall through to chat: that produced the observed
            # (previous bug: permission prompt followed by unrelated chat)
            question = await self._permission_question()
            announce = f"先说一句，{question}。批准还是拒绝？"
            yield (announce, announce)
            return
        announced_pending = False

        # --- D33: pending clarification check ---
        if self._pending_clarify is not None:
            said = text.strip().rstrip("。.！!？?").strip()
            self._ensure_bridge()._ensure_projects()
            aliases = (bridge._projects.aliases()
                       if bridge._projects else [])
            if said in aliases:
                original = self._pending_clarify["task_text"]
                self._pending_clarify = None
                await bridge._do_switch(said)
                yield (f"好的，现在我在{said}", f"好的，现在我在{said}")
                if original.strip():
                    confirm = await bridge.submit(original, speaker_id)
                    yield (confirm, confirm)
                return
            self._pending_clarify = None  # not a confirmation, abandon

        # --- D48: dual wake word -- admin work-wake forces the task route ---
        # The task semantics come from the TRANSCRIPT (robust); the KWS hit
        # keyword is observability only. "小饼小饼" keeps its residue in the
        # transcript (the LLM already copes with it).
        work_wake = False
        if role == "admin":
            if WORK_WAKE_KEYWORD in text:
                text = text.replace(WORK_WAKE_KEYWORD, "").strip(
                    "，。！？、 ,.!?\t")
                if not text:
                    # bare work-wake: acknowledge + arm one-shot pending_work
                    _session.pending_work = True
                    msg = "好，说吧"
                    yield (msg, msg)
                    return
            if _session.pending_work:
                _session.pending_work = False
                work_wake = True  # next utterance after bare work-wake

        # --- TASK.KWS.04: bare chat-wake -> fixed ack (two-stage UX) ---
        # A residue-only transcript ("小饼小饼") gets a scripted ack instead
        # of a full LLM turn: instant, zero tokens, and the user knows the
        # wake actually landed before speaking the real request.
        probe = text.replace("小饼小饼", "").replace(WORK_WAKE_KEYWORD, "")
        probe = probe.strip("，。！？、 ,.!?\t")
        if not probe and "小饼小饼" in text and not announced_pending:
            msg = "嗯？我在呢"
            yield (msg, msg)
            return

        # --- D33: project alias extraction (before routing) ---
        remaining = text
        if role == "admin":
            alias, spoken, remaining = bridge._extract_project(text)
            if alias == "__NOT_FOUND__" or (spoken and alias is None):
                # D33: not-found clarification
                candidates = suggest_alias_candidates(
                    spoken, bridge._projects.aliases()
                    if bridge._projects else [], top=3)
                candidate_str = "、".join(candidates) if candidates else "控制台"
                self._pending_clarify = {"task_text": remaining, "spoken": spoken}
                # TASK.SLOT.03: announce on the bus (LED substitute text)
                await _publish(topics.TASK_CLARIFY,
                               {"spoken": spoken, "candidates": candidates})
                msg = (f"我没找到叫{spoken}的项目。"
                       f"已登记的还有{candidate_str}。请再说一次项目名")
                yield (msg, msg)
                return
            if alias is not None:
                await bridge._do_switch(alias)
                switch_msg = f"好的，现在我在{alias}"
                yield (switch_msg, switch_msg)
                if not remaining.strip():
                    return  # pure "switch project" command

        # --- Route ---
        if work_wake:
            route = Route.TASK_DIRECT  # D48: forced, skip intent scoring
        else:
            route = _route(remaining, role, bridge.has_work())

        if route == Route.SESSION_END:
            yield ("晚安", "晚安")
            return

        if route == Route.TASK_DENIED:
            yield ("这个要主人来才行哦", "这个要主人来才行哦")
            return

        if route == Route.TASK_DIRECT:
            confirm = await bridge.submit(remaining, speaker_id)
            yield (confirm, confirm)
            return

        if route == Route.TASK_AMEND:
            result = await bridge.amend(remaining)
            if result:
                yield (result, result)
            else:
                # bridge idle, fall through to chat
                async for pair in self._chat_llm(remaining):
                    yield pair
            return

        if route == Route.TASK_CANCEL:
            result = await bridge.cancel()
            yield (result, result)
            return

        # CHAT or LLM gray band
        async for pair in self._chat_llm(remaining):
            yield pair

    async def _chat_llm(self, text: str) -> AsyncIterator[tuple[str, str]]:
        """Chat branch: persona prompt + LLM, then sentence split for TTS.

        TASK.RELEASE.06 (2026-10-30): collect the FULL reply before yielding.
        CosyVoice and the 9B LLM share one GPU; yielding sentences while the
        LLM was still generating made both contend (LLM 1.16 tok/s, TTS RTF
        5.0). Serializing removes the contention; the extra wait is only the
        remaining LLM tokens (~1s for a short reply).
        """
        system = build_chat_system_prompt(self._robot_name)
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": text}]

        full = ""
        try:
            async for chunk in self._llm.chat_completion(messages):
                if isinstance(chunk, str):
                    full += chunk
        except Exception as exc:  # noqa: BLE001
            logger.error("WaferAgent LLM error: %s", exc)
            fallback = f"{self._robot_name}出了点问题，我歇一下"
            yield (fallback, fallback)
            return

        emotion_tag, speak_text = parse_emotion_tag(full)
        if emotion_tag:
            await _publish(topics.LLM_EMOTION, {"tag": emotion_tag})
            logger.info("WaferAgent emotion: %s", emotion_tag)

        buf = speak_text.strip()
        if not buf:
            fallback = f"{self._robot_name}出了点问题，我歇一下"
            yield (fallback, fallback)
            return

        def _cut(buffer: str) -> int:
            """Index of the first sentence boundary, or -1."""
            for i, ch in enumerate(buffer):
                if ch in "。！？!?\n；;":
                    return i
                if ch in "，," and i >= 10:
                    return i
            return -1

        while True:
            cut = _cut(buf)
            if cut < 0:
                break
            sentence, buf = buf[:cut + 1].strip(), buf[cut + 1:]
            if sentence:
                yield (sentence, sentence)
        tail = buf.strip()
        if tail:
            yield (tail, tail)

        # Keep recent context for the bridge composer (D14)
        self._memory.append(text)
        if speak_text.strip():
            self._memory.append(speak_text.strip())
        self._memory = self._memory[-RECENT_MAX:]

    def handle_interrupt(self, heard_response: str) -> None:
        """Called when user interrupts the agent mid-speech."""
        logger.info("WaferAgent interrupted (heard: %s)", heard_response[:40])

    def set_memory_from_history(self, conf_uid: str, history_uid: str) -> None:
        """v1: no-op (memory is v1.2/M7 scope per D30)."""
        pass


class _FakeHub:
    """Fallback when the real hub is unreachable (gate degraded)."""

    async def publish(self, topic: str, payload: dict) -> None:
        logger.debug("FakeHub publish %s (hub unreachable)", topic)


# ---------------------------------------------------------------------------
# P5: Wake word (OLVCORE.02, D42) — server-side standby → active switching
# ---------------------------------------------------------------------------

_KWS_ENGINE: Any = None
_KWS_FAILED = False
_KWS_TWO_LEVEL = False  # TASK.KWS.03: low-threshold + transcript re-check
# TASK.WAKE.08 (2026-10-29): VAD can split "小饼小饼" across two segments;
# keep a short tail so the four-syllable keyword still matches.
_STANDBY_TAIL_SECONDS = 3.0
_STANDBY_MAX_SECONDS = 6.0
_STANDBY_TAIL_GAP_S = 5.0
_standby_tail: Any = None
_standby_tail_at: float = 0.0


def _merge_standby_audio(previous: Any, current: Any,
                         sample_rate: int = 16000,
                         tail_seconds: float = _STANDBY_TAIL_SECONDS,
                         max_seconds: float = _STANDBY_MAX_SECONDS):
    """Concatenate the previous standby tail with this segment.

    Returns (merged_for_kws, new_tail). Pure helper so CI can test the
    boundary behaviour without loading sherpa.
    """
    import numpy as np

    cur = np.asarray(current, dtype=np.float32).reshape(-1)
    if previous is None:
        merged = cur
    else:
        prev = np.asarray(previous, dtype=np.float32).reshape(-1)
        merged = cur if prev.size == 0 else np.concatenate([prev, cur])
    max_len = max(1, int(max_seconds * sample_rate))
    if merged.size > max_len:
        merged = merged[-max_len:]
    tail_len = max(1, int(tail_seconds * sample_rate))
    tail = merged[-tail_len:] if merged.size > tail_len else merged.copy()
    return merged, tail


def _ensure_kws() -> bool:
    """Lazy-load the KWS engine (sherpa-onnx, D31).

    Uses absolute paths (_ROOT) because the OLV process CWD is the fork
    directory, not the repo root.

    Env knobs (TASK.KWS.03 two-level verification):
      WAFER_KWS_THRESHOLD   explicit threshold (default 0.25 production)
      WAFER_KWS_TWO_LEVEL=1 low threshold (default 0.12) + ASR transcript
                            re-check; false triggers are dropped silently
    """
    global _KWS_ENGINE, _KWS_FAILED, _KWS_TWO_LEVEL
    if _KWS_ENGINE is not None or _KWS_FAILED:
        return _KWS_ENGINE is not None
    try:
        from engines.wakeword import SherpaKwsEngine

        kws_dir = _ROOT / "models" / "wakeword" / "sherpa-kws-wenetspeech"
        threshold = float(os.environ.get("WAFER_KWS_THRESHOLD", "0.25"))
        _KWS_TWO_LEVEL = os.environ.get("WAFER_KWS_TWO_LEVEL") == "1"
        if _KWS_TWO_LEVEL and "WAFER_KWS_THRESHOLD" not in os.environ:
            threshold = 0.12  # recall-first; ASR re-check filters false hits
        _KWS_ENGINE = SherpaKwsEngine(model_dir=kws_dir,
                                      keywords_threshold=threshold)
        logger.info("wafer KWS ready (threshold=%.2f, two_level=%s, dir=%s)",
                    threshold, _KWS_TWO_LEVEL, kws_dir)
        return True
    except Exception as exc:  # noqa: BLE001
        _KWS_FAILED = True
        logger.warning("wafer KWS unavailable (always-on mode): %s", exc)
        return False


class _SessionMode:
    """Server-side standby/listening state (D22, no browser changes needed).

    standby:  audio → KWS only (zero ASR, zero LLM)
    listening: audio → gate → ASR → agent (full chain)
    """

    def __init__(self) -> None:
        self.state = "standby"  # D22: sleep default
        self.timeout_s = 300.0  # 5 min idle → back to standby
        self.pending_work = False  # D48: armed by bare work-wake
        # TASK.KWS.03: set when a low-threshold KWS hit still awaits the
        # ASR transcript confirmation of the wake word
        self.wake_needs_verify = False
        if os.environ.get("WAFER_ALWAYS_ON") == "1":  # staged debugging
            import time as _t
            self.state = "listening"
            self.last_active = _t.monotonic()
            logger.info("wafer session: listening (WAFER_ALWAYS_ON)")

    def is_standby(self) -> bool:
        import time

        if self.state == "listening":
            if time.monotonic() - self.last_active > self.timeout_s:
                self.state = "standby"
                self.wake_needs_verify = False
                self.pending_work = False  # D48: expires with the session
                logger.info("session timeout -> standby")
        return self.state == "standby"

    def wake(self) -> None:
        import time

        self.state = "listening"
        self.last_active = time.monotonic()

    def keep_alive(self) -> None:
        import time

        self.last_active = time.monotonic()


_session = _SessionMode()


def _wake_mode() -> str:
    """v0.1 wake source: ``asr`` (default) or ``kws`` (experimental)."""
    mode = os.environ.get("WAFER_WAKE_MODE", "asr").strip().lower()
    if mode not in ("asr", "kws"):
        logger.warning("unknown WAFER_WAKE_MODE=%r, using asr", mode)
        return "asr"
    return mode


def _has_asr_wake(text: str) -> bool:
    """Match the wake phrase after ASR, ignoring spaces/punctuation."""
    compact = re.sub(r"[\s，。！？、,.!?]+", "", text or "")
    return "小饼小饼" in compact or WORK_WAKE_KEYWORD in compact


logger.info("wafer session mode=%s always_on=%r wake_mode=%s",
            _session.state, os.environ.get("WAFER_ALWAYS_ON"), _wake_mode())


def _warmup_models() -> None:
    """Load KWS + voiceprint in the background at OLV startup.

    Measured 2026-10-25: first real utterance paid ~2s (sherpa KWS) + ~2s
    (CAM++ session) before the gate could even run -- users perceived it as
    "very slow". Warming after import moves that cost off the first turn.
    """
    if os.environ.get("WAFER_NO_WARMUP") == "1":
        return

    def _run() -> None:
        import time

        began = time.monotonic()
        try:
            _ensure_kws()
            _warm_verifier_sync()
            logger.info("wafer warmup done in %.2fs", time.monotonic() - began)
        except Exception as exc:  # noqa: BLE001 -- never break startup
            logger.warning("wafer warmup failed: %s", exc)

    import threading

    threading.Thread(target=_run, daemon=True, name="wafer-warmup").start()


def _warm_verifier_sync() -> None:
    """Load CAM++ and run one dummy score -- without the hub / event loop."""
    import numpy as np

    if _state.verifier is not None and _state.setup is not None:
        pass
    else:
        setup = load_gate_setup(_speakers_config_path())
        if not setup.voiceprint_model:
            raise FileNotFoundError("voiceprint.model not set")
        model_path = _ROOT / setup.voiceprint_model
        if not model_path.exists():
            raise FileNotFoundError(f"CAM++ model not found: {model_path}")
        store = EnrollmentStore(_ROOT / setup.embedding_dir)
        _state.setup = setup
        _state.verifier = SherpaCampplusVerifier(store, model_path)
    _state.verifier.scores(np.zeros(8000, dtype=np.int16).tobytes())


_warmup_models()


def _dump_standby(audio: Any) -> None:
    """Dev aid: save the dropped standby segment for offline KWS debugging.

    Enabled with WAFER_DUMP_STANDBY=1; writes logs/standby_last.wav (overwrite)
    and logs the level so we can tell "too quiet" from "KWS miss".
    """
    if os.environ.get("WAFER_DUMP_STANDBY") != "1":
        return
    try:
        import wave

        import numpy as np

        data = np.asarray(audio, dtype=np.float32)
        if data.size == 0:
            return
        peak = float(np.max(np.abs(data)))
        rms = float(np.sqrt(np.mean(data * data)))
        path = _ROOT / "logs" / "standby_last.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(
                (np.clip(data, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes())
        logger.info("standby dump -> %s (peak=%.3f rms=%.4f)", path, peak, rms)
    except Exception as exc:  # noqa: BLE001 -- diagnostics must never break
        logger.warning("standby dump failed: %s", exc)


async def wake_check(audio: Any) -> bool:
    """P5: check audio for wake word. Returns True if we should wake.

    Called by the P1 hook when in standby mode. If the KWS engine is
    unavailable, returns True (always-on fallback — the gate still runs).
    """
    if not _ensure_kws():
        return True  # degraded: no KWS, always-on (gate still protects)

    import numpy as np

    if not isinstance(audio, np.ndarray) or audio.size == 0:
        return False

    global _standby_tail, _standby_tail_at
    import time as _time

    now = _time.monotonic()
    prev = (_standby_tail
            if now - _standby_tail_at <= _STANDBY_TAIL_GAP_S else None)
    merged, tail = _merge_standby_audio(prev, audio)
    pcm = (np.clip(merged, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
    result = _KWS_ENGINE.detect(pcm)
    if result.hit:
        _standby_tail = None
        _standby_tail_at = 0.0
        logger.info("wake word detected! (score=%.3f)", result.score)
        _session.wake()
        if _KWS_TWO_LEVEL:
            # low threshold passed: the transcript must confirm the wake
            # word before anything is answered (WaferAgent silent-drop)
            _session.wake_needs_verify = True
        # D48: publish the real hit keyword (was hardcoded "auto")
        await _publish(topics.WAKE_WORD,
                       {"keyword": result.keyword or "auto"})
        return True
    _standby_tail = tail
    _standby_tail_at = now
    logger.info("wake: miss (score=%.3f, samples=%d merged=%d)", result.score,
                audio.size, merged.size)
    return False

async def publish_state(state: str) -> None:
    """P4: publish session.state to hub (called by fork patches)."""
    await _publish(topics.SESSION_STATE, {"state": state})
