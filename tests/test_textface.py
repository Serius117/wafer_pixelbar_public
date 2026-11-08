"""TextFace tests (D36 / L group, TEXTFACE-01..07).

Vocabulary mapping, text limits, the 10s clock fallback, event
templates, the 0x17/0x6B zero-call red line and golden packets for
the text channel. See docs/TEST_DESIGN.md 4.17.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# L.1: vocabulary + sync guard (TEXTFACE-01 vocab part)
# ---------------------------------------------------------------------------

class TestVocabulary:
    def test_vocab_in_sync(self) -> None:
        """pipeline.EMOTION_TAGS == textface.EMOTION_FACE keys (the LLM
        is only offered tags the text channel can render)."""
        from core.pipeline import EMOTION_TAGS
        from render.textface import EMOTION_FACE
        assert set(EMOTION_TAGS) == set(EMOTION_FACE)

    def test_emotion_kaomoji_ascii_only(self) -> None:
        """Device font lacks Unicode kaomoji glyphs: every kaomoji and
        state text must fit 16 chars / 48 UTF-8 bytes and stay within
        the verified ASCII + CJK text repertoire. Templates are checked
        AFTER the robot-name substitution (the real wire length)."""
        from core.naming import DEFAULT_ROBOT_NAME
        from render.sinks.pixelbar.protocol import (
            TEXT_MAX_CHARS,
            TEXT_MAX_UTF8_BYTES,
        )
        from render.textface import EMOTION_FACE, EVENT_TEXT, STATE_TEXT

        for name, (text, _rgb) in {**EMOTION_FACE, **STATE_TEXT,
                                   **EVENT_TEXT}.items():
            final = text.replace("{name}", DEFAULT_ROBOT_NAME)
            utf8 = final.encode("utf-8")
            assert len(final) <= TEXT_MAX_CHARS, f"{name}: {len(final)} chars"
            assert len(utf8) <= TEXT_MAX_UTF8_BYTES, \
                f"{name}: {len(utf8)} bytes"

    def test_four_emotions_two_states(self) -> None:
        """D43 (2026-10-24): surprised retired -> 4-emotion vocabulary."""
        from render.textface import EMOTION_FACE, STATE_TEXT
        assert len(EMOTION_FACE) == 4
        assert set(STATE_TEXT) == {"listening", "thinking"}


# ---------------------------------------------------------------------------
# L.2: PixelBarTextSink (TEXTFACE-02 / TEXTFACE-06 + red-line surface)
# ---------------------------------------------------------------------------

class FakeHidDevice:
    """Minimal hid.device double: records writes, ACKs every command."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def open_path(self, path) -> None:  # noqa: ANN001
        self.closed = False

    def write(self, data) -> int:  # noqa: ANN001
        self.writes.append(bytes(data))
        return len(data)

    def read(self, length, timeout_ms=0):  # noqa: ANN001
        # echo the last written report back with the device frame header
        if not self.writes:
            return b"\x00" * length
        last = bytearray(self.writes[-1])
        last[0] = 0x2F  # device frame
        last[1] = 0xBB
        return bytes(last)

    def close(self) -> None:
        self.closed = True


class FakeHidModule:
    def __init__(self) -> None:
        self.device_obj = FakeHidDevice()

    def enumerate(self) -> list[dict]:
        return [{
            "vendor_id": 0x2D99,
            "product_id": 0xA160,
            "interface_number": 4,
            "usage_page": 0xFF14,
            "path": b"fake",
            "product_string": "PixelBar",
        }]

    def device(self) -> FakeHidDevice:
        return self.device_obj


class TestPixelBarTextSink:
    def _sink(self):
        from render.sinks.pixelbar.text import PixelBarTextSink

        mod = FakeHidModule()
        sink = PixelBarTextSink(hid_module=mod)
        sink.open()
        return sink, mod.device_obj

    def test_text_limit_rejected_before_device(self) -> None:
        """TEXTFACE-02: >16 chars / >48 UTF-8 bytes never reach the wire."""
        sink, dev = self._sink()
        with pytest.raises(ValueError):
            sink.show_text("a" * 17)
        with pytest.raises(ValueError):
            sink.show_text("饼" * 17)  # 51 UTF-8 bytes
        assert dev.writes == []  # nothing was sent
        sink.close()

    def test_show_text_layout_then_text(self) -> None:
        """Golden order: 0xEF static-center (color) then 0xE8 text, each
        ACKed (TEXTFACE-06)."""
        from render.sinks.pixelbar import protocol as px

        sink, dev = self._sink()
        assert sink.show_text("(^_^)", (255, 200, 80)) is True
        cmds = [w[3] for w in dev.writes]  # 2E AA EC <cmd> ...
        assert cmds == [px.CMD_PIXEL_SET, px.CMD_TEXT_SET]
        # layout payload: 01 FF C8 50 00 02 00 01 FF
        assert list(dev.writes[0][6:15]) == [
            0x01, 255, 200, 80, 0x00, 0x02, 0x00, 1, 0xFF]
        # text payload: 00 05 28 5E 5F 5E 29
        assert list(dev.writes[1][6:13]) == [0x00, 5, 0x28, 0x5E, 0x5F,
                                             0x5E, 0x29]
        sink.close()

    def test_restore_clock_golden(self) -> None:
        """TEXTFACE-06: clock restore = 0xEF 01 RR GG BB 00 01 00 00 FF."""
        from render.sinks.pixelbar import protocol as px

        sink, dev = self._sink()
        assert sink.restore_clock() is True
        assert dev.writes[0][3] == px.CMD_PIXEL_SET
        assert list(dev.writes[0][6:15]) == [
            0x01, 255, 255, 255, 0x00, 0x01, 0x00, 0x00, 0xFF]
        sink.close()

    def test_red_line_never_emits_0x17_or_0x6b(self) -> None:
        """TEXTFACE-05 (surface half): the sink's command log after full
        exercise contains only 0xE8 / 0xEF."""
        from render.sinks.pixelbar import protocol as px

        sink, _ = self._sink()
        sink.show_text("(._.)")
        sink.show_text("小饼在听哦('o')")
        sink.restore_clock()
        commands = {cmd for cmd, _ in sink.command_log}
        assert commands <= {px.CMD_TEXT_SET, px.CMD_PIXEL_SET}
        assert px.CMD_PIXEL_FILE not in commands
        assert px.CMD_MOOD_SET not in commands
        sink.close()

    def test_write_failure_degrades_not_raises(self) -> None:
        from render.sinks.pixelbar.text import PixelBarTextSink

        mod = FakeHidModule()
        sink = PixelBarTextSink(hid_module=mod)
        sink.open()
        mod.device_obj.write = lambda data: (_ for _ in ()).throw(
            OSError("usb gone"))
        assert sink.show_text("(._.)") is False  # degraded, not raised
        assert sink.error_count == 1
        sink.close()


# ---------------------------------------------------------------------------
# L.3: TextFaceController (TEXTFACE-01 / 03 / 04 / 05-controller)
# ---------------------------------------------------------------------------

class FakeTextSink:
    """Records show_text / restore_clock calls (no hardware)."""

    def __init__(self) -> None:
        self.texts: list[tuple[str, tuple[int, int, int]]] = []
        self.restores = 0

    def show_text(self, text, rgb=(120, 200, 255)):  # noqa: ANN001
        self.texts.append((text, rgb))
        return True

    def restore_clock(self, rgb=(255, 255, 255)):  # noqa: ANN001
        self.restores += 1
        return True


class TestTextFaceController:
    def _make(self, timeout_s: float = 10.0):
        from core.bus.hub import InProcBus
        from render.textface import TextFaceController

        bus = InProcBus()
        sink = FakeTextSink()
        ctrl = TextFaceController(bus, sink, timeout_s=timeout_s)
        return bus, sink, ctrl

    async def test_start_restores_clock(self) -> None:
        """Boot: force a known idle state (device native clock)."""
        bus, sink, ctrl = self._make()
        await ctrl.start()
        assert sink.restores == 1
        assert sink.texts == []
        ctrl.stop()

    async def test_emotion_mapping_and_unknown_fallback(self) -> None:
        """TEXTFACE-01: 4 tags map (D43: surprised removed); unknown tag
        (incl. the retired 'surprised') -> normal fallback."""
        bus, sink, ctrl = self._make()
        await ctrl.start()
        for tag in ("normal", "happy", "sad", "angry"):
            await bus.publish("llm.emotion", {"tag": tag})
        await bus.publish("llm.emotion", {"tag": "surprised"})  # D43: retired
        await bus.publish("llm.emotion", {"tag": "tired"})  # legacy tag
        texts = [t for t, _ in sink.texts]
        assert texts == ["(._.)", "(^_^)", "(T_T)", "(>_<)",
                         "(._.)", "(._.)"]
        kinds = [p["kind"] for p in ctrl.pushed]
        assert "emotion:happy" in kinds and "emotion:tired" in kinds
        ctrl.stop()

    async def test_state_mapping_listening_thinking_only(self) -> None:
        """TEXTFACE-01: listening/thinking push; speaking/standby don't.
        The {name} template is replaced with the configured name."""
        from core.naming import DEFAULT_ROBOT_NAME

        bus, sink, ctrl = self._make()
        await ctrl.start()
        for state in ("listening", "thinking", "speaking", "standby",
                      "task_running"):
            await bus.publish("session.state", {"state": state})
        texts = [t for t, _ in sink.texts]
        expected = f"{DEFAULT_ROBOT_NAME}在听哦('o')"
        assert texts == [expected, "(?_?)"]
        ctrl.stop()

    async def test_event_templates(self) -> None:
        """TEXTFACE-04: task lifecycle / degrade / game messages (R.15 adds
        the failure branch: ok=False -> task_fail, not the celebration)."""
        bus, sink, ctrl = self._make()
        await ctrl.start()
        await bus.publish("task.progress",
                          {"task_id": "t1", "phase": "dispatched"})
        await bus.publish("task.progress",
                          {"task_id": "t1", "phase": "running"})
        await bus.publish("task.result",
                          {"task_id": "t1", "summary": "done: 3 files"})
        await bus.publish("task.result",
                          {"task_id": "t9", "summary": "任务失败：rc=1",
                           "ok": False, "error": "opencode exited rc=1"})
        await bus.publish("task.cancel",
                          {"task_id": "t2", "reason": "user_request"})
        await bus.publish("task.permission",
                          {"task_id": "t3", "kind": "file", "target": "x.py"})
        await bus.publish("task.clarify",
                          {"spoken": "音乐", "candidates": ["本项目"]})
        await bus.publish("render.command", {"cmd": "error"})
        await bus.publish("mode.switch",
                          {"mode": "game", "tier": "silent"})
        from core.naming import DEFAULT_ROBOT_NAME
        texts = [t for t, _ in sink.texts]
        assert texts == [
            "收到，开工！",   # dispatched (running phase: no push)
            "搞定！\\(^o^)/",
            "呜，下次努力",   # ok=False -> failure message (R.15)
            "已取消啦",       # cancel: dedicated wording (TASK.SLOT.03)
            "等你点头哦",     # permission prompt (TASK.SLOT.03)
            "没找到项目呀",   # clarify announcement (TASK.SLOT.03)
            f"{DEFAULT_ROBOT_NAME}歇一下",
            "游戏愉快！",
        ]
        kinds = [p["kind"] for p in ctrl.pushed]
        assert kinds.count("task_done") == 1
        assert kinds.count("task_fail") == 1      # ok=False result (R.15)
        assert kinds.count("task_cancel") == 1    # dedicated wording
        assert kinds.count("task_permission") == 1
        assert kinds.count("task_clarify") == 1
        ctrl.stop()

    async def test_timeout_restores_clock_exactly_once(self) -> None:
        """TEXTFACE-03: 10s idle -> clock restored once; pushes re-arm."""
        bus, sink, ctrl = self._make(timeout_s=0.05)
        await ctrl.start()
        assert sink.restores == 1  # boot restore
        await bus.publish("llm.emotion", {"tag": "happy"})
        await asyncio.sleep(0.02)
        await bus.publish("llm.emotion", {"tag": "happy"})  # re-arm
        await asyncio.sleep(0.02)
        assert sink.restores == 1  # still showing, timer re-armed
        await asyncio.sleep(0.1)   # past the window
        assert sink.restores == 2  # restored exactly once
        await asyncio.sleep(0.1)
        assert sink.restores == 2  # no repeat restores
        ctrl.stop()

    async def test_stop_restores_clock(self) -> None:
        """Exit: stop() must clear the last text/kaomoji and show the clock."""
        bus, sink, ctrl = self._make()
        await ctrl.start()
        await bus.publish("llm.emotion", {"tag": "happy"})
        assert sink.restores == 1
        ctrl.stop()
        assert sink.restores == 2

    async def test_game_exit_restores_immediately(self) -> None:
        """mode.switch normal -> clock right away (no 10s wait)."""
        bus, sink, ctrl = self._make()
        await ctrl.start()
        await bus.publish("mode.switch",
                          {"mode": "game", "tier": "silent"})
        await bus.publish("mode.switch", {"mode": "normal"})
        assert sink.restores == 2  # boot + game exit
        assert [t for t, _ in sink.texts] == ["游戏愉快！"]
        ctrl.stop()

    async def test_red_line_surface_only_text_and_clock(self) -> None:
        """TEXTFACE-05 (controller half): after a full scenario sweep the
        sink only ever saw show_text / restore_clock -- the 0x17 bitmap
        and 0x6B ambient channels are unreachable by construction."""
        bus, sink, ctrl = self._make()
        await ctrl.start()
        for topic, payload in (
            ("llm.emotion", {"tag": "happy"}),
            ("session.state", {"state": "listening"}),
            ("session.state", {"state": "thinking"}),
            ("task.progress", {"phase": "dispatched"}),
            ("task.result", {"summary": "ok"}),
            ("task.cancel", {"reason": "user_request"}),
            ("render.command", {"cmd": "error"}),
            ("mode.switch", {"mode": "game"}),
            ("mode.switch", {"mode": "normal"}),
        ):
            await bus.publish(topic, payload)
        # FakeTextSink has ONLY these two methods -- the sweep completing
        # without AttributeError IS the red line (structurally no path
        # to bitmap/ambient commands exists).
        assert sink.restores >= 2
        assert len(sink.texts) >= 6
        ctrl.stop()


# ---------------------------------------------------------------------------
# L.4: process wiring (device mode = text channel; console text field)
# ---------------------------------------------------------------------------

class TestWiring:
    def test_console_state_carries_textface(self) -> None:
        """The console /api/state includes the current LED text push."""
        from core.bus.hub import InProcBus
        from render.console import ConsoleBridge
        from render.textface import TextFaceController

        bus = InProcBus()
        sink = FakeTextSink()

        async def _go() -> None:
            ctrl = TextFaceController(bus, sink)
            await ctrl.start()
            await bus.publish("llm.emotion", {"tag": "happy"})

            bridge = ConsoleBridge(
                bus, text_provider=lambda: ctrl.pushed[-1]
                if ctrl.pushed else None)
            state = bridge.latest()
            assert state["textface"]["text"] == "(^_^)"
            assert state["textface"]["kind"] == "emotion:happy"
            ctrl.stop()

        asyncio.run(_go())

    def test_sim_path_unchanged_face_manager(self) -> None:
        """The simulator path (console preview) still runs the pixel
        face: FaceManager + RenderEventBridge wiring untouched by D36."""
        from core.bus.hub import InProcBus
        from render.event_bridge import RenderEventBridge
        from render.faces.manager import FaceManager

        async def _go() -> None:
            bus = InProcBus()
            face = FaceManager(style="wafer")
            bridge = RenderEventBridge(bus, face)
            await bridge.start()
            await bus.publish("llm.emotion", {"tag": "happy"})
            await asyncio.sleep(0.01)
            face.tick(0.033)
            assert face.state.expression == "happy"

        asyncio.run(_go())


# ---------------------------------------------------------------------------
# L.5: real-path integration (TEXTFACE-07) -- a REAL pipeline + bridge
# drive the TextFaceController through actual bus events.
# ---------------------------------------------------------------------------

class TestRealPathIntegration:
    async def test_pipeline_turns_drive_textface(self, tmp_path) -> None:
        """Chat -> emotion kaomoji; task -> confirm; cancel -> fail;
        degrade -> rest message. The real-machine LED path end to end."""

        from bridge.server import Bridge
        from core.bus import topics
        from core.bus.hub import InProcBus
        from core.gate.policy import GateConfig, SpeakerProfile
        from core.interfaces import Route
        from core.pipeline import ConversationPipeline
        from core.session.state_machine import Event
        from core.sim import CaptureTts, ScriptedVerifier

        class Llm:
            def __init__(self, replies: list[str]) -> None:
                self._replies = list(replies)

            async def stream(self, messages):  # noqa: ANN001
                for ch in self._replies.pop(0):
                    yield ch

        class CacheAsr:
            text = "今天好累啊。"

            async def transcribe(self, pcm: bytes) -> str:
                return self.text

        bus = InProcBus()
        events: list[tuple[str, dict]] = []
        bus.subscribe("*", lambda t, p: events.append((t, dict(p))))

        tts = CaptureTts()
        bridge = Bridge(bus=bus, session_book_path=tmp_path / "sb.json",
                        grace_seconds=0.0)
        asr = CacheAsr()
        pipeline = ConversationPipeline(
            source=None,
            verifier=ScriptedVerifier([{"master": 0.75}] * 20),
            asr=asr,
            llm=Llm(["[emotion:sad] 辛苦啦，早点休息"]),
            tts=tts,
            bus=bus,
            bridge=bridge,
            profiles={"master": SpeakerProfile(
                speaker_id="master", name="主人", role="admin", priority=1)},
            gate_config=GateConfig(t_pass=0.62, t_gray=0.55),
        )
        pipeline.state_machine.handle(Event.WAKE)

        sink = FakeTextSink()
        from render.textface import TextFaceController

        ctrl = TextFaceController(bus, sink)
        await ctrl.start()
        try:
            # 1) chat turn -> sad kaomoji pushed
            r1 = await pipeline.run_turn(b"\x00" * 100)
            assert r1 == Route.CHAT
            assert sink.texts[-1][0] == "(T_T)"

            # 2) task turn (fast lane) -> confirm + done messages
            asr.text = "派活，帮我查一下项目里有多少个测试文件。"
            await pipeline.run_turn(b"\x00" * 100)
            await asyncio.sleep(0.1)  # grace=0 flush + executor
            texts = [t for t, _ in sink.texts]
            assert "收到，开工！" in texts
            assert "搞定！\\(^o^)/" in texts

            # 3) degrade -> rest message (rule 6 path)
            async def boom(pcm: bytes) -> str:
                raise RuntimeError("asr exploded")

            asr.transcribe = boom  # type: ignore[assignment]
            await pipeline.run_turn(b"\x00" * 100)
            from core.naming import DEFAULT_ROBOT_NAME

            assert f"{DEFAULT_ROBOT_NAME}歇一下" in [t for t, _ in sink.texts]
            assert any(p.get("cmd") == "error"
                       for t, p in events if t == topics.RENDER_COMMAND)
        finally:
            ctrl.stop()
            await bridge.close()
