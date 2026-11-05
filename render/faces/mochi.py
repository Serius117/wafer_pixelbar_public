"""Mochi-style face: round dot eyes, emoji aesthetic (F.2).

Expressions driven by: eye size (wide/squint), vertical position
(raised/lowered), and horizontal spacing (spread/together).
The simplest style — maximum readability at LED resolution.
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

EYE_COLOR = (80, 200, 255)
EYE_GLOW = (30, 80, 120)
MOUTH_COLOR = (60, 180, 255)

# base geometry
EYE_CY = HEIGHT // 2 - 2
EYE_L_CX = WIDTH // 2 - 35
EYE_R_CX = WIDTH // 2 + 35
MOUTH_CY = HEIGHT - 7
BASE_RADIUS = 7.0


@dataclass
class MochiState:
    expression: str = "normal"
    mouth_open: float = 0.0
    is_listening: bool = False
    is_thinking: bool = False
    _blink_start: float = 0.0
    _blinking: bool = False
    _next_blink: float = field(default_factory=lambda: time.monotonic() + 3.0)
    _breath_phase: float = 0.0

    # per-expression eye params: (radius, dy, dx_spread)
    _params: dict = field(default_factory=lambda: {
        "normal":    (7.0, 0, 35),
        "happy":     (5.0, -3, 30),   # smaller, raised, closer
        "sad":       (6.0, 3, 35),    # slightly lower
        "angry":     (5.5, -1, 28),   # narrower, closer
        "surprised": (10.0, -2, 42),  # big, spread
        "suspicious": (4.5, 0, 30),   # small squint
        "cute":      (9.0, 1, 32),    # big, slightly lower
        "tired":     (5.0, 2, 35),    # droopy
        "sleepy":    (2.0, 3, 35),   # almost closed
        "thinking":  (6.0, -1, 35),   # slightly raised
        "confused":  (7.0, -1, 40),   # one eye slightly different
        "scared":    (9.0, -3, 45),   # wide, spread, raised
        "upset":     (4.0, 2, 30),    # small, low
        "glee":      (4.0, -4, 28),   # tiny, very raised (^^^)
        "wonder":    (8.0, -2, 38),   # big, slightly spread
        "skeptic":   (5.0, 0, 32),    # one eye raised
        "wink":      (7.0, 0, 35),    # one eye closed
    })


class MochiFace:
    """Round dot eyes — simple, cute, emoji style."""

    def __init__(self) -> None:
        self.state = MochiState()

    def set_expression(self, name: str) -> None:
        if name in self.state._params or name == "talking":
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
        if "status_text" in signals:
            pass  # mochi style ignores status text for simplicity

    def tick(self, dt: float = 1.0 / 30.0) -> None:
        s = self.state
        now = time.monotonic()

        # blink
        if not s._blinking and now >= s._next_blink and s.expression != "sleepy":
            s._blinking = True
            s._blink_start = now
        if s._blinking:
            elapsed = now - s._blink_start
            if elapsed > 0.2:
                s._blinking = False
                s._next_blink = now + random.uniform(3.0, 6.0)

        # breathing
        s._breath_phase += dt * 0.8

    def render(self) -> Frame:
        s = self.state
        pixels = blank_pixels()

        params = s._params.get(s.expression, s._params["normal"])
        radius, dy, spread = params

        # breathing offset
        breath = int(math.sin(s._breath_phase) * 1.5)  # *1.0 was a no-op (VE2E-A3)

        # blink scale
        blink_scale = 0.15 if s._blinking else 1.0
        if s.expression == "sleepy":
            blink_scale = 0.15

        eye_r = max(1.0, radius * blink_scale)
        cy = EYE_CY + dy + breath

        # wink: right eye closed
        left_r = eye_r
        right_r = 0.5 if s.expression == "wink" else eye_r

        # confused: left eye slightly bigger
        if s.expression == "confused":
            left_r = eye_r * 1.2

        # draw eyes (glow ring + solid center)
        for cx, r in [(EYE_L_CX, left_r), (WIDTH - EYE_L_CX, right_r)]:
            actual_cx = WIDTH // 2 - spread if cx == EYE_L_CX else WIDTH // 2 + spread
            if r > 0.8:
                fill_circle(pixels, actual_cx, cy, r + 1, EYE_GLOW)
                fill_circle(pixels, actual_cx, cy, r, EYE_COLOR)

        # mouth (simple line or open circle)
        if s.expression != "sleepy":
            mw = 24
            if s.mouth_open > 0.05:
                mh = max(2, int(s.mouth_open * 6))
                fill_rect(pixels, WIDTH // 2 - mw // 2, MOUTH_CY - mh // 2,
                          mw, mh, MOUTH_COLOR)
            else:
                fill_rect(pixels, WIDTH // 2 - mw // 2, MOUTH_CY, mw, 1, MOUTH_COLOR)
                # happy: curved mouth (arc approximation)
                if s.expression in ("happy", "glee", "cute"):
                    for i in range(mw):
                        offset = int(math.sin(i / mw * math.pi) * 3)
                        set_pixel(pixels, WIDTH // 2 - mw // 2 + i,
                                  MOUTH_CY - offset, MOUTH_COLOR)
                # sad: inverted curve
                elif s.expression in ("sad", "upset"):
                    for i in range(mw):
                        offset = int(math.sin(i / mw * math.pi) * 2)
                        set_pixel(pixels, WIDTH // 2 - mw // 2 + i,
                                  MOUTH_CY + offset, MOUTH_COLOR)

        # listening indicator (dots top-left)
        if s.is_listening:
            for i in range(3):
                set_pixel(pixels, 8 + i * 6, 3, EYE_COLOR)

        # thinking indicator (dots top-right)
        if s.is_thinking:
            for i in range(3):
                fill_rect(pixels, WIDTH - 24 + i * 7, 3, 3, 3, MOUTH_COLOR)

        return Frame(pixels=bytes(pixels), width=WIDTH, height=HEIGHT)
