"""M6 feature tests (v1.1): wake chain, sleep animation, L2 assets,
console, manual-task relay.

Covers TEST_DESIGN groups WAKE-*/SLEEP-*/REND-L2-*/CONSOLE-*/MANUAL-*
(registered in TEST_DESIGN v1.3).
"""

from __future__ import annotations

import asyncio
import time
import wave
from pathlib import Path

import httpx
import pytest

from core.bus import topics
from core.bus.hub import InProcBus
from core.interfaces import WakeResult
from core.session.state_machine import Event, SessionStateMachine
from core.wake import WakeController
from render.assets import AssetLibrary
from render.claw_face import ClawFace
from render.event_bridge import RenderEventBridge

ROOT = Path(__file__).parent.parent
KWS_DIR = ROOT / "models" / "wakeword" / "sherpa-kws-wenetspeech"
AUDIO = ROOT / "tests" / "fixtures" / "audio"


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


class FakeWakeEngine:
    """Scripted wake engine (hit on demand)."""

    def __init__(self) -> None:
        self.force_hit = False
        self.calls = 0

    def detect(self, pcm: bytes) -> WakeResult:
        self.calls += 1
        return WakeResult(hit=self.force_hit, score=1.0 if self.force_hit else 0.0)


class TestWakeController:
    async def test_hotkey_wakes_session(self) -> None:
        """WAKE-01: hotkey entry still works (the always-on fallback)."""
        bus = InProcBus()
        sm = SessionStateMachine()
        WakeController(bus=bus, state_machine=sm, engine=None)
        assert sm.state.value == "standby"
        await bus.publish(topics.CTL_HOTKEY, {"action": "wake"})
        assert sm.state.value == "listening"

    async def test_hotkey_other_action_ignored(self) -> None:
        bus = InProcBus()
        sm = SessionStateMachine()
        WakeController(bus=bus, state_machine=sm, engine=None)
        await bus.publish(topics.CTL_HOTKEY, {"action": "gate_toggle"})
        assert sm.state.value == "standby"

    async def test_keyword_hit_wakes_and_publishes(self) -> None:
        """WAKE-02: KWS hit -> wake.word event + session wakes."""
        bus = InProcBus()
        sm = SessionStateMachine()
        engine = FakeWakeEngine()
        controller = WakeController(bus=bus, state_machine=sm, engine=engine)

        wake_events: list[dict] = []
        bus.subscribe(topics.WAKE_WORD, lambda t, p: wake_events.append(p))

        engine.force_hit = True
        hit = await controller.feed_pcm(b"\x00" * 3200)
        assert hit is True
        assert sm.state.value == "listening"
        assert len(wake_events) == 1

    async def test_keyword_miss_stays_asleep(self) -> None:
        bus = InProcBus()
        sm = SessionStateMachine()
        engine = FakeWakeEngine()
        controller = WakeController(bus=bus, state_machine=sm, engine=engine)
        assert await controller.feed_pcm(b"\x00" * 3200) is False
        assert sm.state.value == "standby"

    async def test_no_engine_is_hotkey_only(self) -> None:
        """D22 fallback: KWS outage degrades to hotkey-only, never dead."""
        bus = InProcBus()
        sm = SessionStateMachine()
        controller = WakeController(bus=bus, state_machine=sm, engine=None)
        assert await controller.feed_pcm(b"\x00" * 100) is False
        controller.wake(source="hotkey")
        assert sm.state.value == "listening"

    async def test_kws_silent_while_awake(self) -> None:
        """WAKE-04: awake sessions do not re-trigger on the wake word
        (D8: wake word opens a session, not a per-turn requirement)."""
        bus = InProcBus()
        sm = SessionStateMachine()
        engine = FakeWakeEngine()
        controller = WakeController(bus=bus, state_machine=sm, engine=engine)
        sm.handle(Event.WAKE)
        engine.force_hit = True
        assert await controller.feed_pcm(b"\x00" * 3200) is False


class TestSherpaKwsEngine:
    """Integration against the real model (skipped when assets absent)."""

    @pytest.fixture
    def engine(self):
        if not (KWS_DIR / "keywords.txt").exists():
            pytest.skip("KWS model assets not present")
        from engines.wakeword import SherpaKwsEngine

        return SherpaKwsEngine()

    def test_wake_word_hits(self, engine) -> None:
        """WAKE-05: '小饼小饼' synthetic speech wakes (clean)."""
        result = engine.detect(read_wav(AUDIO / "wake_xiaobing.wav"))
        assert result.hit is True

    def test_daily_speech_no_false_alarm(self, engine) -> None:
        """WAKE-06: daily sentences never trigger (zero false wake)."""
        for name in ("chat_tired", "task_dispatch", "task_amend", "task_cancel"):
            result = engine.detect(read_wav(AUDIO / f"{name}.wav"))
            assert result.hit is False, f"false wake on {name}"


class TestMakeKeywords:
    def test_word_to_tokens(self) -> None:
        from scripts.make_keywords import split_syllable, word_to_tokens

        assert split_syllable("xiǎo") == ("x", "iǎo")
        assert split_syllable("bǐng") == ("b", "ǐng")
        assert split_syllable("zhōng") == ("zh", "ōng")
        assert split_syllable("ài") == ("", "ài")
        assert word_to_tokens("小饼") == ["x", "iǎo", "b", "ǐng"]

    def test_build_line(self) -> None:
        from scripts.make_keywords import build_line

        line, tokens = build_line("小饼", repeat=2)
        assert line == "x iǎo b ǐng x iǎo b ǐng @小饼"
        assert tokens == ["x", "iǎo", "b", "ǐng"] * 2

    def test_validate_against_real_tokens(self) -> None:
        from scripts.make_keywords import validate_tokens, word_to_tokens

        if not (KWS_DIR / "tokens.txt").exists():
            pytest.skip("KWS tokens not present")
        assert validate_tokens(word_to_tokens("小饼"), KWS_DIR / "tokens.txt") == []
        problems = validate_tokens(["zzz", "qqq"], KWS_DIR / "tokens.txt")
        assert len(problems) == 2


class TestSleepAnimation:
    def test_breathing_eases_in_sleep(self) -> None:
        """SLEEP-01: breathing amplitude shrinks as sleep deepens."""
        face = ClawFace()
        face.update(session_state="standby")

        # simulate 60s of sleep at 1s ticks (wall clock matters: sleep_since)
        face.state._sleep_since = time.monotonic() - 60.0  # noqa: SLF001
        deep = []
        for _ in range(20):
            face.tick(0.5)
            deep.append(abs(face.state.breathing))
        assert max(deep) <= 0.30  # eased from the awake 0.5 amplitude

    def test_waking_resets_sleep_state(self) -> None:
        face = ClawFace()
        face.update(session_state="standby")
        face.state._sleep_since = time.monotonic() - 30.0  # noqa: SLF001
        face.update(session_state="listening")
        face.tick(0.1)
        assert face.state._sleep_since == 0.0  # noqa: SLF001

    def test_peek_squints_lids(self) -> None:
        """SLEEP-02: the occasional peek reduces lid closure for ~1s."""
        face = ClawFace()
        face.update(session_state="standby")
        face.state._peek_until = time.monotonic() + 0.5  # noqa: SLF001
        frame = face.render()  # must not raise; peek params applied
        assert frame.width == 256 and frame.height == 32


class TestL2ImageAssets:
    def test_load_gif_and_png(self, tmp_path) -> None:
        """REND-L2-01: assets load with correct canvas size."""
        lib = AssetLibrary(ROOT / "assets" / "led")
        gif = lib.frames("celebrate")
        assert len(gif) >= 8  # animated
        assert all(f.width == 256 and f.height == 32 for f in gif)
        png = lib.frames("test_pattern")
        assert len(png) == 1

    def test_missing_asset_is_empty_not_error(self, tmp_path) -> None:
        """REND-L2-02: a missing easter egg never breaks rendering."""
        lib = AssetLibrary(tmp_path)
        assert lib.frames("nope") == []

    async def test_image_command_overlay(self) -> None:
        """REND-L2-03: render.command{image} plays the asset, then expires."""
        bus = InProcBus()
        face = ClawFace()
        bridge = RenderEventBridge(bus, face,
                                   assets=AssetLibrary(ROOT / "assets" / "led"))
        await bridge.start()

        await bus.publish(topics.RENDER_COMMAND,
                          {"cmd": "image", "asset": "test_pattern",
                           "duration_ms": 2000})
        frame = bridge.image_frame()
        assert frame is not None and frame.width == 256
        assert bridge.image_active is True

        bridge._image_until = time.monotonic() - 0.1  # force expiry  # noqa: SLF001
        assert bridge.image_frame() is None
        assert bridge.image_active is False

    async def test_unknown_asset_falls_back_to_face(self) -> None:
        bus = InProcBus()
        face = ClawFace()
        bridge = RenderEventBridge(bus, face)
        await bridge.start()
        await bus.publish(topics.RENDER_COMMAND,
                          {"cmd": "image", "asset": "ghost"})
        assert bridge.image_frame() is None  # face renders instead

    async def test_task_result_plays_celebrate(self) -> None:
        """REND-L2-04: task success triggers the celebration GIF."""
        bus = InProcBus()
        face = ClawFace()
        bridge = RenderEventBridge(bus, face,
                                   assets=AssetLibrary(ROOT / "assets" / "led"))
        await bridge.start()
        await bus.publish(topics.TASK_RESULT,
                          {"task_id": "t-1", "summary": "任务完成：修好了"})
        assert bridge.image_active is True
        assert face.state.status_text == "任务完成：修好了"
        frame = bridge.image_frame()
        assert frame is not None


class TestConsole:
    @pytest.fixture
    async def console(self):
        from render.console import serve

        bus = InProcBus()
        server, bridge = serve(bus, port=0,
                                loop=asyncio.get_running_loop())  # ephemeral
        port = server.server_address[1]
        yield bus, bridge, port
        server.shutdown()

    async def test_index_served(self, console) -> None:
        _, _, port = console
        async with httpx.AsyncClient() as client:
            r = await client.get(f"http://127.0.0.1:{port}/")
        assert r.status_code == 200
        assert "console" in r.text

    async def test_state_and_events(self, console) -> None:
        """CONSOLE-01: bus events land in the rolling log and the summary."""
        bus, bridge, port = console
        await bus.publish(topics.SESSION_STATE, {"state": "listening"})
        await bus.publish(topics.GATE_DECISION,
                          {"verdict": "pass", "speaker_id": "master", "score": 0.8})
        await asyncio.sleep(0.05)

        async with httpx.AsyncClient() as client:
            state = (await client.get(f"http://127.0.0.1:{port}/api/state")).json()
            events = (await client.get(
                f"http://127.0.0.1:{port}/api/events?since=0")).json()
        assert state["session_state"] == "listening"
        assert state["gate"]["speaker_id"] == "master"
        assert len(events["events"]) == 2
        assert events["latest_seq"] == 2

        # incremental polling
        async with httpx.AsyncClient() as client:
            again = (await client.get(
                f"http://127.0.0.1:{port}/api/events?since=2")).json()
        assert again["events"] == []

    async def test_state_task_lifecycle(self, console) -> None:
        """TEST.CONSOLE.04 (R.21): task phases in /api/state derivation."""
        bus, bridge, _ = console

        async def phase_after(*events) -> dict:
            for topic, payload in events:
                await bus.publish(topic, payload)
            await asyncio.sleep(0.05)
            return bridge.latest()["task"]

        t = await phase_after(
            (topics.TASK_REQUEST,
             {"task": {"task_id": "t1", "prompt": "查一下测试数量"}}),
            (topics.TASK_PROGRESS,
             {"task_id": "t1", "phase": "running", "detail": "opencode 启动"}),
        )
        assert t["phase"] == "running"
        assert t["prompt"] == "查一下测试数量"
        assert t["detail"] == "opencode 启动"

        # permission overrides running; detail carries kind+target
        t = await phase_after(
            (topics.TASK_PERMISSION,
             {"task_id": "t1", "kind": "file", "target": "a.py"}),
        )
        assert t["phase"] == "permission"
        assert "file" in t["detail"] and "a.py" in t["detail"]

        # subsequent progress returns to running (post-approve path)
        t = await phase_after(
            (topics.TASK_PROGRESS,
             {"task_id": "t1", "phase": "running", "detail": "写文件"}),
        )
        assert t["phase"] == "running"

        # result wins over everything
        t = await phase_after(
            (topics.TASK_RESULT, {"task_id": "t1", "ok": True, "summary": "41 个"}),
        )
        assert t["phase"] == "done"
        assert t["summary"] == "41 个"

    async def test_state_clarify(self, console) -> None:
        """TEST.CONSOLE.05 (R.21/SLOT.03): clarify phase + candidates."""
        bus, bridge, _ = console
        await bus.publish(topics.TASK_CLARIFY,
                          {"spoken": "音", "candidates": ["音乐", "音响"]})
        await asyncio.sleep(0.05)
        t = bridge.latest()["task"]
        assert t["phase"] == "clarify"
        assert "音乐" in t["detail"] and "音响" in t["detail"]
        assert t["candidates"] == ["音乐", "音响"]

    async def test_control_actions_publish_bus_events(self, console) -> None:
        """CONSOLE-02 (D13 boundary): buttons only publish bus events."""
        bus, _, port = console
        seen: list[tuple[str, dict]] = []
        bus.subscribe("*", lambda t, p: seen.append((t, dict(p))))

        async with httpx.AsyncClient() as client:
            r = await client.post(f"http://127.0.0.1:{port}/api/ctl",
                                  json={"action": "wake"})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert any(t == topics.CTL_HOTKEY and p.get("action") == "wake"
                   for t, p in seen)

    async def test_unknown_action_rejected(self, console) -> None:
        _, _, port = console
        async with httpx.AsyncClient() as client:
            r = await client.post(f"http://127.0.0.1:{port}/api/ctl",
                                  json={"action": "rm -rf"})
        assert r.status_code == 400


class TestConsoleCompare:
    """TEST.CONSOLE.01-03: five-style compare view (TASK.CONSOLE.01)."""

    @pytest.fixture
    async def console(self):
        from render.console import serve

        bus = InProcBus()
        server, bridge = serve(bus, port=0,
                                loop=asyncio.get_running_loop())  # ephemeral
        port = server.server_address[1]
        yield bus, bridge, port
        server.shutdown()

    async def test_compare_page_served(self, console) -> None:
        """TEST.CONSOLE.03: /compare serves the comparison page."""
        _, _, port = console
        async with httpx.AsyncClient() as client:
            r = await client.get(f"http://127.0.0.1:{port}/compare")
        assert r.status_code == 200
        assert "compare" in r.text
        # the main console links to it
        async with httpx.AsyncClient() as client:
            r = await client.get(f"http://127.0.0.1:{port}/")
        assert "/compare" in r.text

    async def test_compare_returns_five_live_frames(self, console) -> None:
        """TEST.CONSOLE.01: /api/compare renders all 5 styles live (1bpp)."""
        import base64

        _, _, port = console
        async with httpx.AsyncClient() as client:
            d = (await client.get(
                f"http://127.0.0.1:{port}/api/compare?expression=happy")
                ).json()
        assert d["expression"] == "happy"
        assert [s["id"] for s in d["styles"]] == [
            "wafer", "mochi", "bubble", "mech", "claw"]
        frames = {}
        for s in d["styles"]:
            assert s["ok"] is True
            raw = base64.b64decode(s["frame"])
            assert len(raw) == 1024  # 256x32 @ 1bpp
            assert any(raw)  # non-empty bitmap
            frames[s["id"]] = raw
        # five styles genuinely differ (same expression, distinct geometry)
        assert len({bytes(v) for v in frames.values()}) == 5
        # expression switch produces a different frame
        async with httpx.AsyncClient() as client:
            d2 = (await client.get(
                f"http://127.0.0.1:{port}/api/compare?expression=sad")
                ).json()
        assert base64.b64decode(d2["styles"][0]["frame"]) != frames["wafer"]

    async def test_compare_flags_unsupported(self, console) -> None:
        """TEST.CONSOLE.02: unsupported expression flagged per style."""
        _, _, port = console
        async with httpx.AsyncClient() as client:
            d = (await client.get(
                f"http://127.0.0.1:{port}/api/compare?expression=skeptic")
                ).json()
        by_id = {s["id"]: s for s in d["styles"]}
        # wafer's vocabulary has no 'skeptic' -> flagged, no frame
        assert by_id["wafer"]["ok"] is False
        assert by_id["wafer"]["frame"] is None
        # claw supports it -> live frame
        assert by_id["claw"]["ok"] is True
        assert by_id["claw"]["frame"]


class TestManualTaskRelay:
    async def test_manual_summary_relays_as_result(self, tmp_path) -> None:
        """MANUAL-01 (T6.14/D23): manual opencode tasks flow back through
        the same task.result consumers (LED + toast)."""
        from bridge.service import BridgeService

        bus = InProcBus()
        service = BridgeService(bus, session_book_path=tmp_path / "sb.json")
        await service.start()

        results: list[dict] = []
        bus.subscribe(topics.TASK_RESULT, lambda t, p: results.append(p))
        await bus.publish(topics.TASK_MANUAL,
                          {"summary": "手动任务完成：重构了配置模块"})
        await asyncio.sleep(0.05)

        assert len(results) == 1
        assert "手动任务完成" in results[0]["summary"]

    async def test_manual_phase_relays_as_progress(self, tmp_path) -> None:
        from bridge.service import BridgeService

        bus = InProcBus()
        service = BridgeService(bus, session_book_path=tmp_path / "sb.json")
        await service.start()

        progress: list[dict] = []
        bus.subscribe(topics.TASK_PROGRESS, lambda t, p: progress.append(p))
        await bus.publish(topics.TASK_MANUAL, {"phase": "running",
                                                "detail": "opencode"})
        await asyncio.sleep(0.05)
        assert progress and progress[0]["phase"] == "running"
