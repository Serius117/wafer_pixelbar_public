"""RenderEventBridge: bus events -> face signals (W.6, the missing wire).

Before this module the pipeline published llm.emotion / session.state /
render.command / tts.amplitude and NOTHING consumed them -- the face never
moved. This bridge subscribes on the render process's bus connection and
translates events into ClawFace/SimpleFace `update(**signals)` calls.

Command overlay (render.command):
    question -> confused expression, duration_ms (default 3000)
    error    -> scared expression, duration_ms (default 3000)
    scene    -> scene switch: game_hud -> typing + status text, idle -> normal

Overlay expiry: `apply_timeouts()` is called by the render loop each frame;
after the duration the expression falls back to the pre-overlay expression.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from core.bus import topics
from core.bus.hub import InProcBus
from render.assets import AssetLibrary

logger = logging.getLogger(__name__)

# render.command cmd -> (expression, status_text)
COMMAND_EXPRESSIONS: dict[str, tuple[str, str]] = {
    "question": ("confused", "?"),
    "error": ("scared", "!"),
}

SCENE_EXPRESSIONS: dict[str, tuple[str, str]] = {
    "game_hud": ("typing", "GAME"),
    "idle": ("normal", ""),
}

DEFAULT_IMAGE_FPS = 10  # GIF playback rate on the LED

# tts.amplitude arrives at up to 50Hz from the audio tap; the face only
# needs ~30Hz (HUB-09) -- drop the extras at the consumer (design doc §2.3).
AMPLITUDE_MIN_INTERVAL = 1.0 / 30.0


class RenderEventBridge:
    """Subscribes to bus events and drives a ClawFace (or any face with
    update(**signals) / set_expression)."""

    def __init__(self, bus: InProcBus, face: Any,
                 assets: AssetLibrary | None = None) -> None:
        self._bus = bus
        self._face = face
        self._assets = assets or AssetLibrary()
        self._override_expression: str | None = None
        self._override_until: float = 0.0
        self._scene = "idle"
        self._scene_status = ""
        self._last_emotion: str | None = None
        self._last_amp_update = 0.0
        self._amp_updates = 0  # observability (HUB-09 tests)
        # L2 image overlay (T6.10): highest display priority
        self._image_name: str | None = None
        self._image_until: float = 0.0
        self._image_started: float = 0.0

    @property
    def scene(self) -> str:
        return self._scene

    @property
    def override_active(self) -> bool:
        return self._override_expression is not None and time.monotonic() < (
            self._override_until
        )

    async def start(self) -> None:
        """Register all subscriptions (call once after bus connect)."""
        self._bus.subscribe(topics.SESSION_STATE, self._on_session_state)
        self._bus.subscribe(topics.TTS_AMPLITUDE, self._on_amplitude)
        self._bus.subscribe(topics.LLM_EMOTION, self._on_emotion)
        self._bus.subscribe(topics.RENDER_COMMAND, self._on_command)
        # T4.7/GAME-10: task results show on the LED status area
        self._bus.subscribe(topics.TASK_RESULT, self._on_task_result)
        logger.info("RenderEventBridge subscribed (state/amplitude/emotion/command/result)")

    # --- event handlers ----------------------------------------------------

    def _on_session_state(self, topic: str, payload: dict) -> None:
        self._face.update(session_state=payload.get("state", "standby"))

    def _on_amplitude(self, topic: str, payload: dict) -> None:
        now = time.monotonic()
        if now - self._last_amp_update < AMPLITUDE_MIN_INTERVAL:
            return  # throttled (HUB-09)
        self._last_amp_update = now
        self._amp_updates += 1
        self._face.update(tts_amplitude=payload.get("rms", 0.0))

    def _on_task_result(self, topic: str, payload: dict) -> None:
        """T4.7: finished-task summary shows in the LED status area.
        T6.10: plays the celebrate GIF when the asset exists (L2 easter egg)."""
        summary = str(payload.get("summary", ""))[:32]
        if summary:
            self._face.update(status_text=summary)
        if self._assets.frames("celebrate"):
            self._image_name = "celebrate"
            self._image_started = time.monotonic()
            self._image_until = self._image_started + 2.5

    def _on_emotion(self, topic: str, payload: dict) -> None:
        tag = payload.get("tag")
        if not tag:
            return
        self._last_emotion = tag
        # Emotion from the LLM only applies outside overrides and scenes
        if not self.override_active and self._scene == "idle":
            self._face.update(emotion=tag)

    def _on_command(self, topic: str, payload: dict) -> None:
        cmd = payload.get("cmd")
        if cmd in COMMAND_EXPRESSIONS:
            expression, status = COMMAND_EXPRESSIONS[cmd]
            duration_ms = payload.get("duration_ms", 3000)
            self._override_expression = expression
            self._override_until = time.monotonic() + duration_ms / 1000.0
            self._face.set_expression(expression)
            self._face.update(status_text=status)
        elif cmd == "scene":
            scene = payload.get("scene", "idle")
            self._set_scene(scene)
        elif cmd == "image":
            # T6.10 (L2): show a named asset (PNG/GIF) for a duration
            name = str(payload.get("asset", ""))
            duration_ms = payload.get("duration_ms", 3000)
            if name and self._assets.frames(name):
                self._image_name = name
                self._image_started = time.monotonic()
                self._image_until = self._image_started + duration_ms / 1000.0
            else:
                logger.warning("image command for unknown asset %r", name)
        else:
            logger.warning("unknown render.command: %s", cmd)

    # --- scene & overlay management -----------------------------------------

    def _set_scene(self, scene: str) -> None:
        if scene not in SCENE_EXPRESSIONS:
            logger.warning("unknown scene: %s", scene)
            return
        self._scene = scene
        expression, status = SCENE_EXPRESSIONS[scene]
        self._scene_status = status
        self._face.set_expression(expression)
        self._face.update(status_text=status)

    def apply_timeouts(self) -> None:
        """Call once per rendered frame: expire finished overlays.

        After an overlay expires the face falls back to the last LLM emotion
        (or normal), preserving the scene status text.
        """
        if self._override_expression is not None and time.monotonic() >= (
            self._override_until
        ):
            self._override_expression = None
            # Restore: scene expression first, else last emotion, else normal
            if self._scene != "idle":
                expression, _ = SCENE_EXPRESSIONS[self._scene]
                self._face.set_expression(expression)
            elif self._last_emotion:
                self._face.update(emotion=self._last_emotion)
            else:
                self._face.set_expression("normal")

    # --- L2 image overlay (T6.10) -------------------------------------------

    @property
    def image_active(self) -> bool:
        return self._image_name is not None and time.monotonic() < self._image_until

    def image_frame(self):
        """Current image frame, or None (render the face instead).

        GIFs play at DEFAULT_IMAGE_FPS; a still PNG is one frame.
        """
        if not self.image_active:
            if self._image_name is not None:
                self._image_name = None  # expired
            return None
        frames = self._assets.frames(self._image_name)
        if not frames:
            self._image_name = None
            return None
        elapsed = time.monotonic() - self._image_started
        idx = int(elapsed * DEFAULT_IMAGE_FPS) % len(frames)
        return frames[idx]
