"""Mood-light state indicator tests (D39 / R.9, MOODLED-01..09).

See docs/TEST_DESIGN.md 4.18 and design doc 4.5.2. The mapping is fixed:
idle=breathing (zone 1,淡蓝), running=flow (5), decision=shadow (6),
done=ripple (4), failed=solid red (3).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from render.sinks.pixelbar import protocol as px  # noqa: E402


class FakeMoodSink:
    """Records mood calls; `ok=False` simulates an unacknowledged write."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.lights: list[tuple] = []
        self.effects: list[tuple] = []
        self.switches: list[bool] = []
        self.resets = 0

    def set_mood_light(self, r, g, b, brightness=px.MOOD_BRIGHTNESS_MAX,
                       speed=px.MOOD_SPEED_DEFAULT,
                       index=px.MOOD_INDEX_MAIN):  # noqa: ANN001
        self.lights.append((r, g, b, brightness, speed, index))
        return self.ok

    def set_mood_effect(self, effect, brightness=px.MOOD_BRIGHTNESS_MAX,
                        color=None, speed=px.MOOD_SPEED_DEFAULT):  # noqa: ANN001
        self.effects.append((effect, brightness, color, speed))
        return self.ok

    def set_mood_switch(self, on: bool) -> bool:
        self.switches.append(on)
        return self.ok

    def reset_mood_light(self, index=px.MOOD_INDEX_MAIN) -> bool:  # noqa: ANN001
        self.resets += 1
        return self.ok


class ExplodingSink(FakeMoodSink):
    def set_mood_light(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("device gone")

    set_mood_effect = set_mood_light


def _make(min_dwell_s: float = 0.0, states=None, timeout_s: float = 60.0):
    from core.bus.hub import InProcBus
    from render.mood import MoodStateController

    bus = InProcBus()
    sink = FakeMoodSink()
    ctrl = MoodStateController(bus, sink, states=states,
                               min_dwell_s=min_dwell_s,
                               decision_timeout_s=timeout_s)
    return bus, sink, ctrl


class TestMapping:
    async def test_start_forces_idle_breathing(self) -> None:
        """MOODLED-02: boot correction -> zone 1 breathing in the idle colour."""
        bus, sink, ctrl = _make()
        await ctrl.start()
        assert ctrl.current == "idle"
        assert sink.lights[-1] == (100, 160, 220, px.MOOD_BRIGHTNESS_MAX,
                                   px.MOOD_BREATH_SPEED, px.MOOD_ZONE_BREATHING)
        ctrl.stop()

    async def test_task_progress_is_running_flow(self) -> None:
        bus, sink, ctrl = _make()
        await ctrl.start()
        await bus.publish("task.progress", {"task_id": "t1",
                                           "phase": "dispatched"})
        assert ctrl.current == "running"
        assert sink.effects[-1] == ("flow", px.MOOD_BRIGHTNESS_MAX, None,
                                    px.MOOD_SPEED_DEFAULT)
        ctrl.stop()

    async def test_permission_is_decision_shadow(self) -> None:
        bus, sink, ctrl = _make()
        await ctrl.start()
        await bus.publish("task.permission", {"task_id": "t1",
                                             "kind": "shell"})
        assert ctrl.current == "decision"
        assert sink.effects[-1][0] == "shadow"
        ctrl.stop()

    async def test_approval_returns_decision_to_running(self) -> None:
        """TASK.SLOT.27: a resolved permission leaves decision immediately."""
        bus, sink, ctrl = _make()
        await ctrl.start()
        await bus.publish("task.permission", {"task_id": "t1",
                                             "kind": "shell"})
        assert ctrl.current == "decision"
        await bus.publish("task.approve", {"task_id": "current",
                                          "allow": True})
        assert ctrl.current == "running"
        assert sink.effects[-1][0] == "flow"
        ctrl.stop()

    async def test_result_is_done_ripple(self) -> None:
        bus, sink, ctrl = _make()
        await ctrl.start()
        await bus.publish("task.result", {"task_id": "t1", "summary": "ok"})
        assert ctrl.current == "done"
        assert sink.effects[-1][0] == "ripple"
        ctrl.stop()

    @pytest.mark.parametrize("topic,payload", [
        ("task.cancel", {"task_id": "t1", "reason": "user"}),
        ("render.command", {"cmd": "error", "duration_ms": 3000}),
        ("task.result", {"task_id": "t1", "summary": "boom", "ok": False}),
        ("task.result", {"task_id": "t1", "summary": "boom",
                         "error": "timeout"}),
    ])
    async def test_failure_is_solid_red(self, topic, payload) -> None:
        """MOODLED-06: cancel / degrade / failed result -> zone 3 red."""
        bus, sink, ctrl = _make()
        await ctrl.start()
        await bus.publish(topic, payload)
        assert ctrl.current == "failed"
        assert sink.lights[-1] == (255, 60, 60, px.MOOD_BRIGHTNESS_MAX,
                                   px.MOOD_SPEED_DEFAULT, px.MOOD_ZONE_SOLID)
        ctrl.stop()

    async def test_game_mode_is_idle(self) -> None:
        """D39 user ruling: game mode keeps the default breathing look."""
        bus, sink, ctrl = _make()
        await ctrl.start()
        await bus.publish("task.progress", {"task_id": "t1", "phase": "running"})
        await bus.publish("mode.switch", {"mode": "game"})
        assert ctrl.current == "idle"
        ctrl.stop()


class TestNoiseSuppression:
    async def test_progress_burst_writes_once(self) -> None:
        """MOODLED-03: progress events map to the same state -> one write."""
        bus, sink, ctrl = _make()
        await ctrl.start()
        for _ in range(5):
            await bus.publish("task.progress", {"task_id": "t1",
                                               "phase": "tool"})
        assert [e[0] for e in sink.effects] == ["flow"]
        ctrl.stop()

    async def test_min_dwell_defers_second_change(self) -> None:
        """MOODLED-07: no state change faster than min_dwell."""
        bus, sink, ctrl = _make(min_dwell_s=0.2)
        await ctrl.start()
        await asyncio.sleep(0.25)  # let the boot correction settle
        await bus.publish("task.progress", {"task_id": "t1", "phase": "running"})
        assert ctrl.current == "running"
        await bus.publish("task.permission", {"task_id": "t1", "kind": "x"})
        assert ctrl.current == "running"  # still inside the dwell window
        await asyncio.sleep(0.25)
        assert ctrl.current == "decision"
        ctrl.stop()

    async def test_boot_correction_respects_dwell(self) -> None:
        """A burst right after boot is deferred, never dropped."""
        bus, sink, ctrl = _make(min_dwell_s=0.1)
        await ctrl.start()
        assert ctrl.current == "idle"
        await bus.publish("task.progress", {"task_id": "t1", "phase": "running"})
        assert ctrl.current == "idle"  # deferred by the boot dwell window
        await asyncio.sleep(0.15)
        assert ctrl.current == "running"
        ctrl.stop()


class TestHolds:
    async def test_done_returns_to_idle(self) -> None:
        """MOODLED-05: done holds 5s then idles (0.05s in this test)."""
        from render.mood import DEFAULT_STATES, MoodStateSpec

        states = dict(DEFAULT_STATES)
        states["done"] = MoodStateSpec(zone=px.MOOD_ZONE_RIPPLE,
                                       effect="ripple", hold_s=0.05)
        bus, sink, ctrl = _make(states=states)
        await ctrl.start()
        await bus.publish("task.result", {"task_id": "t1", "summary": "ok"})
        assert ctrl.current == "done"
        await asyncio.sleep(0.12)
        assert ctrl.current == "idle"
        ctrl.stop()

    async def test_decision_survives_but_times_out(self) -> None:
        """MOODLED-04: decision does not auto-return; 60s timeout -> idle."""
        bus, sink, ctrl = _make(timeout_s=0.1)
        await ctrl.start()
        await bus.publish("task.permission", {"task_id": "t1", "kind": "x"})
        assert ctrl.current == "decision"
        await asyncio.sleep(0.05)
        assert ctrl.current == "decision"  # no hold timer fired
        await asyncio.sleep(0.1)
        assert ctrl.current == "idle"
        ctrl.stop()


class TestLifecycle:
    async def test_stop_resets_device(self) -> None:
        """MOODLED-08: exit resets the device (state is persisted on it)."""
        bus, sink, ctrl = _make()
        await ctrl.start()
        ctrl.stop()
        assert sink.resets == 1

    async def test_sink_failure_does_not_raise(self) -> None:
        """MOODLED-09: unacknowledged write -> state tracked, no exception."""
        from core.bus.hub import InProcBus
        from render.mood import MoodStateController

        bus, sink, ctrl = _make()
        sink.ok = False
        await ctrl.start()
        await bus.publish("task.result", {"task_id": "t1", "summary": "ok"})
        assert ctrl.current == "done"

        exploding = ExplodingSink()
        ctrl2 = MoodStateController(InProcBus(), exploding, min_dwell_s=0.0)
        await ctrl2.start()  # must not raise
        ctrl2.stop()
        ctrl.stop()


class TestConfig:
    def test_shipped_config_matches_defaults(self) -> None:
        from render.mood import DEFAULT_STATES, load_mood_states

        assert load_mood_states() == DEFAULT_STATES

    def test_partial_override_merges(self, tmp_path) -> None:
        from render.mood import DEFAULT_STATES, load_mood_states

        cfg = tmp_path / "mood.yaml"
        cfg.write_text(
            "states:\n"
            "  failed:\n"
            "    color: [255, 0, 0]\n"
            "    hold_s: null\n",
            encoding="utf-8")
        states = load_mood_states(cfg)
        assert states["failed"].color == (255, 0, 0)
        assert states["failed"].hold_s is None       # explicit null wins
        assert states["failed"].zone == px.MOOD_ZONE_SOLID
        assert states["idle"] == DEFAULT_STATES["idle"]  # untouched

    def test_missing_config_falls_back(self, tmp_path) -> None:
        from render.mood import DEFAULT_STATES, load_mood_states

        assert load_mood_states(tmp_path / "nope.yaml") == DEFAULT_STATES


class FakeHidDevice:
    """hid.device double: records writes, ACKs and answers 0x6A."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.reply_payload = [0x20, 0x01,
                              0x01, 100, 160, 220, 100, 8,
                              0x07, 0x00, 0x00, 0x01, 0xFF, 0xFF]

    def open_path(self, path) -> None:  # noqa: ANN001
        pass

    def write(self, data) -> int:  # noqa: ANN001
        self.writes.append(bytes(data))
        return len(data)

    def read(self, length, timeout_ms=0):  # noqa: ANN001
        if not self.writes:
            return b"\x00" * length
        frame = bytearray(length)
        command = self.writes[-1][3]
        payload = self.reply_payload if command == px.CMD_MOOD_QUERY else [
            int(b) for b in self.writes[-1][6:13]]
        frame[0:4] = bytes([0x2F, 0xBB, px.DEVICE_TYPE, command])
        frame[4] = (len(payload) >> 8) & 0xFF
        frame[5] = len(payload) & 0xFF
        frame[6:6 + len(payload)] = bytes(payload)
        return bytes(frame)

    def close(self) -> None:
        pass


class FakeHidModule:
    def __init__(self) -> None:
        self.device_obj = FakeHidDevice()

    def enumerate(self):  # noqa: ANN201
        return [{"vendor_id": px.VID, "product_id": 0xA160,
                 "interface_number": px.HID_INTERFACE,
                 "usage_page": px.USAGE_PAGE, "path": b"fake"}]

    def device(self):  # noqa: ANN201
        return self.device_obj


class TestMoodSink:
    def test_writes_expected_golden_packets(self) -> None:
        """MOODLED-01 (wire half): the sink emits the captured payloads."""
        from render.sinks.pixelbar.mood import PixelBarMoodSink

        mod = FakeHidModule()
        sink = PixelBarMoodSink(hid_module=mod)
        sink.open()

        assert sink.mood_state()["light_array_index"] == 0x20
        assert sink.set_mood_effect("flow")
        assert sink.set_mood_light(100, 160, 220, speed=px.MOOD_BREATH_SPEED,
                                   index=px.MOOD_ZONE_BREATHING)
        assert sink.set_mood_switch(False)
        assert sink.reset_mood_light()

        assert sink.command_log == [
            (px.CMD_MOOD_SET, [0x20, 0x05, 0x01, 0x00, 0x00, 0x64, 0xFF]),
            (px.CMD_MOOD_SET, [0x20, 0x01, 100, 160, 220, 0x64, 0x08]),
            (px.CMD_MOOD_SET, [0x20, 0x07, 0x00, 0x00, 0x00, 0xFF, 0xFF]),
            (px.CMD_MOOD_RESET, [0x20, 0x01]),
        ]
        sink.close()

    def test_uses_device_reported_array_index(self) -> None:
        from render.sinks.pixelbar.mood import PixelBarMoodSink

        mod = FakeHidModule()
        mod.device_obj.reply_payload = [0x1A, 0x03,
                                        0x03, 0x00, 0x00, 0x00, 100, 255]
        sink = PixelBarMoodSink(hid_module=mod)
        sink.open()
        sink.mood_state()
        sink.set_mood_effect("flow")
        assert sink.command_log[-1][1][0] == 0x1A
        sink.close()

    def test_write_failure_degrades(self) -> None:
        from render.sinks.pixelbar.mood import PixelBarMoodSink

        class Broken(FakeHidDevice):
            def write(self, data):  # noqa: ANN001
                raise OSError("unplugged")

        class BrokenModule(FakeHidModule):
            def __init__(self) -> None:
                self.device_obj = Broken()

        sink = PixelBarMoodSink(hid_module=BrokenModule())
        sink.open()
        assert sink.set_mood_effect("flow") is False
        assert sink.mood_state() is None
        assert sink.error_count >= 1
        sink.close()
