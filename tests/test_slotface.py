"""TEST.SLOT.01~12: ExpressionSlotController (TASK.SLOT.02) unit tests.

Drives the controller with fake bus events and asserts category switches
via a FakeSlotSink. No hardware needed. These mirror the TEST.POLICY
scenarios (display_policy_model.py) but test the real controller code.

Key design fact (user observation, 2026-10-26): category switching is
fast (~2ms single command); option switching within a category requires
re-upload (slow). So each expression lives in its OWN category.
"""
from __future__ import annotations

import asyncio

import pytest

from render.slotface import (
    CAT_ANGRY,
    CAT_CELEBRATE,
    CAT_CLOCK,
    CAT_GAME,
    CAT_HAPPY,
    CAT_NORMAL,
    CAT_SAD,
    CAT_THINKING,
    ExpressionSlotController,
    FakeSlotSink,
)


class TestSlotController:
    @pytest.fixture
    def ctrl(self):
        from core.bus.hub import InProcBus

        bus = InProcBus()
        sink = FakeSlotSink()
        c = ExpressionSlotController(bus, sink=sink)
        return c, bus, sink

    async def _publish(self, bus, topic, payload):
        await bus.publish(topic, payload)
        await asyncio.sleep(0.05)

    def _last_cat(self, sink) -> int:
        assert sink.last is not None
        return sink.last

    async def test_01_emotion_happy(self, ctrl):
        """TEST.SLOT.01: llm.emotion{happy} → category 3."""
        c, bus, sink = ctrl
        await c.start()
        await self._publish(bus, "llm.emotion", {"tag": "happy"})
        assert self._last_cat(sink) == CAT_HAPPY

    async def test_02_emotion_sad(self, ctrl):
        """TEST.SLOT.02: llm.emotion{sad} → category 4."""
        c, bus, sink = ctrl
        await c.start()
        await self._publish(bus, "llm.emotion", {"tag": "sad"})
        assert self._last_cat(sink) == CAT_SAD

    async def test_03_emotion_angry(self, ctrl):
        """TEST.SLOT.03: llm.emotion{angry} → category 5."""
        c, bus, sink = ctrl
        await c.start()
        await self._publish(bus, "llm.emotion", {"tag": "angry"})
        assert self._last_cat(sink) == CAT_ANGRY

    async def test_04_task_complete(self, ctrl):
        """TEST.SLOT.04: task.result{ok:true} → category 7 (celebrate)."""
        c, bus, sink = ctrl
        await c.start()
        await self._publish(bus, "task.result",
                            {"task_id": "t1", "ok": True})
        assert self._last_cat(sink) == CAT_CELEBRATE

    async def test_05_task_failure(self, ctrl):
        """TEST.SLOT.05: task.result{ok:false} → category 4 (sad)."""
        c, bus, sink = ctrl
        await c.start()
        await self._publish(bus, "task.result",
                            {"task_id": "t1", "ok": False, "error": "rc=1"})
        assert self._last_cat(sink) == CAT_SAD

    async def test_06_listening(self, ctrl):
        """TEST.SLOT.06: session.listening → category 2 (normal)."""
        c, bus, sink = ctrl
        await c.start()
        await self._publish(bus, "session.state", {"state": "listening"})
        assert self._last_cat(sink) == CAT_NORMAL

    async def test_07_standby_restores_clock(self, ctrl):
        """TEST.SLOT.07: session.standby → native clock (category 0)."""
        c, bus, sink = ctrl
        await c.start()
        await self._publish(bus, "session.state", {"state": "standby"})
        assert c._restored is True

    async def test_08_cancel(self, ctrl):
        """TEST.SLOT.08: task.cancel → category 2 (normal)."""
        c, bus, sink = ctrl
        await c.start()
        await self._publish(bus, "task.cancel",
                            {"task_id": "t1", "reason": "user"})
        assert self._last_cat(sink) == CAT_NORMAL

    async def test_09_unknown_emotion_fallback(self, ctrl):
        """TEST.SLOT.09: unknown emotion tag → category 2 (normal)."""
        c, bus, sink = ctrl
        await c.start()
        await self._publish(bus, "llm.emotion", {"tag": "bogus"})
        assert self._last_cat(sink) == CAT_NORMAL

    async def test_10_game_mode(self, ctrl):
        """TEST.SLOT.10: mode→game → category 1 (native game scene)."""
        c, bus, sink = ctrl
        await c.start()
        await self._publish(bus, "mode.switch", {"mode": "game"})
        assert self._last_cat(sink) == CAT_GAME

    async def test_11_game_exit_restores_clock(self, ctrl):
        """TEST.SLOT.11: mode→normal → immediate clock restore."""
        c, bus, sink = ctrl
        await c.start()
        await self._publish(bus, "mode.switch", {"mode": "game"})
        await self._publish(bus, "mode.switch", {"mode": "normal"})
        assert c._restored is True

    async def test_12_degrade_angry(self, ctrl):
        """TEST.SLOT.12: render.command{error} → category 5 (angry) 5s."""
        c, bus, sink = ctrl
        await c.start()
        await self._publish(bus, "render.command", {"cmd": "error"})
        assert self._last_cat(sink) == CAT_ANGRY
        # simulate 5s timeout
        c._on_timeout()
        assert c._restored is True

    async def test_22_dispatch_confirm_happy(self, ctrl):
        """TEST.SLOT.22 (D61): task.request -> happy (确认→happy)."""
        c, bus, sink = ctrl
        await c.start()
        await self._publish(bus, "task.request",
                            {"op": "execute",
                             "task": {"task_id": "t1", "prompt": "查测试"}})
        assert self._last_cat(sink) == CAT_HAPPY

    async def test_23_executing_back_to_clock(self, ctrl):
        """TEST.SLOT.23 (裁决②): task.progress running -> native clock."""
        c, bus, sink = ctrl
        await c.start()
        await self._publish(bus, "task.request",
                            {"op": "execute",
                             "task": {"task_id": "t1", "prompt": "查"}})
        await self._publish(bus, "task.progress",
                            {"task_id": "t1", "phase": "running",
                             "detail": "opencode"})
        assert self._last_cat(sink) == CAT_CLOCK

    async def test_24_permission_thinking(self, ctrl):
        """TEST.SLOT.24 (D43): task.permission -> thinking (待决策)."""
        c, bus, sink = ctrl
        await c.start()
        await self._publish(bus, "task.progress",
                            {"task_id": "t1", "phase": "running"})
        await self._publish(bus, "task.permission",
                            {"task_id": "t1", "kind": "edit",
                             "target": "a.py"})
        assert self._last_cat(sink) == CAT_THINKING

    async def test_26_permission_survives_listening(self, ctrl):
        """TEST.SLOT.26: session.listening must not erase permission face."""
        c, bus, sink = ctrl
        await c.start()
        await self._publish(bus, "task.request",
                            {"op": "execute",
                             "task": {"task_id": "t1", "prompt": "x"}})
        await self._publish(bus, "task.permission",
                            {"task_id": "t1", "kind": "edit",
                             "target": "a.py"})
        await self._publish(bus, "session.state", {"state": "listening"})
        assert self._last_cat(sink) == CAT_THINKING
        assert c._permission_pending is True

    async def test_27_approve_switches_to_executing(self, ctrl):
        """TEST.SLOT.27: approval ends decision, returns to clock/working."""
        c, bus, sink = ctrl
        await c.start()
        await self._publish(bus, "task.request",
                            {"op": "execute",
                             "task": {"task_id": "t1", "prompt": "x"}})
        await self._publish(bus, "task.permission",
                            {"task_id": "t1", "kind": "edit"})
        await self._publish(bus, "task.approve",
                            {"task_id": "current", "allow": True})
        assert self._last_cat(sink) == CAT_CLOCK
        assert c._permission_pending is False

    async def test_28_stale_result_does_not_override_new_task(self, ctrl):
        """TEST.SLOT.28: old task.result must not turn the new task into stars."""
        c, bus, sink = ctrl
        await c.start()
        await self._publish(bus, "task.request",
                            {"op": "execute",
                             "task": {"task_id": "t-new", "prompt": "new"}})
        await self._publish(bus, "task.result",
                            {"task_id": "t-old", "ok": True})
        assert self._last_cat(sink) == CAT_HAPPY

    async def test_29_result_face_survives_listening(self, ctrl):
        """TEST.SLOT.29: celebrate stays while the result is being spoken."""
        c, bus, sink = ctrl
        await c.start()
        await self._publish(bus, "task.result",
                            {"task_id": "t1", "ok": True})
        await self._publish(bus, "session.state", {"state": "listening"})
        await self._publish(bus, "session.state", {"state": "standby"})
        assert self._last_cat(sink) == CAT_CELEBRATE
        assert c._result_hold_until > 0

    async def test_25_clarify_thinking(self, ctrl):
        """TEST.SLOT.25 (D43): task.clarify -> thinking (待用户参与)."""
        c, bus, sink = ctrl
        await c.start()
        await self._publish(bus, "task.clarify",
                            {"spoken": "音", "candidates": ["音乐"]})
        assert self._last_cat(sink) == CAT_THINKING

    def test_20_sink_report_bytes(self):
        """TEST.SLOT.20: real sink builds exact 0xEF switch reports.

        Protocol shape (verified on device, PIXELBAR_GUIDE §12):
            [CRC 3B] EF 00 09 02 FF FF FF 00 01 <cat> FF FF [end]
                     ^cmd ^len=9 payload ^
        Locks the byte layout so protocol drift is caught in CI.
        """
        from render.slotface import _PixelBarSlotSink

        class RecordingSink(_PixelBarSlotSink):
            def __init__(self) -> None:
                super().__init__()
                self.reports: list[bytes] = []

            def _write(self, report: bytes) -> None:
                self.reports.append(bytes(report))

        prefix = bytes.fromhex("02ffffff0001")
        sink = RecordingSink()
        for cat in (0, 2, 3, 4, 5, 7):
            sink.switch_category(cat)
        assert len(sink.reports) == 6
        for cat, rep in zip((0, 2, 3, 4, 5, 7), sink.reports, strict=True):
            idx = rep.find(prefix)
            assert idx >= 0, f"switch payload not found for cat {cat}"
            assert rep[idx - 3] == 0xEF   # command byte
            assert rep[idx - 2:idx] == b"\x00\x09"   # length = 9
            assert rep[idx + 6] == cat    # category byte
            assert rep[idx + 7:idx + 9] == b"\xff\xff"  # trailing
