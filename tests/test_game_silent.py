"""Game mode "listen but cannot speak" tests (2026-10-16 user decision).

GAME-15  silent pipeline (llm=None/tts=None): voice task dispatch still
         works end-to-end; confirmations go to system.notify (toast/LED)
GAME-16  silent pipeline chat: template answer via notify, no LLM involved
GAME-17  silent-mode shape: engines absent yet gate/router/bridge intact
GAME-04  exit debounce: a gone game must stay gone for the window before
         the stack restarts; re-entering cancels the pending exit
GAME-05  multi-game: silent mode survives until the LAST whitelisted game
         exits (second game must not be torn down by the first one leaving)
GAME-10  task result during game mode reaches LED/toast channels
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from bridge.server import Bridge
from core.bus import topics
from core.bus.hub import InProcBus
from core.gate.policy import GateConfig, SpeakerProfile
from core.interfaces import Route
from core.pipeline import ConversationPipeline
from core.sim import FixtureAsr, ScriptedVerifier
from gamedog.mode import ModeManager
from gamedog.service import GameDog
from gamedog.stack import NoopStackController, StackController
from gamedog.watcher import ProcessWatcher
from render.claw_face import ClawFace
from render.event_bridge import RenderEventBridge

PROFILES = {
    "master": SpeakerProfile(speaker_id="master", name="主人", role="admin", priority=1),
}


class ScriptedProvider:
    def __init__(self) -> None:
        self.snapshot: set[str] = set()

    def __call__(self) -> set[str]:
        return set(self.snapshot)


class SpyStack(StackController):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def stop(self) -> None:
        self.calls.append("stop")

    def start(self) -> None:
        self.calls.append("start")


def make_silent_pipeline(tmp_path: Path, transcripts: list[str]):
    """Pipeline in game-silent shape: ASR + gate + router + bridge, NO LLM/TTS."""
    (tmp_path / "transcripts.json").write_text(json.dumps(transcripts), encoding="utf-8")
    (tmp_path / "replies.json").write_text('["unused"]', encoding="utf-8")
    bus = InProcBus()
    bridge = Bridge(bus=bus, session_book_path=tmp_path / "sb.json", grace_seconds=999.0)
    pipeline = ConversationPipeline(
        source=None,
        verifier=ScriptedVerifier([{"master": 0.75}] * len(transcripts)),
        asr=FixtureAsr(tmp_path),
        llm=None,  # silent mode: no local brain on the GPU
        tts=None,  # silent mode: cannot speak
        bus=bus,
        bridge=bridge,
        profiles=PROFILES,
    )
    return pipeline, bridge, bus


class TestGame15SilentTaskDispatch:
    async def test_task_dispatch_without_llm_tts(self, tmp_path) -> None:
        """Voice task works in game mode; confirmations become notifications."""
        pipeline, bridge, bus = make_silent_pipeline(
            tmp_path, ["派活，帮我修一下登录报错"]
        )
        notifies: list[dict] = []
        bus.subscribe(topics.SYSTEM_NOTIFY, lambda t, p: notifies.append(p))
        events: list[tuple[str, dict]] = []
        bus.subscribe("*", lambda t, p: events.append((t, dict(p))))

        result = await pipeline.run_turn(b"\x00" * 100)

        assert result == Route.TASK_DIRECT
        # confirmation spoken via the notify channel (toast/LED), not TTS
        assert any("收到" in n["text"] for n in notifies)
        assert bridge.has_work()  # held in grace

        await bridge.flush()
        results = [p for t, p in events if t == topics.TASK_RESULT]
        assert len(results) == 1
        assert "任务完成" in results[0]["summary"]
        await bridge.close()


class TestGame16SilentChatTemplate:
    async def test_chat_gets_template_answer_via_notify(self, tmp_path) -> None:
        pipeline, bridge, bus = make_silent_pipeline(tmp_path, ["陪我聊聊天"])
        notifies: list[dict] = []
        bus.subscribe(topics.SYSTEM_NOTIFY, lambda t, p: notifies.append(p))

        result = await pipeline.run_turn(b"\x00" * 100)

        assert result == Route.CHAT
        assert any("派活" in n["text"] for n in notifies)  # template guidance
        # no llm.emotion events (no LLM involved)
        await bridge.close()


class TestGame17SilentShape:
    async def test_gate_and_router_still_enforce(self, tmp_path) -> None:
        """Silent mode keeps the security spine: gate drops strangers,
        member task requests are refused (all rule-based, no LLM)."""
        (tmp_path / "transcripts.json").write_text(
            json.dumps(["派活，帮我修一下报错"]), encoding="utf-8"
        )
        (tmp_path / "replies.json").write_text('["unused"]', encoding="utf-8")
        bus = InProcBus()
        profiles = {
            "master": PROFILES["master"],
            "wife": SpeakerProfile(speaker_id="wife", name="老婆", role="member", priority=2),
        }
        pipeline = ConversationPipeline(
            source=None,
            verifier=ScriptedVerifier([{"wife": 0.80}, {"stranger": 0.90}]),
            asr=FixtureAsr(tmp_path),
            llm=None,
            tts=None,
            bus=bus,
            profiles=profiles,
            gate_config=GateConfig(t_pass=0.62, t_gray=0.55),
        )
        notifies: list[dict] = []
        bus.subscribe(topics.SYSTEM_NOTIFY, lambda t, p: notifies.append(p))

        # member refused (deterministic, via notify since no TTS)
        r1 = await pipeline.run_turn(b"\x00" * 100)
        assert r1 == Route.TASK_DENIED
        assert any("主人" in n["text"] for n in notifies)

        # unknown-but-scoring speaker treated as member -> also refused
        r2 = await pipeline.run_turn(b"\x00" * 100)
        assert r2 == Route.TASK_DENIED


class TestGame04ExitDebounce:
    async def test_short_lived_game_does_not_restore(self) -> None:
        bus = InProcBus()
        provider = ScriptedProvider()
        stack = SpyStack()
        dog = GameDog(bus=bus, watcher=ProcessWatcher(["game.exe"], provider),
                      mode=ModeManager(), stack=stack, exit_debounce_s=0.2)

        provider.snapshot = {"game.exe"}
        assert len(await dog.poll_once()) == 1
        assert stack.calls == ["stop"]

        # game disappears (launcher crash) -- NOT restored within the window
        provider.snapshot = set()
        await dog.poll_once()
        await asyncio.sleep(0.05)  # inside debounce window
        await dog.poll_once()
        assert dog.mode.mode != "normal"
        assert stack.calls == ["stop"]  # no premature stack restart

        # after the window it really restores
        await asyncio.sleep(0.2)
        switches = await dog.poll_once()
        assert len(switches) == 1
        assert dog.mode.mode == "normal"
        assert stack.calls == ["stop", "start"]

    async def test_reenter_cancels_pending_exit(self) -> None:
        bus = InProcBus()
        provider = ScriptedProvider()
        dog = GameDog(bus=bus, watcher=ProcessWatcher(["game.exe"], provider),
                      mode=ModeManager(), stack=NoopStackController(),
                      exit_debounce_s=0.2)

        provider.snapshot = {"game.exe"}
        await dog.poll_once()
        provider.snapshot = set()
        await dog.poll_once()          # exit candidate recorded
        provider.snapshot = {"game.exe"}
        await dog.poll_once()          # back before the window closed
        await asyncio.sleep(0.25)
        await dog.poll_once()          # debounce long past, but game is back
        assert dog.mode.mode == "game_silent"  # never restored


class TestGame05MultiGame:
    async def test_second_game_keeps_silent_mode(self) -> None:
        bus = InProcBus()
        provider = ScriptedProvider()
        stack = SpyStack()
        dog = GameDog(bus=bus, watcher=ProcessWatcher(["a.exe", "b.exe"], provider),
                      mode=ModeManager(), stack=stack, exit_debounce_s=0.0)

        provider.snapshot = {"a.exe"}
        await dog.poll_once()
        provider.snapshot = {"a.exe", "b.exe"}
        await dog.poll_once()
        assert dog.mode.mode == "game_silent"

        # a exits while b still runs -> must NOT restore
        provider.snapshot = {"b.exe"}
        await dog.poll_once()
        assert dog.mode.mode == "game_silent"
        assert stack.calls == ["stop"]

        # b also exits -> restore now
        provider.snapshot = set()
        switches = await dog.poll_once()
        assert len(switches) == 1
        assert dog.mode.mode == "normal"
        assert stack.calls == ["stop", "start"]


class TestGame10TaskResultDuringGameMode:
    async def test_result_reaches_led_and_toast_channels(self, tmp_path) -> None:
        """GAME-10: while in game scene, a finished task shows on HUD/toast."""
        (tmp_path / "transcripts.json").write_text(
            json.dumps(["派活，帮我跑个测试"]), encoding="utf-8"
        )
        (tmp_path / "replies.json").write_text('["unused"]', encoding="utf-8")

        bus = InProcBus()
        # render side in game scene
        face = ClawFace()
        render_bridge = RenderEventBridge(bus, face)
        await render_bridge.start()
        await bus.publish(topics.RENDER_COMMAND, {"cmd": "scene", "scene": "game_hud"})
        assert render_bridge.scene == "game_hud"

        # ctl side toasts
        from ctl import CtlService

        ctl = CtlService(bus=bus)
        await ctl.handle_bus_events()

        # silent pipeline dispatches a task
        bridge = Bridge(bus=bus, session_book_path=tmp_path / "sb.json", grace_seconds=0.0)
        pipeline = ConversationPipeline(
            source=None,
            verifier=ScriptedVerifier([{"master": 0.75}]),
            asr=FixtureAsr(tmp_path),
            llm=None,
            tts=None,
            bus=bus,
            bridge=bridge,
            profiles=PROFILES,
        )
        await pipeline.run_turn(b"\x00" * 100)
        await asyncio.sleep(0.2)

        # result flowed to the toast channel while the face stays in HUD
        assert any("任务完成" in t["text"] for t in ctl.toast_log)
        assert render_bridge.scene == "game_hud"
        await bridge.close()
