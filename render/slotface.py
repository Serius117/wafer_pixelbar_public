"""ExpressionSlotController (TASK.SLOT.02): E-scheme expression display.

Subscribes to the same bus events as TextFaceController and maps them
to PixelBar category switches via 0xEF. The RENDER_POLICY §4 display
state machine implemented in code.

Architecture (user design, 2026-10-26):
  Each expression lives in its OWN PixelBar category. Switching between
  categories is a single 0xEF command (~2ms) — no re-upload needed.
  Switching options WITHIN a category requires re-upload (slow) — so we
  never do that at runtime.

Category mapping (one expression per category):
    0 = native clock     (standby / idle timeout — no upload)
    1 = native game      (game mode — no upload)
    2 = normal           (listening / default / after cancel)
    3 = happy            (happy emotion / dispatch confirm)
    4 = sad              (sad emotion / task failure)
    5 = angry            (angry emotion / degrade 5s)
    6 = thinking         (clarify / permission / user-participation)
    7 = celebrate        (task complete — the hero moment)
    8 = spectrum         (spare)

Burn commands (one-time, per expression):
    upload_slot.py --png expr_normal.png   --category 2
    upload_slot.py --png expr_happy.png    --category 3
    upload_slot.py --png expr_sad.png      --category 4
    upload_slot.py --png expr_angry.png    --category 5
    upload_slot.py --png expr_thinking.png --category 6
    upload_slot.py --png expr_celebrate.png --category 7

This controller does NOT push text (0xE8) — that stays in
TextFaceController. In slot mode, TextFace is restricted to event
announcements only (game/degrade); in text mode (fallback), TextFace
handles everything and this controller is idle.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# Expression → PixelBar category number (one category per expression)
# Categories 0-8 are native scene types; we overwrite 2-7 with our content
CAT_CLOCK = 0
CAT_GAME = 1
CAT_NORMAL = 2
CAT_HAPPY = 3
CAT_SAD = 4
CAT_ANGRY = 5
CAT_THINKING = 6
CAT_CELEBRATE = 7

# Emotion tag → category
EMOTION_TO_CAT = {
    "normal": CAT_NORMAL,
    "happy": CAT_HAPPY,
    "sad": CAT_SAD,
    "angry": CAT_ANGRY,
}

IDLE_TIMEOUT_S = 10.0
GAME_HOLD_S = 10.0
DEGRADE_ANGRY_S = 5.0
RESULT_HOLD_S = 12.0  # keep celebrate/sad while the result is being spoken


class _PixelBarSlotSink:
    """Sends 0xEF category-switch commands to the PixelBar."""

    def __init__(self) -> None:
        self._device = None

    def open(self) -> None:
        from render.sinks.pixelbar import open_pixelbar_device

        self._device = open_pixelbar_device()

    def close(self) -> None:
        if self._device:
            self._device.close()
            self._device = None

    @property
    def is_open(self) -> bool:
        return self._device is not None

    def switch_category(self, category: int) -> None:
        """Switch to a scene category — single command, ~2ms.

        This works for both native scenes (clock=0, game=1) and our
        uploaded expressions (categories 2-7), because the switch
        command is the same regardless of what's stored in the category.
        """
        from render.sinks.pixelbar import protocol as px

        self._write(px.build_report(
            px.CMD_PIXEL_SET,
            px.payload_scene_category(255, 255, 255, category)))

    def restore_clock(self) -> None:
        """Return to the native clock scene (idle state)."""
        self.switch_category(CAT_CLOCK)

    def _write(self, report: bytes) -> None:
        if self._device is None:
            return
        self._device.write(report)


class FakeSlotSink:
    """Test double: records category switches without hardware."""

    def __init__(self) -> None:
        self.calls: list[int] = []  # category numbers
        self.is_open = True

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def switch_category(self, category: int) -> None:
        self.calls.append(category)

    def restore_clock(self) -> None:
        self.calls.append(CAT_CLOCK)

    @property
    def last(self) -> int | None:
        return self.calls[-1] if self.calls else None


class ExpressionSlotController:
    """E-scheme expression display: bus events → category switches.

    Each expression occupies one PixelBar category; switching is a single
    fast 0xEF command. Subscribes to the same events as TextFaceController;
    in slot mode, expressions carry the mood display while TextFace handles
    event announcements only.
    """

    def __init__(self, bus, sink=None, timeout_s: float = IDLE_TIMEOUT_S,
                 robot_name: str = "wafer") -> None:  # noqa: ANN001
        self._bus = bus
        self._sink = sink or _PixelBarSlotSink()
        self._timeout_s = timeout_s
        self._robot_name = robot_name
        self._timer: object | None = None
        self._current_cat: int = CAT_CLOCK
        self._mode = "normal"  # normal | game
        self._game_timer: object | None = None
        self.pushed: list[dict] = []  # for assertions / console display
        self._restored = True  # boot state: clock assumed
        self._task_id: str | None = None
        self._permission_pending = False
        self._result_hold_until = 0.0

    async def start(self) -> None:
        from core.bus import topics

        self._bus.subscribe(topics.LLM_EMOTION, self._on_emotion)
        self._bus.subscribe(topics.SESSION_STATE, self._on_session_state)
        self._bus.subscribe(topics.TASK_REQUEST, self._on_task_request)
        self._bus.subscribe(topics.TASK_PROGRESS, self._on_task_progress)
        self._bus.subscribe(topics.TASK_RESULT, self._on_task_result)
        self._bus.subscribe(topics.TASK_CANCEL, self._on_task_cancel)
        self._bus.subscribe(topics.TASK_PERMISSION, self._on_task_permission)
        self._bus.subscribe(topics.TASK_APPROVE, self._on_task_approve)
        self._bus.subscribe(topics.TASK_CLARIFY, self._on_task_clarify)
        self._bus.subscribe(topics.MODE_SWITCH, self._on_mode_switch)
        self._bus.subscribe(topics.RENDER_COMMAND, self._on_render_command)
        self._restored = True  # clock is the initial state

    def stop(self) -> None:
        self._cancel_timer()
        self._cancel_game_timer()

    # --- category switching --------------------------------------------------

    def _switch(self, category: int, reason: str) -> None:
        """Switch to an expression category and reset the idle timer."""
        self._cancel_timer()
        if category != self._current_cat:
            self._sink.switch_category(category)
            self._current_cat = category
            self._restored = False
            self.pushed.append({"cat": category, "reason": reason,
                                "t": time.monotonic()})
            logger.info("expression category %d (%s)", category, reason)

        try:
            loop = asyncio.get_running_loop()
            self._timer = loop.call_later(self._timeout_s, self._on_timeout)
        except RuntimeError:
            pass  # tests driving synchronously

    def _restore_clock(self) -> None:
        """Return to the native clock scene and clear task-scoped state."""
        self._cancel_timer()
        self._task_id = None
        self._permission_pending = False
        self._result_hold_until = 0.0
        if not self._restored:
            self._sink.restore_clock()
            self._restored = True
            self._current_cat = CAT_CLOCK
            self.pushed.append({"cat": CAT_CLOCK, "reason": "timeout",
                                "t": time.monotonic()})

    @staticmethod
    def _task_id_of(payload: dict) -> str:
        task = payload.get("task") or {}
        return str(payload.get("task_id") or task.get("task_id") or "")

    def _task_matches(self, payload: dict) -> bool:
        task_id = self._task_id_of(payload)
        return not self._task_id or not task_id or task_id == self._task_id

    def _on_timeout(self) -> None:
        now = time.monotonic()
        if self._permission_pending:
            # A pending permission has no natural timeout; hold the face.
            try:
                self._timer = asyncio.get_running_loop().call_later(
                    30.0, self._on_timeout)
            except RuntimeError:
                pass
            return
        if now < self._result_hold_until:
            try:
                self._timer = asyncio.get_running_loop().call_later(
                    max(0.1, self._result_hold_until - now), self._on_timeout)
            except RuntimeError:
                pass
            return
        self._restore_clock()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _cancel_game_timer(self) -> None:
        if self._game_timer is not None:
            self._game_timer.cancel()
            self._game_timer = None

    # --- event handlers ------------------------------------------------------

    async def _on_emotion(self, topic: str, payload: dict) -> None:
        # Task/permission/result owns the face; chat emotion must not stomp it.
        if (self._task_id or self._permission_pending
                or time.monotonic() < self._result_hold_until):
            return
        tag = str(payload.get("tag", "normal"))
        cat = EMOTION_TO_CAT.get(tag, CAT_NORMAL)
        self._switch(cat, f"emotion:{tag}")

    async def _on_session_state(self, topic: str, payload: dict) -> None:
        state = str(payload.get("state", ""))
        if state == "listening":
            # An announcement conversation ends with listening; it must not
            # erase a pending permission or a task/result face.
            if (self._task_id or self._permission_pending
                    or time.monotonic() < self._result_hold_until):
                return
            self._switch(CAT_NORMAL, "listening")
        elif state == "thinking":
            # RENDER_POLICY v2: LLM generating → hold current (not thinking)
            pass
        elif state == "standby":
            if (not self._task_id and not self._permission_pending
                    and time.monotonic() >= self._result_hold_until):
                self._restore_clock()

    async def _on_task_result(self, topic: str, payload: dict) -> None:
        if not self._task_matches(payload):
            logger.info("ignore stale task_result: %s (current=%s)",
                        self._task_id_of(payload), self._task_id)
            return
        failed = payload.get("ok") is False or bool(payload.get("error"))
        self._permission_pending = False
        self._task_id = None
        self._result_hold_until = time.monotonic() + RESULT_HOLD_S
        self._switch(CAT_SAD if failed else CAT_CELEBRATE, "task_result")

    async def _on_task_request(self, topic: str, payload: dict) -> None:
        # New task owns the display; clear any stale result/permission state.
        self._task_id = self._task_id_of(payload) or None
        self._permission_pending = False
        self._result_hold_until = 0.0
        self._switch(CAT_HAPPY, "dispatch_confirm")

    async def _on_task_progress(self, topic: str, payload: dict) -> None:
        if not self._task_matches(payload):
            return
        phase = payload.get("phase")
        if phase in ("dispatched", "running", "tool"):
            self._switch(CAT_CLOCK, f"executing:{phase}")

    async def _on_task_permission(self, topic: str, payload: dict) -> None:
        if not self._task_matches(payload):
            return
        # awaiting user decision -> thinking (D43: thinking = 待用户参与)
        self._permission_pending = True
        self._result_hold_until = 0.0
        self._switch(CAT_THINKING, "permission")

    async def _on_task_approve(self, topic: str, payload: dict) -> None:
        # The decision is over: return to the working scene immediately.
        self._permission_pending = False
        self._result_hold_until = 0.0
        if payload.get("allow") is False:
            self._task_id = None
            self._switch(CAT_NORMAL, "approval_denied")
        else:
            self._switch(CAT_CLOCK, "approved_executing")

    async def _on_task_clarify(self, topic: str, payload: dict) -> None:
        # alias-not-found clarification -> thinking (待用户参与)
        self._switch(CAT_THINKING, "clarify")

    async def _on_task_cancel(self, topic: str, payload: dict) -> None:
        if not self._task_matches(payload):
            return
        self._task_id = None
        self._permission_pending = False
        self._result_hold_until = 0.0
        self._switch(CAT_NORMAL, "cancelled")

    async def _on_mode_switch(self, topic: str, payload: dict) -> None:
        mode = payload.get("mode")
        if mode == "game":
            self._mode = "game"
            self._switch(CAT_GAME, "game_mode")
            self._cancel_game_timer()
            try:
                loop = asyncio.get_running_loop()
                self._game_timer = loop.call_later(
                    GAME_HOLD_S, self._on_game_timeout)
            except RuntimeError:
                pass
            logger.info("game scene for %.0fs", GAME_HOLD_S)
        elif mode == "normal":
            self._mode = "normal"
            self._cancel_game_timer()
            self._restore_clock()

    def _on_game_timeout(self) -> None:
        self._cancel_game_timer()
        if self._mode == "game":
            self._restore_clock()

    async def _on_render_command(self, topic: str, payload: dict) -> None:
        if payload.get("cmd") == "error":
            # degrade: angry 5s then clock (2026-10-24 ruling)
            self._switch(CAT_ANGRY, "degrade")
            self._cancel_timer()
            try:
                loop = asyncio.get_running_loop()
                self._timer = loop.call_later(
                    DEGRADE_ANGRY_S, self._on_timeout)
            except RuntimeError:
                pass
