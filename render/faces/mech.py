"""Mech-style face: square/angular eyes, industrial aesthetic (F.4).

WALL-E / Johnny-5 inspired — rectangular eyes with corner accents
and a more robotic feel. Square shapes read well on LED matrices.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Any

from core.interfaces import Frame
from render.faces import (
    HEIGHT,
    WIDTH,
    blank_pixels,
    fill_rect,
    set_pixel,
)

EYE_COLOR = (70, 180, 255)
EYE_DARK = (20, 50, 80)
ACCENT = (120, 220, 255)
MOUTH_COLOR = (50, 160, 240)

EYE_CY = HEIGHT // 2 - 3
EYE_W = 22
EYE_H = 12
MOUTH_CY = HEIGHT - 7


@dataclass
class MechState:
    expression: str = "normal"
    mouth_open: float = 0.0
    is_listening: bool = False
    is_thinking: bool = False
    _blink_start: float = 0.0
    _blinking: bool = False
    _next_blink: float = field(default_factory=lambda: time.monotonic() + 4.0)
    _scan_y: float = 0.0  # scanning line animation
    _breath: float = 0.0

    # per-expression: (w, h, dy, tilt)
    # tilt = 0 (none), 1 (left up), -1 (right up)
    _params: dict = field(default_factory=lambda: {
        "normal":    (22, 12, 0, 0),
        "happy":     (22, 8, -2, 0),      # shorter, raised = smiling squint
        "sad":       (20, 10, 2, 1),      # lowered, left tilted down
        "angry":     (24, 8, -1, -1),     # wider, flat, tilted inward
        "surprised": (26, 16, -2, 0),     # big
        "suspicious": (18, 6, 0, 1),      # narrow, tilted
        "cute":      (24, 14, 1, 0),      # round-ish square
        "tired":     (20, 8, 2, 0),       # droopy
        "sleepy":    (18, 3, 3, 0),       # almost flat
        "thinking":  (22, 11, -1, 1),     # slightly tilted
        "confused":  (22, 12, 0, 1),      # tilted
        "scared":    (26, 15, -3, 0),     # wide, raised
        "upset":     (16, 6, 2, 0),       # small, low
        "glee":      (20, 6, -4, 0),      # tiny, very raised
        "wonder":    (24, 13, -1, -1),    # big, tilted other way
        "skeptic":   (18, 8, 0, -1),      # narrow, tilted
        "wink":      (22, 12, 0, 0),      # one eye closed
    })


class MechFace:
    """Square eyes with corner accents — industrial/robotic style."""

    def __init__(self) -> None:
        self.state = MechState()

    def set_expression(self, name: str) -> None:
        if name in self.state._params:
            self.state.expression = name
        else:
            self.state.expression = "normal"

    def update(self, **signals: Any) -> None:
        s = self.state
        if "emotion" in signals:
            self.set_expression(str(signals["emotion"]))
        if "tts_amplitude" in signals:
            s.mouth_open = float(signals["tts_amplitude"])
        if "session_state" in signals:
            state = signals["session_state"]
            if state == "standby":
                s.expression = "sleepy"
                s.is_listening = False
            elif state == "listening":
                s.is_listening = True
                s.is_thinking = False
                if s.expression == "sleepy":
                    s.expression = "normal"
            elif state == "thinking":
                s.is_thinking = True
                s.is_listening = False
            else:
                s.is_listening = False
                s.is_thinking = False

    def tick(self, dt: float = 1.0 / 30.0) -> None:
        s = self.state
        now = time.monotonic()

        if not s._blinking and now >= s._next_blink and s.expression != "sleepy":
            s._blinking = True
            s._blink_start = now
        if s._blinking:
            if now - s._blink_start > 0.15:
                s._blinking = False
                s._next_blink = now + random.uniform(3.0, 6.0)

        s._breath += dt * 0.5
        s._scan_y = (s._scan_y + dt * 8.0) % HEIGHT  # scanning line

    def _draw_eye(self, pixels, cx, cy, w, h, tilt, blink):
        if blink:
            h = max(1, int(h * 0.15))

        x0 = cx - w // 2
        y0 = cy - h // 2

        # tilt: shift top row horizontally
        tilt_px = tilt * 2

        # outer border (dark)
        for yy in range(y0, y0 + h):
            xx_shift = int(tilt_px * (yy - y0) / max(h - 1, 1))
            for xx in range(x0 + xx_shift, x0 + xx_shift + w):
                set_pixel(pixels, xx, yy, EYE_DARK)

        # inner fill (bright), inset by 1
        if h > 2:
            for yy in range(y0 + 1, y0 + h - 1):
                xx_shift = int(tilt_px * (yy - y0) / max(h - 1, 1))
                for xx in range(x0 + 1 + xx_shift, x0 + xx_shift + w - 1):
                    set_pixel(pixels, xx, yy, EYE_COLOR)

        # corner accents (bright dots at corners)
        if h > 4 and w > 6:
            set_pixel(pixels, x0 + tilt_px, y0, ACCENT)
            set_pixel(pixels, x0 + w - 1 + tilt_px, y0, ACCENT)
            set_pixel(pixels, x0, y0 + h - 1, ACCENT)
            set_pixel(pixels, x0 + w - 1, y0 + h - 1, ACCENT)

    def render(self) -> Frame:
        s = self.state
        pixels = blank_pixels()

        params = s._params.get(s.expression, s._params["normal"])
        w, h, dy, tilt = params

        breath_dy = int(math.sin(s._breath) * 1.5)  # *1.0 was a no-op (VE2E-A3)
        cy = EYE_CY + dy + breath_dy
        spread = 42

        # wink: right eye closed
        left_blink = s._blinking
        right_blink = s._blinking or s.expression == "wink"

        # draw eyes
        self._draw_eye(pixels, WIDTH // 2 - spread, cy, w, h, tilt, left_blink)
        self._draw_eye(pixels, WIDTH // 2 + spread, cy, w, h, -tilt, right_blink)

        # scanning line (subtle horizontal line moving across screen)
        scan_y = int(s._scan_y)
        for xx in range(4, WIDTH - 4, 3):
            set_pixel(pixels, xx, scan_y, (15, 30, 50))

        # mouth (rectangular, mechanical)
        if s.expression != "sleepy":
            mw = 28
            if s.mouth_open > 0.05:
                mh = max(2, int(s.mouth_open * 6))
                # hollow rectangle mouth (open)
                fill_rect(pixels, WIDTH // 2 - mw // 2, MOUTH_CY - mh // 2,
                          mw, mh, MOUTH_COLOR)
                fill_rect(pixels, WIDTH // 2 - mw // 2 + 2, MOUTH_CY - mh // 2 + 1,
                          mw - 4, max(1, mh - 2), (0, 0, 0))
            else:
                # line with small notches
                fill_rect(pixels, WIDTH // 2 - mw // 2, MOUTH_CY, mw, 2, MOUTH_COLOR)
                # corner notches
                set_pixel(pixels, WIDTH // 2 - mw // 2 - 2, MOUTH_CY, MOUTH_COLOR)
                set_pixel(pixels, WIDTH // 2 + mw // 2 + 1, MOUTH_CY, MOUTH_COLOR)

        # listening indicator (vertical bars)
        if s.is_listening:
            for i in range(3):
                fill_rect(pixels, 6 + i * 5, 2, 2, 5, EYE_COLOR)

        # thinking indicator (horizontal dashes)
        if s.is_thinking:
            for i in range(3):
                fill_rect(pixels, WIDTH - 22 + i * 6, 3, 4, 2, MOUTH_COLOR)

        return Frame(pixels=bytes(pixels), width=WIDTH, height=HEIGHT)
