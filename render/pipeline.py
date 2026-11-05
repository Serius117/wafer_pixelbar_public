"""Minimal render pipeline: L0 parameterized face + sink abstraction.

This is the skeleton for M1 (T1.7). It draws a simple face (two eyes + mouth)
on a 256x32 canvas and pushes frames to connected sinks. Real emotion macros,
layout state machine, and dirty-rect optimization come in M3 (T3.2).

The face parameters are driven by:
  - Session state (standby=closed eyes, listening=radar, thinking=spinning)
  - TTS amplitude (mouth open/close)
  - Emotion tag (affects eye shape -- v0: just neutral)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from core.interfaces import Frame

logger = logging.getLogger(__name__)

WIDTH = 256
HEIGHT = 32

# Colors (RGB)
COLOR_BG = (0, 0, 0)
COLOR_EYE = (0, 255, 100)
COLOR_MOUTH = (0, 200, 255)
COLOR_RADAR = (255, 200, 0)


class FaceParams:
    """Current face drawing parameters (mutated by signals)."""

    def __init__(self) -> None:
        self.eye_open = 1.0        # 0=closed, 1=fully open
        self.eye_w = 24             # eye width in pixels
        self.eye_h = 12             # eye height when fully open
        self.pupil_x = 0.0          # -1=left, 0=center, 1=right
        self.mouth_open = 0.0       # 0=closed, 1=fully open
        self.mouth_w = 40
        self.is_listening = False
        self.is_thinking = False


class SimpleFace:
    """Draws a parameterized face onto a 256x32 RGB frame."""

    def __init__(self) -> None:
        self.params = FaceParams()
        self._blink_counter = 0
        self._blink_interval = 30  # frames between blinks

    def update(self, **signals: Any) -> None:
        """Update face parameters from hub signals."""
        if "session_state" in signals:
            state = signals["session_state"]
            if state == "standby":
                self.params.eye_open = 0.05
                self.params.is_listening = False
                self.params.is_thinking = False
            elif state == "listening":
                self.params.eye_open = 1.0
                self.params.is_listening = True
                self.params.is_thinking = False
            elif state == "thinking":
                self.params.eye_open = 0.7
                self.params.is_listening = False
                self.params.is_thinking = True
            else:  # speaking, task_running
                self.params.eye_open = 1.0
                self.params.is_listening = False
                self.params.is_thinking = False

        if "tts_amplitude" in signals:
            self.params.mouth_open = signals["tts_amplitude"]

        if "emotion" in signals:
            # v0: emotions don't change shape, just log
            logger.debug("Emotion: %s", signals["emotion"])

    def tick(self) -> None:
        """Advance animation (blink cycle). Called once per frame."""
        self._blink_counter += 1
        if self._blink_counter >= self._blink_interval:
            self._blink_counter = 0
            # Brief blink: eye_open dips to 0 for one frame
            # (simplified: just reset, the render uses eye_open directly)

    def render(self) -> Frame:
        """Render the current face to a Frame."""
        # Create a black canvas
        pixels = bytearray(WIDTH * HEIGHT * 3)

        p = self.params

        # Eye positions (centered, with gap)
        left_cx = WIDTH // 2 - 40
        right_cx = WIDTH // 2 + 40
        eye_cy = HEIGHT // 2 - 4

        # Draw eyes
        self._draw_eye(pixels, left_cx, eye_cy, p)
        self._draw_eye(pixels, right_cx, eye_cy, p)

        # Draw mouth
        mouth_cy = HEIGHT - 8
        if p.mouth_open > 0.05:
            mh = max(2, int(p.mouth_open * 6))
            self._fill_rect(pixels, WIDTH // 2 - p.mouth_w // 2, mouth_cy - mh // 2,
                           p.mouth_w, mh, COLOR_MOUTH)

        # Listening indicator: radar dots
        if p.is_listening:
            for i in range(4):
                x = 8 + i * 12
                y = 4 + (i % 2) * 4
                self._set_pixel(pixels, x, y, COLOR_RADAR)

        return Frame(pixels=bytes(pixels), width=WIDTH, height=HEIGHT)

    def _draw_eye(self, pixels: bytearray, cx: int, cy: int, p: FaceParams) -> None:
        """Draw one eye based on parameters."""
        eh = max(1, int(p.eye_h * p.eye_open))
        ew = p.eye_w

        if p.is_thinking:
            # Thinking: draw a spinning indicator (simplified as pulsing dot)
            self._fill_rect(pixels, cx - 4, cy - 4, 8, 8, COLOR_EYE)
            return

        # Normal eye: filled rectangle
        self._fill_rect(pixels, cx - ew // 2, cy - eh // 2, ew, eh, COLOR_EYE)

        # Pupil (darker area within the eye)
        if p.eye_open > 0.3:
            pupil_w = 6
            px = cx + int(p.pupil_x * (ew // 2 - pupil_w // 2))
            self._fill_rect(pixels, px - pupil_w // 2, cy - eh // 2 + 2,
                           pupil_w, eh - 4, (0, 100, 50))

    def _fill_rect(self, pixels: bytearray, x: int, y: int, w: int, h: int,
                   color: tuple[int, int, int]) -> None:
        """Fill a rectangle on the pixel buffer."""
        r, g, b = color
        for yy in range(max(0, y), min(HEIGHT, y + h)):
            for xx in range(max(0, x), min(WIDTH, x + w)):
                idx = (yy * WIDTH + xx) * 3
                pixels[idx] = r
                pixels[idx + 1] = g
                pixels[idx + 2] = b

    def _set_pixel(self, pixels: bytearray, x: int, y: int,
                   color: tuple[int, int, int]) -> None:
        if 0 <= x < WIDTH and 0 <= y < HEIGHT:
            idx = (y * WIDTH + x) * 3
            pixels[idx], pixels[idx + 1], pixels[idx + 2] = color


# Sink abstraction
FrameConsumer = Callable[[Frame], None]


class RenderPipeline:
    """Minimal render pipeline: face + frame consumers (sinks)."""

    def __init__(self) -> None:
        self.face = SimpleFace()
        self._sinks: list[FrameConsumer] = []
        self._frame_count = 0

    def add_sink(self, consumer: FrameConsumer) -> None:
        self._sinks.append(consumer)

    def update(self, **signals: Any) -> None:
        """Forward signals to the face."""
        self.face.update(**signals)

    def render_frame(self) -> Frame:
        """Render one frame and push to all sinks."""
        self.face.tick()
        frame = self.face.render()
        self._frame_count += 1
        for sink in self._sinks:
            try:
                sink(frame)
            except Exception:
                logger.exception("Sink error")
        return frame

    @property
    def frame_count(self) -> int:
        return self._frame_count
