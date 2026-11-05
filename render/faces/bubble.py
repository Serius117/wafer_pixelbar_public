"""Bubble-style face: round eyes + white highlight, kawaii aesthetic (F.3).

The highlight (catchlight) dot gives instant "life" to the eyes.
Think EMO robot / Pixar lamp — expressive through size and highlight position.
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
    fill_circle,
    fill_rect,
    set_pixel,
)

EYE_COLOR = (70, 180, 255)
EYE_RIM = (25, 70, 110)
HIGHLIGHT = (220, 245, 255)
MOUTH_COLOR = (50, 170, 255)

EYE_CY = HEIGHT // 2 - 3
BASE_RX = 16.0
BASE_RY = 10.0
MOUTH_CY = HEIGHT - 6


@dataclass
class BubbleState:
    expression: str = "normal"
    mouth_open: float = 0.0
    is_listening: bool = False
    is_thinking: bool = False
    _blink_start: float = 0.0
    _blinking: bool = False
    _next_blink: float = field(default_factory=lambda: time.monotonic() + 4.0)
    _breath: float = 0.0
    _hl_wobble: float = 0.0  # highlight wobble phase

    # per-expression: (rx, ry, dy, hl_x, hl_y)
    # hl = highlight position relative to eye center
    _params: dict = field(default_factory=lambda: {
        "normal":    (16, 10, 0, -5, -4),
        "happy":     (14, 7, -2, -4, -3),    # squint, raised
        "sad":       (14, 9, 2, -4, -3),     # droopy, lowered
        "angry":     (15, 7, -1, 4, -3),     # narrowed, highlight right
        "surprised": (20, 14, -2, -6, -5),   # big wide
        "suspicious": (13, 6, 0, -4, -2),    # squinty
        "cute":      (18, 12, 1, -6, -5),    # big round
        "tired":     (14, 8, 2, -3, -2),     # droopy
        "sleepy":    (12, 3, 3, -3, -1),     # almost flat
        "thinking":  (15, 9, -1, 5, -4),     # highlight looking right/up
        "confused":  (16, 10, 0, -6, 2),     # highlight low-left
        "scared":    (18, 13, -3, -6, -6),   # wide, highlight high
        "upset":     (12, 6, 2, 0, -2),      # small, flat
        "glee":      (12, 6, -4, -4, -2),    # tiny, very raised
        "wonder":    (17, 11, -1, -6, -5),   # slightly big
        "skeptic":   (14, 8, 0, 5, 3),       # highlight low-right
        "wink":      (16, 10, 0, -5, -4),    # one eye closed
    })


class BubbleFace:
    """Round eyes with white highlight — kawaii style."""

    def __init__(self) -> None:
        self.state = BubbleState()

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
                s._next_blink = now + random.uniform(3.0, 7.0)

        s._breath += dt * 0.6
        s._hl_wobble += dt * 2.0

    def _draw_eye(self, pixels, cx, cy, rx, ry, hl_dx, hl_dy, blink):
        from render.faces import fill_ellipse

        if blink:
            ry = max(1.0, ry * 0.15)

        # outer rim (darker)
        fill_ellipse(pixels, cx, cy, rx + 1, ry + 1, EYE_RIM)
        # main eye
        fill_ellipse(pixels, cx, cy, rx, ry, EYE_COLOR)
        # highlight (white, 2×2 or 3×3 depending on eye size)
        hl_x = cx + hl_dx
        hl_y = cy + hl_dy
        if rx > 14:
            fill_rect(pixels, hl_x, hl_y, 3, 3, HIGHLIGHT)
        else:
            fill_rect(pixels, hl_x, hl_y, 2, 2, HIGHLIGHT)

    def render(self) -> Frame:
        s = self.state
        pixels = blank_pixels()

        params = s._params.get(s.expression, s._params["normal"])
        rx, ry, dy, hl_dx, hl_dy = params

        breath_dy = int(math.sin(s._breath) * 1.5)  # *1.0 was a no-op (VE2E-A3)
        cy = EYE_CY + dy + breath_dy
        spread = 40

        # highlight wobble (subtle life)
        hl_wobble = math.sin(s._hl_wobble) * 1.0
        hl_dx = int(hl_dx + hl_wobble)

        # wink: right eye closed
        left_blink = s._blinking
        right_blink = s._blinking or s.expression == "wink"

        # confused: left eye slightly different size
        left_rx, left_ry = rx, ry
        if s.expression == "confused":
            left_rx, left_ry = rx * 1.15, ry * 1.15

        # draw eyes
        self._draw_eye(pixels, WIDTH // 2 - spread, cy,
                       left_rx, left_ry, hl_dx, hl_dy, left_blink)
        self._draw_eye(pixels, WIDTH // 2 + spread, cy,
                       rx, ry, -hl_dx, hl_dy, right_blink)

        # mouth
        if s.expression != "sleepy":
            mw = 28
            if s.mouth_open > 0.05:
                max(2, int(s.mouth_open * 6))
                fill_circle(pixels, WIDTH // 2, MOUTH_CY, mw // 2, MOUTH_COLOR)
            else:
                if s.expression in ("happy", "glee", "cute"):
                    # smile curve
                    for i in range(mw):
                        off = int(math.sin(i / mw * math.pi) * 3)
                        set_pixel(pixels, WIDTH // 2 - mw // 2 + i,
                                  MOUTH_CY - off, MOUTH_COLOR)
                elif s.expression in ("sad", "upset"):
                    for i in range(mw):
                        off = int(math.sin(i / mw * math.pi) * 2)
                        set_pixel(pixels, WIDTH // 2 - mw // 2 + i,
                                  MOUTH_CY + off, MOUTH_COLOR)
                else:
                    fill_rect(pixels, WIDTH // 2 - mw // 2, MOUTH_CY,
                              mw, 1, MOUTH_COLOR)

        # indicators
        if s.is_listening:
            for i in range(3):
                set_pixel(pixels, 8 + i * 6, 3, EYE_COLOR)
        if s.is_thinking:
            for i in range(3):
                fill_rect(pixels, WIDTH - 24 + i * 7, 3, 3, 3, MOUTH_COLOR)

        return Frame(pixels=bytes(pixels), width=WIDTH, height=HEIGHT)
