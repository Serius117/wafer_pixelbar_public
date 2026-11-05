"""Wafer face: clean, minimal LED expressions (F.5, user-named).

Based on mochi (round dot eyes) but simplified and refined:
  - Mouth = ONE geometric shape per expression (arc / line / circle), never layered
  - 11 expressions, each with minimal but distinct visual elements
  - Angry has eyebrow lines; surprised has a hollow-circle mouth; thinking has
    an asymmetric mouth offset

Design table (user approved 2026-10-17; happy redesigned 2026-10-24):
  normal    ●● medium dots       — straight line
  happy     ∩∩ curved arc eyes   ⌣ wide deep grin (both curved, 2026-10-24)
  sad       ●● medium dots low   ⌢ deep frown arc (corners down, deepened)
  surprised ⬤⬤ big dots          ○ hollow circle
  angry     ＞＜ shut chevrons   ≋ pursed wavy line (2026-10-24)
  sleepy    ── near-flat lines   (no mouth)
  wink      ● ─ one dot one line ⌣ upward arc
  cute      ⬤⬤ big dots low      ⌣ shallow arc
  scared    ⬤⬤ huge dots high    ○ big hollow circle
  tired     ▬▬ narrow dots low   — faint short line
  thinking  🧐 monocle (fixed)   — short line shifted left (v2: hand cut)
  celebrate ✧✧ curved-diamond    ⌣ widest laugh + 星眼脉冲5..9 + 星星雨 (v3, 2026-10-27)
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
EYE_GLOW = (25, 60, 90)
BROW_COLOR = (60, 160, 220)
MOUTH_COLOR = (60, 180, 255)

EYE_CY = HEIGHT // 2 - 3
MOUTH_CY = HEIGHT - 7
SPREAD = 36

# --- celebrate star rain (v2 2026-10-27, user feedback: cannons out) --------
# Gentle star rain falling from the top. Seamless-loop constraint: each
# star's total fall per 2.4 s loop must be an INTEGER number of screen
# heights, so speed = n * (32/2.4) px/s -- n = crossings per loop
# (1 = slow twinkle, 2 = rain, 3 = fast streak) and the star wraps to
# its start position: frame 24 == frame 0. Sway period divides 2.4 s.
# Per star: (x, y0, n, size, sway_amp, sway_period, sway_phase)
#   size 0 = 1 px dot, 1 = 3x3 plus (5 px), 2 = 5x5 plus (9 px, ✦)
_STAR_RAIN: tuple = (
    (16, 3.0, 1, 2, 1.5, 1.2, 0.0),
    (52, 18.0, 2, 1, 1.0, 2.4, 2.1),
    (78, 7.0, 3, 0, 0.0, 2.4, 0.0),
    (104, 22.0, 1, 1, 1.0, 1.2, 4.0),
    (128, 11.0, 2, 2, 1.5, 2.4, 1.0),
    (150, 1.0, 3, 0, 0.0, 2.4, 0.0),
    (176, 26.0, 1, 1, 1.0, 1.2, 3.0),
    (198, 9.0, 2, 0, 0.0, 2.4, 0.0),
    (222, 16.0, 1, 2, 1.5, 1.2, 5.0),
    (240, 5.0, 2, 1, 1.0, 2.4, 0.7),
    (38, 12.0, 2, 1, 1.0, 1.2, 1.4),
    (92, 0.0, 1, 0, 0.0, 2.4, 0.0),
    (164, 28.0, 3, 0, 0.0, 2.4, 0.0),
    (212, 20.0, 2, 1, 1.0, 2.4, 2.8),
)
_STAR_FALL = 32.0 / 2.4  # px/s per crossing (one screen height per loop)


@dataclass
class WaferState:
    expression: str = "normal"
    mouth_open: float = 0.0
    is_listening: bool = False
    is_thinking: bool = False
    _blink_start: float = 0.0
    _blinking: bool = False
    _next_blink: float = field(default_factory=lambda: time.monotonic() + 3.5)
    _breath: float = 0.0
    _anim_t: float = 0.0  # deterministic clock for parametric animations

    # per-expression: (eye_radius, eye_dy, brow, brow_tilt)
    # brow: 0=none, 1=draw angry brows
    # brow_tilt: direction of tilt (1=left up, -1=right up)
    _eyes: dict = field(default_factory=lambda: {
        "normal":    (7.0, 0, 0, 0),
        "happy":     (4.0, -4, 0, 0),     # arc eyes (radius unused)
        "sad":       (6.0, 3, 0, 0),      # medium, lowered
        "surprised": (10.0, -2, 0, 0),    # big
        "angry":     (5.0, -1, 0, 0),     # chevron eyes (brow unused)
        "sleepy":    (1.5, 3, 0, 0),      # near-flat
        "wink":      (7.0, 0, 0, 0),      # one dot one line
        "cute":      (9.0, 1, 0, 0),      # big, slightly low
        "scared":    (12.0, -3, 0, 0),    # huge, very high
        "tired":     (4.0, 2, 0, 0),      # small, droopy
        "thinking":  (6.0, -1, 0, 0),     # medium, slightly raised
        "celebrate": (7.0, -3, 0, 0),     # star eyes (radius unused)
    })

    # mouth type per expression
    # "line" | "grin" | "arc_up" | "arc_down" | "circle" | "none" | "thick" | "offset"
    _mouths: dict = field(default_factory=lambda: {
        "normal": "line",
        "happy": "grin",
        "sad": "arc_down",
        "surprised": "circle",
        "angry": "wave",
        "sleepy": "none",
        "wink": "arc_up",
        "cute": "arc_up",
        "scared": "circle",
        "tired": "line",
        "thinking": "offset",
        "celebrate": "laugh",
    })


class WaferFace:
    """Clean minimal LED face — wafer style (user-named, F.5)."""

    def __init__(self) -> None:
        self.state = WaferState()

    def set_expression(self, name: str) -> None:
        if name in self.state._eyes:
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
                s.is_thinking = False
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
                if s.expression == "sleepy":
                    s.expression = "normal"

    def tick(self, dt: float = 1.0 / 30.0) -> None:
        s = self.state
        now = time.monotonic()

        if not s._blinking and now >= s._next_blink \
                and s.expression not in ("sleepy", "angry"):
            # angry eyes are already "shut" (chevrons) -- blinking them
            # makes no sense, same as sleepy
            s._blinking = True
            s._blink_start = now
        if s._blinking:
            if now - s._blink_start > 0.18:
                s._blinking = False
                s._next_blink = now + random.uniform(3.0, 6.0)

        s._breath += dt * 0.7
        s._anim_t += dt

    def _draw_eyes(self, pixels, radius, dy, brow, brow_tilt):
        s = self.state
        # *1.5 so int() yields {-1,0,1}: *1.0 was a visual no-op (sin<1
        # always truncates to 0) -- caught by VE2E-A3 frame-diff assert
        breath_dy = int(math.sin(s._breath) * 1.5)
        cy = EYE_CY + dy + breath_dy

        blink_scale = 0.15 if s._blinking else 1.0
        if s.expression == "sleepy":
            blink_scale = 0.15

        r = max(1.0, radius * blink_scale)

        left_cx = WIDTH // 2 - SPREAD
        right_cx = WIDTH // 2 + SPREAD

        # thinking (v2 2026-10-24, user feedback): monocle eye -- left
        # eye plain dot; right eye dot inside a monocle ring whose size
        # is FIXED (never shrinks with blink -- glasses don't blink);
        # hand removed (user ruling: awkward) -- mouth only, shifted
        # left for the pondering asymmetry
        if s.expression == "thinking":
            # left eye: plain dot
            fill_circle(pixels, left_cx, cy, r + 1, EYE_GLOW)
            fill_circle(pixels, left_cx, cy, r, EYE_COLOR)
            # right eye: dot + monocle ring (nominal size, blink-proof)
            fill_circle(pixels, right_cx, cy, r + 1, EYE_GLOW)
            fill_circle(pixels, right_cx, cy, r, EYE_COLOR)
            ring_r = round(radius) + 4  # nominal radius, NOT blink-scaled
            for angle in range(0, 360, 12):
                x = right_cx + round(
                    math.cos(math.radians(angle)) * ring_r)
                y = cy + round(
                    math.sin(math.radians(angle)) * ring_r * 0.9)
                set_pixel(pixels, x, y, EYE_COLOR)
            # monocle cord: short tick below the ring
            set_pixel(pixels, right_cx, cy + ring_r, EYE_COLOR)
            set_pixel(pixels, right_cx, cy + ring_r + 1, EYE_COLOR)
            return

        # angry (2026-10-24 redesign): tightly-shut ＞＜ eyes -- inward-
        # pointing chevrons (vertex toward the nose), 2px thick; no
        # separate brows (the chevrons read angry on their own); never
        # blinks (already "shut")
        if s.expression == "angry":
            hw, hh = 7, 5
            for ecx, direction in ((left_cx, 1), (right_cx, -1)):
                vx = ecx + direction * hw  # vertex, pointing inward
                for arm in (-1, 1):
                    ax, ay = ecx - direction * hw, cy + arm * hh
                    steps = max(hw, hh) * 2
                    for i in range(steps + 1):
                        t = i / steps
                        x = round(ax + (vx - ax) * t)
                        y = round(ay + (cy - ay) * t)
                        set_pixel(pixels, x, y, EYE_COLOR)
                        set_pixel(pixels, x, y + 1, EYE_COLOR)
            return

        # celebrate (v2 2026-10-26, user request: 星眼脉冲+礼花筒):
        # curved-diamond sparkle eyes (filled astroid: |dx/a|^(2/3)+
        # |dy/a|^(2/3)<=1 -- four tips, concave edges, the classic anime
        # twinkle); size PULSATES 5..9 with period 1.2 s (two cycles per
        # 2.4 s burn loop) driven by _anim_t, NOT _breath -- breath is
        # wall-clock-drifting and would break the seamless loop; same
        # reason cy drops the breath offset here; blink (live pipeline
        # only) still collapses the stars to a flat line
        if s.expression == "celebrate":
            cy = EYE_CY + dy  # no breath offset: keep loop seamless
            for ecx in (left_cx, right_cx):
                if s._blinking:
                    for i in range(-5, 6):
                        set_pixel(pixels, ecx + i, cy, EYE_COLOR)
                        set_pixel(pixels, ecx + i, cy + 1, EYE_COLOR)
                    continue
                a = 5 + int((math.sin(
                    s._anim_t * (2.0 * math.pi / 1.2)) + 1.0) * 2.0)  # 5..9
                for dy2 in range(-a, a + 1):
                    for dx in range(-a, a + 1):
                        if ((abs(dx) / a) ** (2 / 3)
                                + (abs(dy2) / a) ** (2 / 3)) <= 1.0:
                            set_pixel(pixels, ecx + dx, cy + dy2, EYE_COLOR)
            return

        # happy (2026-10-24 redesign): classic curved eyes -- both are ∩
        # arcs (peak up in the middle), 2px thick; blink flattens them
        # into brief horizontal lines (eyes "closing")
        if s.expression == "happy":
            arc_w = 18
            depth = 0 if s._blinking else 4
            for ecx in (left_cx, right_cx):
                for i in range(arc_w):
                    t = i / (arc_w - 1)
                    off = int(math.sin(t * math.pi) * depth)
                    set_pixel(pixels, ecx - arc_w // 2 + i, cy - off,
                              EYE_COLOR)
                    set_pixel(pixels, ecx - arc_w // 2 + i, cy - off + 1,
                              EYE_COLOR)
            return

        # left eye (always a circle)
        if r > 0.8:
            fill_circle(pixels, left_cx, cy, r + 1, EYE_GLOW)
            fill_circle(pixels, left_cx, cy, r, EYE_COLOR)

        # right eye (dot or line depending on expression)
        if s.expression == "wink" and not s._blinking:
            # wink: right eye = horizontal line
            line_w = int(radius * 2)
            fill_rect(pixels, right_cx - line_w // 2, cy, line_w, 2, EYE_COLOR)
        elif s.expression == "sleepy":
            # sleepy: both eyes = flat lines
            line_w = int(radius * 3)
            fill_rect(pixels, left_cx - line_w // 2, cy, line_w, 2, EYE_COLOR)
            fill_rect(pixels, right_cx - line_w // 2, cy, line_w, 2, EYE_COLOR)
        else:
            if r > 0.8:
                fill_circle(pixels, right_cx, cy, r + 1, EYE_GLOW)
                fill_circle(pixels, right_cx, cy, r, EYE_COLOR)

        # angry eyebrows (diagonal lines above eyes)
        if brow:
            brow_dy = cy - int(radius) - 4
            for i in range(6):
                # left brow: goes from low-left to high-right (tilt inward)
                set_pixel(pixels, left_cx - 5 + i, brow_dy + int(i * brow_tilt * 0.5),
                          BROW_COLOR)
                # right brow: mirror
                set_pixel(pixels, right_cx + 5 - i, brow_dy + int(i * brow_tilt * 0.5),
                          BROW_COLOR)

    def _draw_star_rain(self, pixels) -> None:
        """Celebrate particle layer v2: star rain from the top.

        14 stars at three phase-locked speeds (n = 1/2/3 screen
        crossings per 2.4 s loop); each wraps bottom-to-top continuously
        so the rain never stops, and the integer-crossing constraint
        makes frame 24 == frame 0 (seamless burn loop).
        """
        s = self.state
        t = s._anim_t % 2.4
        for i, (x0, y0, n, size, amp, period, ph) in enumerate(_STAR_RAIN):
            color = (EYE_COLOR, BROW_COLOR, MOUTH_COLOR)[i % 3]
            y = (y0 + n * _STAR_FALL * t) % 32.0
            if amp:
                x = x0 + math.sin(
                    ph + t * (2.0 * math.pi / period)) * amp
            else:
                x = float(x0)
            px, py = round(x), round(y)
            if size == 0:
                set_pixel(pixels, px, py, color)
            elif size == 1:  # 3x3 plus
                for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
                    set_pixel(pixels, px + dx, py + dy, color)
            else:            # 5x5 plus (classic pixel star)
                for dx in range(-2, 3):
                    for dy in range(-2, 3):
                        if dx == 0 or dy == 0:
                            set_pixel(pixels, px + dx, py + dy, color)

    def _draw_mouth(self, pixels):
        s = self.state
        mouth_type = s._mouths.get(s.expression, "line")
        mw = 22  # mouth width

        if s.mouth_open > 0.05:
            # speaking: open mouth (animated by amplitude)
            mh = max(2, int(s.mouth_open * 6))
            fill_rect(pixels, WIDTH // 2 - mw // 2, MOUTH_CY - mh // 2, mw, mh,
                      MOUTH_COLOR)
            return

        if mouth_type == "none" or s.expression == "sleepy":
            return

        cx = WIDTH // 2

        if mouth_type == "wave":
            # angry (2026-10-24): pursed-lips wavy line -- 2 humps,
            # oscillates above AND below MOUTH_CY (a flat line never
            # rises above it)
            ww, humps, amp = 22, 2, 2
            for i in range(ww):
                x = cx - ww // 2 + i
                y = MOUTH_CY + round(
                    math.sin(i / (ww - 1) * math.pi * 2 * humps) * amp)
                set_pixel(pixels, x, y, MOUTH_COLOR)
                set_pixel(pixels, x, y + 1, MOUTH_COLOR)

        elif mouth_type == "grin":
            # happy (2026-10-24): wide deep smile, 2px thick -- pairs with
            # the curved arc eyes for the classic joyful face
            gw = 24
            depth = 4
            for i in range(gw):
                t = i / (gw - 1)
                off = max(1, int(math.sin(t * math.pi) * depth))
                y = MOUTH_CY + off
                set_pixel(pixels, cx - gw // 2 + i, y, MOUTH_COLOR)
                set_pixel(pixels, cx - gw // 2 + i, y + 1, MOUTH_COLOR)

        elif mouth_type == "laugh":
            # celebrate (v0 draft 2026-10-24): widest, deepest open smile
            lw = 26
            depth = 5
            for i in range(lw):
                t = i / (lw - 1)
                off = max(1, int(math.sin(t * math.pi) * depth))
                y = MOUTH_CY + off
                set_pixel(pixels, cx - lw // 2 + i, y, MOUTH_COLOR)
                set_pixel(pixels, cx - lw // 2 + i, y + 1, MOUTH_COLOR)

        elif mouth_type == "line":
            fill_rect(pixels, cx - mw // 2, MOUTH_CY, mw, 1, MOUTH_COLOR)

        elif mouth_type == "arc_up":
            # smile (U shape): corners high, middle dips DOWN
            # y increases downward on LED, so middle = MOUTH_CY + offset
            depth = 3
            for i in range(mw):
                t = i / (mw - 1)  # 0 to 1
                offset = max(1, int(math.sin(t * math.pi) * depth))
                set_pixel(pixels, cx - mw // 2 + i, MOUTH_CY + offset, MOUTH_COLOR)

        elif mouth_type == "arc_down":
            # frown (∩ shape): corners low, middle bumps UP.
            # Deepened 2026-10-24: the old 1px sag over 22px was
            # invisible on LED -- round() keeps the full 3px sag,
            # 2px thick upward (corners never touch MOUTH_CY:
            # pure-arc test invariant)
            depth = 4
            for i in range(mw):
                t = i / (mw - 1)
                offset = max(1, round(math.sin(t * math.pi) * depth))
                y = MOUTH_CY - offset
                set_pixel(pixels, cx - mw // 2 + i, y, MOUTH_COLOR)
                set_pixel(pixels, cx - mw // 2 + i, y - 1, MOUTH_COLOR)

        elif mouth_type == "circle":
            # hollow circle ("O" mouth)
            r = 4
            for angle in range(0, 360, 15):
                x = cx + int(math.cos(math.radians(angle)) * r)
                y = MOUTH_CY + int(math.sin(math.radians(angle)) * r * 0.7)
                set_pixel(pixels, x, y, MOUTH_COLOR)

        elif mouth_type == "thick":
            # short thick line (angry)
            tw = 14
            fill_rect(pixels, cx - tw // 2, MOUTH_CY, tw, 2, MOUTH_COLOR)

        elif mouth_type == "offset":
            # thinking (2026-10-24): short line shifted LEFT (the fist
            # occupies the bottom-right, mouth leans away for balance)
            tw = 12
            fill_rect(pixels, cx - tw // 2 - 6, MOUTH_CY, tw, 1, MOUTH_COLOR)

    def render(self) -> Frame:
        s = self.state
        pixels = blank_pixels()

        eye_params = s._eyes.get(s.expression, s._eyes["normal"])
        radius, dy, brow, brow_tilt = eye_params

        self._draw_eyes(pixels, radius, dy, brow, brow_tilt)
        self._draw_mouth(pixels)
        if s.expression == "celebrate":
            self._draw_star_rain(pixels)

        # listening indicator (3 dots, top-left)
        if s.is_listening:
            for i in range(3):
                set_pixel(pixels, 8 + i * 6, 3, EYE_COLOR)

        # thinking indicator (3 dashes, top-right)
        if s.is_thinking:
            for i in range(3):
                fill_rect(pixels, WIDTH - 22 + i * 6, 3, 3, 2, MOUTH_COLOR)

        return Frame(pixels=bytes(pixels), width=WIDTH, height=HEIGHT)
