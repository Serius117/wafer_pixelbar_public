"""TextFace: LED text-channel content vocabulary (D36, L.1).

The real PixelBar has NO real-time frame channel: 0x17 is a flash
resource upload (1.41-1.43s/frame + firmware "uploading" UI) and 0x6B
ambient light is unreliable. The ONLY real-time channel is 0xE8 text
(~2ms, max 16 chars / 48 UTF-8 bytes, whole-screen single string).

This module holds the fixed content vocabulary (rule 2 in spirit: the
LLM only emits tags; tag -> kaomoji/color mapping never passes through
the LLM). Pixel faces (wafer et al.) remain for simulator/console
preview only (design doc 4.5.1).
"""

from __future__ import annotations

import logging
import os

from core.naming import DEFAULT_ROBOT_NAME

_DEBUG = os.environ.get("WAFER_RENDER_DEBUG") == "1"
_log = logging.getLogger(__name__)

# Emotion vocabulary -- MUST stay in sync with core.pipeline.EMOTION_TAGS
# (guard test: tests/test_textface.py::test_vocab_in_sync).
# Kaomoji are ASCII-only: the device font lacks Unicode kaomoji glyphs.
# D43 (2026-10-24): surprised removed with the LLM vocabulary.
EMOTION_FACE: dict[str, tuple[str, tuple[int, int, int]]] = {
    "normal":    ("(._.)", (120, 200, 255)),  # light blue
    "happy":     ("(^_^)", (255, 200, 80)),   # warm yellow
    "sad":       ("(T_T)", (100, 150, 255)),  # blue
    "angry":     ("(>_<)", (255, 100, 100)),  # red
}
DEFAULT_EMOTION = "normal"

# Session-state content (state machine driven, not the LLM).
# ``{name}`` is replaced with the configured robot name at push time
# (rule 4: the name is config, never hardcoded -- GUARD-05).
STATE_TEXT: dict[str, tuple[str, tuple[int, int, int]]] = {
    "listening": ("{name}在听哦('o')", (120, 200, 255)),
    "thinking":  ("(?_?)", (120, 200, 255)),
}

# Event message templates (task lifecycle / degrade / game).
# TASK.SLOT.03 (2026-10-24): task_cancel/task_permission/task_clarify
# added per RENDER_POLICY §6 rulings (cancel != failure; permission
# prompt; clarify announcement). These are voice-substitute entries.
EVENT_TEXT: dict[str, tuple[str, tuple[int, int, int]]] = {
    "task_confirm":    ("收到，开工！", (120, 200, 255)),
    "task_done":       ("搞定！\\(^o^)/", (255, 200, 80)),
    "task_fail":       ("呜，下次努力", (100, 150, 255)),
    "task_cancel":     ("已取消啦", (100, 150, 255)),
    "task_permission": ("等你点头哦", (120, 200, 255)),
    "task_clarify":    ("没找到项目呀", (120, 200, 255)),
    "degrade":         ("{name}歇一下", (255, 100, 100)),
    "game_enter":      ("游戏愉快！", (120, 200, 255)),
}

# Idle fallback: the device's native clock scene (zero host cost).
CLOCK_RESTORE_COLOR = (255, 255, 255)


class TextFaceController:
    """D36: bus events -> text pushes + 10s native-clock fallback.

    Subscribes to the same events the old frame pipeline consumed and
    translates them into single text pushes (kaomoji / short messages)
    on the PixelBarTextSink. Every push re-arms the timeout; when no
    event arrives for `timeout_s` the device's native clock scene is
    restored (clears any text -- zero host cost idle state).

    The content mapping is FIXED (rule 2 in spirit): the LLM only emits
    tags, this class maps them deterministically. It never calls the
    0x17 bitmap or 0x6B ambient channels (they don't even exist on the
    text sink's surface).
    """

    def __init__(self, bus, sink, timeout_s: float = 10.0,
                 robot_name: str = DEFAULT_ROBOT_NAME) -> None:  # noqa: ANN001
        self._bus = bus
        self._sink = sink
        self._timeout_s = timeout_s
        self._robot_name = robot_name  # rule 4: config, never hardcoded
        self._timer: object | None = None
        self._restored = True  # boot state: native clock assumed
        # push records for assertions / console display
        self.pushed: list[dict] = []
        self.restore_count = 0

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Subscribe to events and force a known idle state (clock)."""
        from core.bus import topics

        self._bus.subscribe(topics.LLM_EMOTION, self._on_emotion)
        self._bus.subscribe(topics.SESSION_STATE, self._on_session_state)
        self._bus.subscribe(topics.TASK_PROGRESS, self._on_task_progress)
        self._bus.subscribe(topics.TASK_RESULT, self._on_task_result)
        self._bus.subscribe(topics.TASK_CANCEL, self._on_task_cancel)
        self._bus.subscribe(topics.TASK_PERMISSION, self._on_task_permission)
        self._bus.subscribe(topics.TASK_CLARIFY, self._on_task_clarify)
        self._bus.subscribe(topics.MODE_SWITCH, self._on_mode_switch)
        self._bus.subscribe(topics.RENDER_COMMAND, self._on_render_command)
        self._restore()  # ensure known initial state

    def stop(self) -> None:
        self._cancel_timer()

    # --- push machinery ------------------------------------------------------

    def _push(self, kind: str, text: str,
              rgb: tuple[int, int, int]) -> None:
        # rule 4: the robot name is the configured variable
        text = text.replace("{name}", self._robot_name)
        self.pushed.append({"kind": kind, "text": text, "rgb": rgb})
        if _DEBUG:
            _log.info("push %s %r rgb=%s", kind, text, rgb)
        self._restored = False
        try:
            self._sink.show_text(text, rgb)
        except ValueError:
            # vocabulary texts are pre-validated; a violation here is a
            # programming error -- log loudly, never crash the process
            _log.exception("textface content exceeds device limit: %r", text)
        self._reset_timer()

    def _reset_timer(self) -> None:
        import asyncio

        self._cancel_timer()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (tests driving sync) -- timeout disabled
        self._timer = loop.call_later(self._timeout_s, self._on_timeout)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _on_timeout(self) -> None:
        self._restore()

    def _restore(self) -> None:
        """Back to the device's native clock (the D36 idle state)."""
        self._cancel_timer()
        self._restored = True
        self.restore_count += 1
        self._sink.restore_clock(CLOCK_RESTORE_COLOR)

    # --- event handlers (all deterministic mappings) -------------------------

    async def _on_emotion(self, topic: str, payload: dict) -> None:
        tag = str(payload.get("tag", "") or DEFAULT_EMOTION)
        text, rgb = EMOTION_FACE.get(tag, EMOTION_FACE[DEFAULT_EMOTION])
        self._push(f"emotion:{tag}", text, rgb)

    async def _on_session_state(self, topic: str, payload: dict) -> None:
        state = str(payload.get("state", ""))
        if state in STATE_TEXT:  # listening / thinking only
            text, rgb = STATE_TEXT[state]
            self._push(f"state:{state}", text, rgb)
        # speaking: the emotion kaomoji (llm.emotion) already covers it;
        # task_running / standby: nothing to show

    async def _on_task_progress(self, topic: str, payload: dict) -> None:
        if payload.get("phase") == "dispatched":
            text, rgb = EVENT_TEXT["task_confirm"]
            self._push("task_confirm", text, rgb)

    async def _on_task_result(self, topic: str, payload: dict) -> None:
        if not payload.get("summary"):
            return
        # R.15: ok=False / error -> the failure message, not the celebration
        failed = payload.get("ok") is False or bool(payload.get("error"))
        key = "task_fail" if failed else "task_done"
        text, rgb = EVENT_TEXT[key]
        self._push(key, text, rgb)

    async def _on_task_cancel(self, topic: str, payload: dict) -> None:
        # TASK.SLOT.03 (2026-10-24): dedicated wording -- cancel is a
        # user action, not a failure (RENDER_POLICY §6 ruling)
        text, rgb = EVENT_TEXT["task_cancel"]
        self._push("task_cancel", text, rgb)

    async def _on_task_permission(self, topic: str, payload: dict) -> None:
        """TASK.SLOT.03: opencode needs approval -> prompt text
        (voice-substitute; pairs with D39 待决策 light)."""
        text, rgb = EVENT_TEXT["task_permission"]
        self._push("task_permission", text, rgb)

    async def _on_task_clarify(self, topic: str, payload: dict) -> None:
        """TASK.SLOT.03: alias not found -> clarification announcement
        (voice-substitute; the WaferAgent reply itself is TTS)."""
        text, rgb = EVENT_TEXT["task_clarify"]
        self._push("task_clarify", text, rgb)

    async def _on_mode_switch(self, topic: str, payload: dict) -> None:
        mode = payload.get("mode")
        if mode == "game":
            text, rgb = EVENT_TEXT["game_enter"]
            self._push("game_enter", text, rgb)
        elif mode == "normal":
            # game over: straight back to the clock, no 10s wait
            self._restore()

    async def _on_render_command(self, topic: str, payload: dict) -> None:
        if payload.get("cmd") == "error":  # rule 6 degradation
            text, rgb = EVENT_TEXT["degrade"]
            self._push("degrade", text, rgb)
