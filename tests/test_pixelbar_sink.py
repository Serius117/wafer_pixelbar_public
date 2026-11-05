"""PixelBar sink: mono bitmap encoding + ACK-driven 0x17 transfer (R.7)."""

import math

from core.interfaces import Frame
from render.sinks.pixelbar import PixelBarSink
from render.sinks.pixelbar import protocol as px


def blank_frame() -> Frame:
    return Frame(pixels=bytes(256 * 32 * 3), width=256, height=32)


def lit_frame(points) -> Frame:
    pixels = bytearray(256 * 32 * 3)
    for x, y in points:
        base = (y * 256 + x) * 3
        pixels[base:base + 3] = b"\xff\xff\xff"
    return Frame(pixels=bytes(pixels), width=256, height=32)


class AckDevice:
    """Fake HID device that answers the 0x17/0xEF state machine."""

    def __init__(self, finish: bool = True, retry_once: bool = False) -> None:
        self.finish = finish
        self.retry_once = retry_once
        self.retried = False
        self.responses: list[bytes] = []
        self.writes: list[bytes] = []
        self.opened_path = None
        # Live 0x6A reply shape from PID 0xA160 (2026-10-18)
        self.mood_reply = [0x20, 0x01,
                           0x01, 0xFF, 0x00, 0x00, 0x64, 0x08,
                           0x07, 0x00, 0x00, 0x01, 0xFF, 0xFF]

    def open_path(self, path) -> None:
        self.opened_path = path

    def close(self) -> None:
        pass

    def _queue(self, command: int, payload: list[int]) -> None:
        frame = [0x2F, 0xBB, 0xEC, command,
                 (len(payload) >> 8) & 0xFF, len(payload) & 0xFF, *payload]
        frame.append(sum(frame[1:]) & 0xFF)
        frame += [0] * (64 - len(frame))
        self.responses.append(bytes(frame))

    def write(self, data) -> int:
        raw = bytes(data)
        self.writes.append(raw)
        command = raw[3]
        length = (raw[4] << 8) | raw[5]
        payload = list(raw[6:6 + length])
        if command == px.CMD_MOOD_QUERY:
            self._queue(command, self.mood_reply)
        elif command == px.CMD_PIXEL_SET:
            self._queue(command, payload[:9])
        elif command == px.CMD_PIXEL_FILE:
            mode, state = payload[0], payload[1]
            if state == px.STATE_NOTIFY_DEVICE_RECEIVE:
                self._queue(command, [mode, px.STATE_NOTIFY_APP_SEND_DATA, 0, 53])
            elif state == px.STATE_SEND_DATA_TO_DEVICE:
                if self.retry_once and not self.retried:
                    self.retried = True
                    self._queue(command, [mode, px.STATE_NOTIFY_APP_CURSOR_ERROR, 0, 0])
                else:
                    self._queue(command, [mode, px.STATE_NOTIFY_APP_SEND_NEXT_DATA])
            elif state == px.STATE_NOTIFY_DEVICE_SEND_DATA_END:
                if self.finish:
                    self._queue(command, [mode, px.STATE_NOTIFY_APP_CHECK_FINISH])
                else:
                    self._queue(command, [mode, px.STATE_NOTIFY_APP_CHECK_ERROR])
        return len(raw)

    def read(self, size, timeout_ms=0) -> bytes:
        return self.responses.pop(0) if self.responses else b""


class FakeHidModule:
    def __init__(self, devices: list[dict], device: AckDevice) -> None:
        self._devices = devices
        self._device = device

    def enumerate(self) -> list[dict]:
        return self._devices

    def device(self) -> AckDevice:
        return self._device


def device_entry(pid: int, usage_page: int, path: bytes) -> dict:
    return {
        "vendor_id": px.VID,
        "product_id": pid,
        "interface_number": px.HID_INTERFACE,
        "usage_page": usage_page,
        "path": path,
        "product_string": "Fake PixelBar",
    }


class TestMonoEncoding:
    def test_size_and_headers(self) -> None:
        data = px.encode_mono_bitmap(blank_frame().pixels)
        assert len(data) == px.MONO_FILE_SIZE == 1064
        assert data[0:5] == bytes([0x02, 0x00, 0x00, 0xFF, 0xFF])
        assert data[532:537] == bytes([0x02, 0x00, 0x04, 0xFF, 0xFF])

    def test_global_left_shift_aligned_rows(self) -> None:
        # Rows 0-7: canvas x -> physical x-10 (canvas 0..9 fall off-screen).
        data = px.encode_mono_bitmap(lit_frame([(10, 0)]).pixels)
        assert data[20] == 0x01                    # page0 byte 0
        data = px.encode_mono_bitmap(lit_frame([(255, 0)]).pixels)
        assert data[148 + 117] == 0x01             # page1 byte 117
        data = px.encode_mono_bitmap(lit_frame([(0, 0), (9, 0)]).pixels)
        assert all(byte == 0 for byte in data[20:276])

    def test_global_left_shift_compensated_rows(self) -> None:
        # Rows 8-15: device shifts left 10, so canvas x is stored at byte x.
        data = px.encode_mono_bitmap(lit_frame([(10, 8)]).pixels)
        assert data[276 + 10] == 0x01              # page2 byte 10
        data = px.encode_mono_bitmap(lit_frame([(255, 8)]).pixels)
        assert data[404 + 127] == 0x01             # page3 byte 127
        # canvas 0..9 are stored at bytes 0..9; the panel renders them off-screen.
        data = px.encode_mono_bitmap(lit_frame([(0, 8)]).pixels)
        assert data[276] == 0x01

    def test_bottom_right_bit(self) -> None:
        data = px.encode_mono_bitmap(lit_frame([(255, 31)]).pixels)
        assert data[936 + 127] == 0x80             # unit1 page3 col127 bit7


class TestSinkTransfer:
    def _sink(self, device: AckDevice, devices=None) -> PixelBarSink:
        entries = devices or [device_entry(0xA160, px.USAGE_PAGE, b"control")]
        return PixelBarSink(
            hid_module=FakeHidModule(entries, device), fps=1000, ack_timeout=0.5)

    def test_pid_auto_discovery_prefers_control_collection(self) -> None:
        dev = AckDevice()
        entries = [
            device_entry(0xA106, 0xFF13, b"wrong-collection"),
            device_entry(0xA160, px.USAGE_PAGE, b"control"),
        ]
        sink = self._sink(dev, entries)
        sink.open()
        assert dev.opened_path == b"control"

    def test_push_selects_scene_waits_for_ack_and_finishes(self) -> None:
        dev = AckDevice()
        sink = self._sink(dev)
        sink.open()
        sink.push(blank_frame())

        assert sink.frame_count == 1
        assert sink.error_count == 0
        commands = [frame[3] for frame in dev.writes]
        assert commands[0] == px.CMD_PIXEL_SET       # WORK scene first
        assert commands[1] == px.CMD_PIXEL_SET
        start_index = commands.index(px.CMD_PIXEL_FILE)
        start = dev.writes[start_index]
        size = ((start[8] << 24) | (start[9] << 16)
                | (start[10] << 8) | start[11])
        assert size == px.MONO_FILE_SIZE
        chunks = math.ceil(size / px.PIXEL_FILE_CHUNK)
        data_writes = [
            frame for frame in dev.writes
            if frame[3] == px.CMD_PIXEL_FILE and frame[7] == px.STATE_SEND_DATA_TO_DEVICE
        ]
        assert len(data_writes) == chunks
        assert dev.writes[-1][7] == px.STATE_NOTIFY_DEVICE_SEND_DATA_END

    def test_cursor_error_retries_package(self) -> None:
        dev = AckDevice(retry_once=True)
        sink = self._sink(dev)
        sink.open()
        sink.push(blank_frame())

        assert sink.frame_count == 1
        data_writes = [
            frame for frame in dev.writes
            if frame[3] == px.CMD_PIXEL_FILE and frame[7] == px.STATE_SEND_DATA_TO_DEVICE
        ]
        assert len(data_writes) == math.ceil(px.MONO_FILE_SIZE / px.PIXEL_FILE_CHUNK) + 1

    def test_handshake_error_degrades_without_raising(self) -> None:
        dev = AckDevice(finish=False)
        sink = self._sink(dev)
        sink.open()
        sink.push(blank_frame())  # must not raise

        assert sink.frame_count == 0
        assert sink.error_count == 1
        assert sink.is_healthy is False


class TestSinkMoodLight:
    """Mood light API (0x6A/0x6B/0x72) — RE-corrected layout, 2026-10-18."""

    def _sink(self, device: AckDevice) -> PixelBarSink:
        entries = [device_entry(0xA160, px.USAGE_PAGE, b"control")]
        return PixelBarSink(
            hid_module=FakeHidModule(entries, device), fps=1000, ack_timeout=0.5)

    @staticmethod
    def _mood_writes(device: AckDevice, command: int) -> list[list[int]]:
        out = []
        for frame in device.writes:
            if frame[3] != command:
                continue
            length = (frame[4] << 8) | frame[5]
            out.append(list(frame[6:6 + length]))
        return out

    def test_mood_state_parses_and_caches_array_index(self) -> None:
        dev = AckDevice()
        sink = self._sink(dev)
        sink.open()

        state = sink.mood_state()
        assert state["light_array_index"] == 0x20
        assert state["on"] is True
        assert sink._mood_light_array_index == 0x20

    def test_set_mood_light_writes_payload_three_times(self) -> None:
        dev = AckDevice()
        sink = self._sink(dev)
        sink.open()
        assert sink.set_mood_light(0x66, 0xAF, 0xFF) is True

        writes = self._mood_writes(dev, px.CMD_MOOD_SET)
        assert len(writes) == px.MOOD_WRITE_REPEAT == 3
        assert writes[0] == [0x20, 0x01, 0x66, 0xAF, 0xFF, 0x64, 0xFF]
        assert writes[0] == writes[2]

    def test_set_mood_effect_matches_vendor_capture(self) -> None:
        """Each effect owns a light zone; zone + code come from the capture."""
        dev = AckDevice()
        sink = self._sink(dev)
        sink.open()
        assert sink.set_mood_effect("flow") is True

        writes = self._mood_writes(dev, px.CMD_MOOD_SET)
        assert writes[-1] == px.MOOD_VENDOR_PAYLOADS["flow"]
        assert writes[-1] == [0x20, 0x05, 0x01, 0x00, 0x00, 0x64, 0xFF]

    def test_set_mood_effect_breathing_carries_colour(self) -> None:
        dev = AckDevice()
        sink = self._sink(dev)
        sink.open()
        assert sink.set_mood_effect(
            "breathing", color=(28, 255, 248), speed=px.MOOD_BREATH_SPEED) is True

        assert self._mood_writes(dev, px.CMD_MOOD_SET)[-1] == [
            0x20, 0x01, 0x1C, 0xFF, 0xF8, 0x64, 0x08]

    def test_set_mood_light_uses_queried_array_index(self) -> None:
        dev = AckDevice()
        dev.mood_reply = [0x1A, 0x01, 0x01, 0x00, 0x00, 0x00, 0x64, 0x08]
        sink = self._sink(dev)
        sink.open()
        sink.mood_state()
        sink.set_mood_light(255, 0, 0, brightness=10, speed=100)

        writes = self._mood_writes(dev, px.CMD_MOOD_SET)
        assert writes[-1] == [0x1A, 0x01, 0xFF, 0x00, 0x00, 0x0A, 0x64]

    def test_switch_and_reset_use_expected_commands(self) -> None:
        dev = AckDevice()
        sink = self._sink(dev)
        sink.open()

        assert sink.set_mood_switch(False) is True
        assert sink.reset_mood_light() is True

        assert self._mood_writes(dev, px.CMD_MOOD_SET) == [
            [0x20, 0x07, 0x00, 0x00, 0x00, 0xFF, 0xFF]] * px.MOOD_WRITE_REPEAT
        assert self._mood_writes(dev, px.CMD_MOOD_RESET) == [
            [0x20, 0x01]] * px.MOOD_WRITE_REPEAT

    def test_mood_calls_return_false_when_device_absent(self) -> None:
        dev = AckDevice()
        sink = PixelBarSink(  # no devices listed: open() and reconnect both fail
            hid_module=FakeHidModule([], dev), fps=1000, ack_timeout=0.5)
        assert sink.mood_state() is None
        assert sink.set_mood_light(255, 0, 0) is False
        assert sink.set_mood_switch(True) is False
        assert sink.reset_mood_light() is False
