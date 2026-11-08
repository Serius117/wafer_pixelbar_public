"""Whale face style: line-art blue whale for the LED (TASK.AVATAR.01).

A horizontal whale suits the 256x32 (8:1) canvas natively. Line art
(1-2px strokes) reads well at 32px height. Six expressions mapped to
whale poses + eye/mouth/spout variations.

Design inspired by the "minimal line whale" designer idiom (public
visual vocabulary); NOT a copy of any specific trademark.
"""
from __future__ import annotations

import math
from typing import Any

from core.interfaces import Frame
from render.faces import (
    HEIGHT,
    WIDTH,
    blank_pixels,
    set_pixel,
)

WHALE_COLOR = (80, 180, 255)
WHALE_GLOW = (20, 50, 80)
SPRAY_COLOR = (120, 220, 255)

# whale body geometry (centered at cx, top of body at body_top)
BODY_HALF_W = 80   # half-width of the whale body
BODY_H = 14         # body height (thickest point)
TAIL_X_OFFSET = 85  # tail starts this far from center
EYE_DX = -45        # eye position relative to center
EYE_DY = 4          # eye vertical offset from body center


class WhaleFace:
    """Line-art whale with six expressions for the 256x32 LED."""

    _expressions = ("normal", "happy", "sad", "angry", "thinking",
                    "celebrate")

    def __init__(self) -> None:
        self.state = WhaleState()

    def set_expression(self, name: str) -> None:
        if name in self._expressions:
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
                if s.expression == "sleepy":
                    s.expression = "normal"

    def tick(self, dt: float = 1.0 / 30.0) -> None:
        s = self.state
        s._breath += dt * 0.8
        s._anim_t += dt

    def render(self) -> Frame:
        s = self.state
        pixels = blank_pixels()
        cx = WIDTH // 2
        bob = int(math.sin(s._breath) * 1.5)  # gentle vertical bob
        body_top = HEIGHT // 2 - BODY_H // 2 + bob

        expr = s.expression

        # body outline (ellipse-ish, thick middle, tapered ends)
        self._draw_body(pixels, cx, body_top, expr)

        # eye (position + shape varies by expression)
        self._draw_eye(pixels, cx + EYE_DX, body_top + EYE_DY, expr, s)

        # mouth (curve or line)
        self._draw_mouth(pixels, cx + EYE_DX + 12, body_top + EYE_DY + 5,
                          expr, s)

        # spout (spray above head, only for happy/celebrate)
        if expr in ("happy", "celebrate"):
            self._draw_spout(pixels, cx + EYE_DX + 5, body_top - 2, expr)

        # tail (two curved flukes)
        self._draw_tail(pixels, cx + TAIL_X_OFFSET, body_top, expr)

        # expression-specific extras
        if expr == "thinking":
            self._draw_bubbles(pixels, cx + EYE_DX - 15, body_top - 5)
        elif expr == "celebrate":
            self._draw_stars(pixels, s)
        elif expr == "angry":
            self._draw_anger_marks(pixels, cx + EYE_DX, body_top - 4)
        elif expr == "sad":
            self._draw_tear(pixels, cx + EYE_DX, body_top + EYE_DY + 4)

        # listening indicator (top-right)
        if s.is_listening:
            for i in range(3):
                set_pixel(pixels, WIDTH - 20 + i * 5, 3, WHALE_COLOR)

        return Frame(pixels=bytes(pixels), width=WIDTH, height=HEIGHT)

    def _draw_body(self, pixels, cx, top, expr):
        """Draw the whale body outline (dome top, tapered bottom)."""
        for dx in range(-BODY_HALF_W, BODY_HALF_W + 1):
            # top curve (dome)
            t = dx / BODY_HALF_W  # -1 to 1
            dome_h = int(BODY_H * 0.6 * math.cos(t * math.pi / 2))
            set_pixel(pixels, cx + dx, top + (BODY_H // 2 - dome_h),
                      WHALE_COLOR)
            # bottom curve (flatter)
            bot_h = int(BODY_H * 0.35 * math.cos(t * math.pi / 2))
            set_pixel(pixels, cx + dx, top + BODY_H // 2 + bot_h,
                      WHALE_COLOR)
        # belly lines (2 short arcs for detail)
        for dx in range(-30, 31, 3):
            set_pixel(pixels, cx + dx, top + BODY_H // 2 - 1, WHALE_GLOW)

    def _draw_eye(self, pixels, ex, ey, expr, state):
        """Draw the eye; shape varies: dot/arc/squint/X/closed."""
        if expr == "happy":
            # curved arc eye (∩ shape, 3px)
            set_pixel(pixels, ex - 1, ey, WHALE_COLOR)
            set_pixel(pixels, ex, ey - 1, WHALE_COLOR)
            set_pixel(pixels, ex + 1, ey, WHALE_COLOR)
        elif expr == "sad":
            # droopy eye (dot + downward line)
            set_pixel(pixels, ex, ey, WHALE_COLOR)
            set_pixel(pixels, ex, ey + 1, WHALE_COLOR)
        elif expr == "angry":
            # X eye (closed/angry)
            set_pixel(pixels, ex - 1, ey - 1, WHALE_COLOR)
            set_pixel(pixels, ex + 1, ey - 1, WHALE_COLOR)
            set_pixel(pixels, ex - 1, ey + 1, WHALE_COLOR)
            set_pixel(pixels, ex + 1, ey + 1, WHALE_COLOR)
        elif expr == "thinking":
            # half-closed (line + dot above)
            set_pixel(pixels, ex - 1, ey, WHALE_COLOR)
            set_pixel(pixels, ex, ey, WHALE_COLOR)
            set_pixel(pixels, ex + 1, ey, WHALE_COLOR)
        elif expr == "celebrate":
            # star eye (plus shape)
            set_pixel(pixels, ex, ey - 1, WHALE_COLOR)
            set_pixel(pixels, ex, ey, WHALE_COLOR)
            set_pixel(pixels, ex, ey + 1, WHALE_COLOR)
            set_pixel(pixels, ex - 1, ey, WHALE_COLOR)
            set_pixel(pixels, ex + 1, ey, WHALE_COLOR)
        else:
            # normal: round dot with glow
            set_pixel(pixels, ex, ey, WHALE_GLOW)
            set_pixel(pixels, ex, ey, WHALE_COLOR)

    def _draw_mouth(self, pixels, mx, my, expr, state):
        """Draw the mouth curve."""
        if state.mouth_open > 0.05:
            # speaking: open mouth (small circle)
            mh = max(1, int(state.mouth_open * 3))
            for dx in range(-3, 4):
                set_pixel(pixels, mx + dx, my + mh // 2, WHALE_COLOR)
        elif expr == "happy":
            # wide smile (upward curve)
            for dx in range(-5, 6):
                off = int(math.sin((dx + 5) / 10 * math.pi) * 2)
                set_pixel(pixels, mx + dx, my - off, WHALE_COLOR)
        elif expr == "sad":
            # frown (downward curve)
            for dx in range(-5, 6):
                off = int(math.sin((dx + 5) / 10 * math.pi) * 2)
                set_pixel(pixels, mx + dx, my + off, WHALE_COLOR)
        elif expr == "angry":
            # wavy line (pursed)
            for dx in range(-4, 5):
                off = int(math.sin(dx * 2) * 1.5)
                set_pixel(pixels, mx + dx, my + off, WHALE_COLOR)
        else:
            # normal: short line
            for dx in range(-3, 4):
                set_pixel(pixels, mx + dx, my, WHALE_COLOR)

    def _draw_spout(self, pixels, sx, sy, expr):
        """Draw a water spout (3 vertical dots + spray)."""
        for i in range(3):
            set_pixel(pixels, sx, sy - i * 2, SPRAY_COLOR)
        # spray dots
        set_pixel(pixels, sx - 2, sy - 5, SPRAY_COLOR)
        set_pixel(pixels, sx + 2, sy - 5, SPRAY_COLOR)

    def _draw_tail(self, pixels, tx, top, expr):
        """Draw two curved tail flukes."""
        # upper fluke
        for i in range(8):
            x = tx + i
            y = top + BODY_H // 2 - int(i * 0.8)
            set_pixel(pixels, x, y, WHALE_COLOR)
        # lower fluke
        for i in range(8):
            x = tx + i
            y = top + BODY_H // 2 + int(i * 0.8)
            set_pixel(pixels, x, y, WHALE_COLOR)

    def _draw_bubbles(self, pixels, bx, by):
        """Thinking bubbles (3 dots ascending)."""
        set_pixel(pixels, bx, by, WHALE_COLOR)
        set_pixel(pixels, bx - 3, by - 4, WHALE_COLOR)
        set_pixel(pixels, bx - 5, by - 8, WHALE_COLOR)

    def _draw_stars(self, pixels, state):
        """Celebrate: twinkling stars around the whale."""
        t = state._anim_t
        positions = [(cx, cy) for cx in range(20, 236, 45)
                     for cy in (3, 27)]
        for i, (x, y) in enumerate(positions):
            phase = math.sin(t * 3 + i * 1.3)
            if phase > 0:
                set_pixel(pixels, x, y, SPRAY_COLOR)
                set_pixel(pixels, x + 1, y, SPRAY_COLOR)

    def _draw_anger_marks(self, pixels, ax, ay):
        """Angry: 3 short diagonal lines above the eye."""
        for i in range(3):
            set_pixel(pixels, ax + i, ay - 2 - i, WHALE_COLOR)
            set_pixel(pixels, ax + i + 1, ay - 2 - i, WHALE_COLOR)

    def _draw_tear(self, pixels, tx, ty):
        """Sad: a single tear drop below the eye."""
        set_pixel(pixels, tx, ty, WHALE_COLOR)
        set_pixel(pixels, tx, ty + 1, WHALE_COLOR)


from dataclasses import dataclass  # noqa: E402


@dataclass
class WhaleState:
    """Mutable state for the whale face animation."""
    expression: str = "normal"
    mouth_open: float = 0.0
    is_listening: bool = False
    is_thinking: bool = False
    _breath: float = 0.0
    _anim_t: float = 0.0
