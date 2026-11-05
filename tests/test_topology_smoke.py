"""W.8: full-topology smoke test over a REAL hub server.

Six roles, each on its own HubClient connection (as they would be separate
processes in production):
  - hub        : HubServer (TCP, ephemeral port)
  - olv        : ConversationPipeline + Bridge(RemoteExecutor), sim engines
  - bridge     : BridgeService (the independent opencode channel, D23)
  - render     : RenderEventBridge + ClawFace
  - ctl        : CtlService (toasts)
  - gamedog    : GameDog with scripted process provider
  - probe      : passive event collector (assertions)

Scenario: wake -> chat (emotion crosses to render) -> task dispatch
(pipeline -> TASK_REQUEST -> bridge service -> progress/result back on the
bus) -> in-grace amend (cancel + re-dispatch) -> member denied -> game
enter/exit (render scene switch). Every hop crosses a socket.
"""

import asyncio
from pathlib import Path

import pytest

from bridge.server import Bridge, RemoteExecutor
from bridge.service import BridgeService
from core.bus import topics
from core.bus.hub import HubServer
from core.bus.hub_client import HubClient
from core.gate.policy import GateConfig, SpeakerProfile
from core.interfaces import Route
from core.pipeline import ConversationPipeline
from core.session.state_machine import Event
from core.sim import CaptureTts, FixtureAsr, ScriptedLlm, ScriptedVerifier
from ctl import CtlService
from gamedog.mode import ModeManager
from gamedog.service import GameDog
from gamedog.watcher import ProcessWatcher
from render.claw_face import ClawFace
from render.event_bridge import RenderEventBridge

PROFILES = {
    "master": SpeakerProfile(speaker_id="master", name="主人", role="admin", priority=1),
    "wife": SpeakerProfile(speaker_id="wife", name="老婆", role="member", priority=2),
}


class ScriptedProvider:
    """Process-list provider with injectable snapshots (gamedog tests)."""

    def __init__(self) -> None:
        self.snapshot: set[str] = set()

    def __call__(self) -> set[str]:
        return set(self.snapshot)


async def until(cond, timeout: float = 3.0, step: float = 0.02) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while not cond():
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError("topology condition not met in time")
        await asyncio.sleep(step)


class Topology:
    """One full system wired over a real hub, all engines simulated."""

    def __init__(self, tmp_path: Path) -> None:
        (tmp_path / "transcripts.json").write_text("[]", encoding="utf-8")
        (tmp_path / "replies.json").write_text("[]", encoding="utf-8")
        self.tmp_path = tmp_path
        self.server = HubServer("127.0.0.1", 0)

        # per-role collections (assertion targets)
        self.probe_events: list[tuple[str, dict]] = []

    async def start(self, grace: float = 999.0, exec_delay: float = 0.02) -> None:
        await self.server.start()
        port = self.server.port

        async def client() -> HubClient:
            c = HubClient("127.0.0.1", port)
            await c.connect()
            return c

        # probe: sees everything task/mode/session related
        self.probe = await client()
        for t in (topics.TASK_REQUEST, topics.TASK_PROGRESS, topics.TASK_RESULT,
                  topics.TASK_CANCEL, topics.MODE_SWITCH, topics.SESSION_STATE):
            self.probe.subscribe(t, lambda t_, p: self.probe_events.append((t_, dict(p))))

        # olv process: pipeline + bridge decision half
        bus_olv = await client()
        self.bridge = Bridge(
            bus=bus_olv,
            session_book_path=self.tmp_path / "sb.json",
            grace_seconds=grace,
            executor=RemoteExecutor(),
        )
        self.pipeline = ConversationPipeline(
            source=None,
            verifier=ScriptedVerifier([{"master": 0.0}]),  # replaced per turn
            asr=FixtureAsr(self.tmp_path),
            llm=ScriptedLlm(self.tmp_path),
            tts=CaptureTts(),
            bus=bus_olv,
            bridge=self.bridge,
            profiles=PROFILES,
            gate_config=GateConfig(t_pass=0.62, t_gray=0.55),
        )
        self.bus_olv = bus_olv

        # bridge process: execution half
        self.bus_bsvc = await client()
        self.service = BridgeService(
            self.bus_bsvc,
            session_book_path=self.tmp_path / "sb_service.json",
            execution_delay=exec_delay,
        )
        await self.service.start()

        # render process
        self.bus_render = await client()
        self.face = ClawFace()
        self.render_bridge = RenderEventBridge(self.bus_render, self.face)
        await self.render_bridge.start()

        # ctl process
        self.bus_ctl = await client()
        self.ctl = CtlService(bus=self.bus_ctl)
        await self.ctl.handle_bus_events()

        # gamedog process
        self.bus_dog = await client()
        self.provider = ScriptedProvider()
        self.        dog = GameDog(
            bus=self.bus_dog,
            watcher=ProcessWatcher(["game.exe"], self.provider),
            mode=ModeManager(),
            exit_debounce_s=0.0,  # immediate exit confirmation (smoke test)
        )

    async def turn(self, scores: dict, transcript: str, reply: str = "好的") -> Route | None:
        """One voice turn: swap the scripted verifier/asr/llm payloads."""
        self.pipeline._verifier._sets = [dict(scores)]  # noqa: SLF001 (test rig)
        self.pipeline._asr._transcripts = [transcript]  # noqa: SLF001
        self.pipeline._asr._call_count = 0              # noqa: SLF001
        self.pipeline._llm._replies = [reply]           # noqa: SLF001
        self.pipeline._llm._call_count = 0              # noqa: SLF001
        return await self.pipeline.run_turn(b"\x00" * 100)

    def probe_of(self, topic: str) -> list[dict]:
        return [p for t, p in self.probe_events if t == topic]

    async def close(self) -> None:
        await self.bridge.close()
        for c in (self.probe, self.bus_olv, self.bus_bsvc,
                  self.bus_render, self.bus_ctl, self.bus_dog):
            await c.close()
        await self.server.close()


@pytest.fixture
async def topo(tmp_path):
    t = Topology(tmp_path)
    await t.start()
    yield t
    await t.close()


class TestTopologySmoke:
    async def test_wake_chat_emotion_crosses_to_render(self, topo) -> None:
        """Wake -> chat: the LLM emotion tag must reach the render process's
        face across the socket (the W.6 gap: it used to reach nobody)."""
        topo.pipeline.state_machine.handle(Event.WAKE)
        await asyncio.sleep(0.05)
        assert topo.face.state.expression != "sleepy"  # woke up

        result = await topo.turn(
            {"master": 0.75}, "今天好累啊", "[emotion:sad] 辛苦啦，休息一下吧"
        )
        assert result == Route.CHAT

        await until(lambda: topo.face.state.expression == "sad")
        # session states crossed too -- wait for both events before reading the
        # probe: under load the face update can arrive before the state events
        # (observed once in the full suite, 2026-10-29).
        await until(lambda: {"thinking", "speaking"} <= {
            p["state"] for p in topo.probe_of(topics.SESSION_STATE)})
        states = [p["state"] for p in topo.probe_of(topics.SESSION_STATE)]
        assert "thinking" in states
        assert "speaking" in states

    async def test_task_dispatch_crosses_processes(self, topo) -> None:
        """Voice task -> pipeline (olv process) -> TASK_REQUEST over the hub
        -> BridgeService (bridge process) executes -> progress/result events
        broadcast back. Every hop is a socket."""
        topo.pipeline.state_machine.handle(Event.WAKE)
        result = await topo.turn({"master": 0.75}, "派活，帮我修一下登录报错")
        assert result == Route.TASK_DIRECT

        # held in grace window (999s): nothing executed yet
        await asyncio.sleep(0.1)
        assert topo.probe_of(topics.TASK_REQUEST) == []

        # flush -> request crosses to the bridge process
        await topo.bridge.flush()
        await until(lambda: len(topo.probe_of(topics.TASK_REQUEST)) == 1)
        await until(lambda: len(topo.probe_of(topics.TASK_RESULT)) == 1)

        request = topo.probe_of(topics.TASK_REQUEST)[0]
        assert request["op"] == "execute"
        assert "登录" in request["task"]["original_text"]

        progress = [p["phase"] for p in topo.probe_of(topics.TASK_PROGRESS)]
        assert "dispatched" in progress
        assert "running" in progress

        result_ev = topo.probe_of(topics.TASK_RESULT)[0]
        assert "任务完成" in result_ev["summary"]

    async def test_in_grace_amend_crosses_processes(self, topo) -> None:
        """Amend inside the grace window: cancel event + amended re-dispatch,
        the corrected task reaching the bridge process."""
        topo.pipeline.state_machine.handle(Event.WAKE)
        await topo.turn({"master": 0.75}, "派活，帮我修一下登录报错")

        # correction lands before flush (grace=999)
        result = await topo.turn({"master": 0.75}, "不对，是改按钮颜色")
        assert result == Route.TASK_AMEND

        await until(lambda: len(topo.probe_of(topics.TASK_CANCEL)) == 1)
        assert topo.probe_of(topics.TASK_CANCEL)[0]["reason"] == "amendment"
        # nothing executed yet (zero-cost amend)
        assert topo.probe_of(topics.TASK_RESULT) == []

        await topo.bridge.flush()
        await until(lambda: len(topo.probe_of(topics.TASK_RESULT)) == 1)
        request = topo.probe_of(topics.TASK_REQUEST)[0]
        assert "改按钮颜色" in request["task"]["original_text"]

    async def test_member_denied_no_task_request(self, topo) -> None:
        """Member task request refused at the pipeline; the bridge process
        never hears about it (permission enforced before the wire)."""
        topo.pipeline.state_machine.handle(Event.WAKE)
        result = await topo.turn({"wife": 0.80}, "派活，帮我修一下报错")

        assert result == Route.TASK_DENIED
        await asyncio.sleep(0.1)
        assert topo.probe_of(topics.TASK_REQUEST) == []
        assert topo.probe_of(topics.TASK_RESULT) == []

    async def test_game_mode_switch_reaches_render_and_ctl(self, topo) -> None:
        """gamedog detects game -> mode.switch + scene command cross to the
        render process (typing face) and ctl shows a toast; exit restores."""
        topo.provider.snapshot = {"game.exe"}
        await topo.dog.poll_once()

        await until(lambda: topo.render_bridge.scene == "game_hud")
        assert topo.face.state.expression == "typing"

        mode_events = topo.probe_of(topics.MODE_SWITCH)
        assert mode_events and mode_events[0]["mode"] == "game"
        await until(lambda: any("游戏" in t.get("text", "") for t in topo.ctl.toast_log))

        # game exits -> back to idle
        topo.provider.snapshot = set()
        await topo.dog.poll_once()
        await until(lambda: topo.render_bridge.scene == "idle")
        assert topo.face.state.expression == "normal"

    async def test_gray_zone_led_command_crosses_to_render(self, topo) -> None:
        """Gray-zone gate -> question overlay reaches the render process."""
        topo.pipeline.state_machine.handle(Event.WAKE)
        result = await topo.turn({"master": 0.58}, "随便说点什么")
        assert result is None

        await until(lambda: topo.render_bridge.override_active)
        assert topo.face.state.expression == "confused"
