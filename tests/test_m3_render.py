"""Tests for M3: PixelBarSink + ClawFace expressions + render pipeline v2."""

import time
from unittest.mock import MagicMock, patch

import pytest

from core.interfaces import Frame
from render.claw_face import EXPRESSIONS, ClawFace
from render.pipeline import RenderPipeline
from render.sinks.pixelbar import PixelBarSink
from render.sinks.pixelbar import protocol as px


class TestPixelBarSink:
    def test_init_defaults(self) -> None:
        sink = PixelBarSink()
        assert sink._vid == 0x2D99
        assert sink._pid is None  # auto-discover (0xA106 / 0xA160)
        assert sink._fps == 30
        assert not sink.is_open

    def test_push_without_open_logs_warning(self) -> None:
        sink = PixelBarSink()
        frame = Frame(pixels=b"\x00" * (256 * 32 * 3))
        with patch.dict("sys.modules", {"hid": MagicMock(enumerate=MagicMock(return_value=[]))}):
            sink.push(frame)  # should not raise, reconnect finds nothing
        assert sink.frame_count == 0

    def test_open_no_device_raises(self) -> None:
        sink = PixelBarSink()
        # Mock the hid module that's imported inside open()
        with patch.dict("sys.modules", {"hid": MagicMock(enumerate=MagicMock(return_value=[]))}):
            with pytest.raises(RuntimeError, match="not found"):
                sink.open()


class TestClawFaceExpressions:
    def test_all_expressions_defined(self) -> None:
        assert len(EXPRESSIONS) >= 19  # 16 canonical + 3 special

    def test_render_returns_valid_frame(self) -> None:
        face = ClawFace()
        frame = face.render()
        assert frame.width == 256
        assert frame.height == 32
        assert len(frame.pixels) == 256 * 32 * 3

    def test_set_expression(self) -> None:
        face = ClawFace()
        for expr in ["happy", "sad", "angry", "sleepy"]:
            face.set_expression(expr)
            assert face.state.expression == expr

    def test_invalid_expression_falls_to_normal(self) -> None:
        face = ClawFace()
        face.set_expression("nonexistent")
        assert face.state.expression == "normal"

    def test_each_expression_produces_different_pixels(self) -> None:
        face = ClawFace()
        frames = {}
        for expr in list(EXPRESSIONS.keys())[:8]:
            face.set_expression(expr)
            frames[expr] = face.render().pixels
        unique = len(set(frames.values()))
        assert unique >= 5, f"Only {unique} unique frames from {len(frames)} expressions"

    def test_update_with_signals(self) -> None:
        face = ClawFace()
        face.update(tts_amplitude=0.8)
        assert face.state.mouth_open == 0.8
        face.update(session_state="standby")
        assert face.state.expression == "sleepy"

    def test_tick_advances_breathing(self) -> None:
        face = ClawFace()
        face.tick(0.1)
        assert face.state.breathing != 0.0

    def test_blink_produces_closure(self) -> None:
        face = ClawFace()
        # Force blink to start
        face.state._next_blink = time.monotonic() - 0.01
        face.tick(0.01)  # first tick: starts blink, elapsed≈0
        assert face.state._blinking
        # Second tick after some time: blink should have progressed
        time.sleep(0.05)
        face.tick(0.05)
        assert face.state.blink_progress > 0.0

    def test_catchlight_renders(self) -> None:
        face = ClawFace()
        face.set_expression("happy")  # happy has catchlight
        frame = face.render()
        has_bright = any(
            frame.pixels[i] > 150 and frame.pixels[i + 1] > 200
            for i in range(0, len(frame.pixels), 3)
        )
        assert has_bright, "Happy expression should have catchlight pixels"


class TestRenderPipelineV2:
    def test_claw_face_integrated(self) -> None:
        rp = RenderPipeline()
        frames: list[Frame] = []
        rp.add_sink(frames.append)

        rp.update(emotion="happy", tts_amplitude=0.5)
        rp.render_frame()
        rp.update(session_state="listening")
        rp.render_frame()

        assert len(frames) == 2
        assert all(f.width == 256 and f.height == 32 for f in frames)

    def test_expression_transition_different_frames(self) -> None:
        face = ClawFace()
        face.set_expression("normal")
        frame1 = face.render()
        face.set_expression("angry")
        frame2 = face.render()
        assert frame1.pixels != frame2.pixels


class TestProtocolIntegration:
    def test_full_push_sequence(self) -> None:
        bitmap = bytes(range(256)) * (32 * 3 // 256 + 1)

        start = px.build_report(0x17, px.payload_file_start(0, len(bitmap)))
        assert len(start) == 64
        assert start[3] == 0x17

        chunk = bitmap[:51]
        data = px.build_report(0x17, px.payload_file_data(0, 0, chunk))
        assert len(data) == 64

        end = px.build_report(0x17, px.payload_file_end(0))
        assert len(end) == 64

    def test_pixelbar_sink_frame_rate_limiting(self) -> None:
        class AckDevice:
            def __init__(self) -> None:
                self.responses: list[bytes] = []

            def open_path(self, path) -> None:
                pass

            def close(self) -> None:
                pass

            def _queue(self, command: int, payload: list[int]) -> None:
                frame = [0x2F, 0xBB, 0xEC, command,
                         (len(payload) >> 8) & 0xFF, len(payload) & 0xFF, *payload]
                frame.append(sum(frame[1:]) & 0xFF)
                frame += [0] * (64 - len(frame))
                self.responses.append(bytes(frame))

            def write(self, data) -> int:
                payload = list(data[6:6 + ((data[4] << 8) | data[5])])
                if data[3] == px.CMD_PIXEL_SET:
                    self._queue(data[3], payload[:9])
                elif data[3] == 0x17:
                    mode, state = payload[0], payload[1]
                    if state == px.STATE_NOTIFY_DEVICE_RECEIVE:
                        self._queue(0x17, [mode, px.STATE_NOTIFY_APP_SEND_DATA, 0, 53])
                    elif state == px.STATE_SEND_DATA_TO_DEVICE:
                        self._queue(0x17, [mode, px.STATE_NOTIFY_APP_SEND_NEXT_DATA])
                    elif state == px.STATE_NOTIFY_DEVICE_SEND_DATA_END:
                        self._queue(0x17, [mode, px.STATE_NOTIFY_APP_CHECK_FINISH])
                return len(data)

            def read(self, size, timeout_ms=0) -> bytes:
                return self.responses.pop(0) if self.responses else b""

        sink = PixelBarSink(fps=10)
        sink._device = AckDevice()

        frame = Frame(pixels=b"\x00" * (256 * 32 * 3))
        sink.push(frame)
        assert sink.frame_count == 1

        sink.push(frame)
        assert sink.frame_count == 1  # rate-limited
