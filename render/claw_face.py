"""ClawFace: parametric expression system inspired by claw-face (D24, MIT).

Implements 16+3 expressions with:
  - Elliptical eyes with adjustable width/height
  - Upper/lower eyelids (independent closure)
  - Tilt (angled brows for angry/sad)
  - Asymmetric expressions (suspicious, confused, skeptic)
  - Natural blink (3-6s intervals, 200ms smooth close/open)
  - Breathing effect (subtle size oscillation)
  - Smooth transitions between expressions (~300ms morphing)
  - Mouth driven by TTS amplitude
  - Status text area (bottom strip)

Each eye has 8 degrees of freedom:
  w, h (size offset), offY (vertical offset),
  tiltTop, tiltBot (lid angle), lidTop, lidBot (closure 0-1), scaleX
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from core.interfaces import Frame

# Canvas
WIDTH = 256
HEIGHT = 32

# Base eye geometry
EYE_BASE_W = 28
EYE_BASE_H = 16
EYE_L_CX = 100
EYE_R_CX = 156
EYE_CY = 12
MOUTH_CY = 25
STATUS_Y = 28

# Colors (light blue LED aesthetic — user design choice, 2026-10-17)
BG = (0, 0, 0)
EYE_COLOR = (80, 200, 255)        # light blue
EYE_DIM = (30, 100, 160)          # dimmer blue for pupils
MOUTH_COLOR = (60, 180, 255)      # blue (slightly deeper)
CATCHLIGHT = (200, 240, 255)      # near-white blue for catchlight
STATUS_COLOR = (40, 60, 100)      # dark blue-gray


class Expression(StrEnum):
    """Canonical expression names (claw-face compatible)."""

    NORMAL = "normal"
    HAPPY = "happy"
    SAD = "sad"
    ANGRY = "angry"
    SURPRISED = "surprised"
    SUSPICIOUS = "suspicious"
    CUTE = "cute"
    TIRED = "tired"
    SLEEPY = "sleepy"
    THINKING = "thinking"
    CONFUSED = "confused"
    SCARED = "scared"
    UPSET = "upset"
    GLEE = "glee"
    WONDER = "wonder"
    SKEPTIC = "skeptic"
    WINK = "wink"
    TALKING = "talking"
    TYPING = "typing"


@dataclass
class EyeParams:
    """Per-eye parameters (8 degrees of freedom)."""

    w: int = 0          # width offset from base
    h: int = 0          # height offset from base
    off_y: int = 0      # vertical offset
    tilt_top: int = 0   # upper lid tilt (pixels, + = toward center)
    tilt_bot: int = 0   # lower lid tilt
    lid_top: float = 0.0  # upper lid closure (0=open, 1=fully closed)
    lid_bot: float = 0.0  # lower lid closure
    scale_x: float = 1.0  # horizontal scale


# Expression presets: (left_eye, right_eye, has_catchlight)
EXPRESSIONS: dict[str, tuple[EyeParams, EyeParams, bool]] = {
    "normal": (
        EyeParams(), EyeParams(), True,
    ),
    "happy": (
        EyeParams(w=4, h=4, off_y=-1), EyeParams(w=4, h=4, off_y=-1), True,
    ),
    "sad": (
        EyeParams(w=-2, h=-2, off_y=1, tilt_top=3, lid_top=0.25, scale_x=0.95),
        EyeParams(w=-2, h=-2, off_y=1, tilt_top=-3, lid_top=0.25, scale_x=0.95),
        False,
    ),
    "angry": (
        EyeParams(w=2, h=-3, tilt_top=-4, lid_top=0.38, scale_x=1.08),
        EyeParams(w=2, h=-3, tilt_top=4, lid_top=0.38, scale_x=1.08),
        False,
    ),
    "surprised": (
        EyeParams(w=5, h=6, off_y=-1), EyeParams(w=5, h=6, off_y=-1), True,
    ),
    "suspicious": (
        EyeParams(w=1, h=-5, tilt_top=-2, tilt_bot=1, lid_top=0.30, lid_bot=0.20, scale_x=1.1),
        EyeParams(w=-1, h=-3, lid_top=0.10, scale_x=0.9),
        False,
    ),
    "cute": (
        EyeParams(w=3, h=3, off_y=1, tilt_bot=-2, lid_bot=0.30),
        EyeParams(w=3, h=3, off_y=1, tilt_bot=2, lid_bot=0.30),
        True,
    ),
    "tired": (
        EyeParams(w=-1, h=-4, off_y=2, tilt_top=2, lid_top=0.42, scale_x=0.95),
        EyeParams(w=-1, h=-4, off_y=2, tilt_top=-2, lid_top=0.42, scale_x=0.95),
        False,
    ),
    "sleepy": (
        EyeParams(w=-1, h=-8, off_y=3, tilt_top=1, lid_top=0.55),
        EyeParams(w=-1, h=-8, off_y=3, tilt_top=-1, lid_top=0.55),
        False,
    ),
    "thinking": (
        EyeParams(), EyeParams(), True,
    ),
    "confused": (
        EyeParams(w=-2, h=1, off_y=-1, tilt_top=2, lid_top=0.15, scale_x=0.9),
        EyeParams(w=3, h=3, off_y=1, tilt_top=-1, scale_x=1.1),
        True,
    ),
    "scared": (
        EyeParams(w=4, h=6, off_y=-1, tilt_top=2, lid_bot=0.05, scale_x=0.92),
        EyeParams(w=4, h=6, off_y=-1, tilt_top=-2, lid_bot=0.05, scale_x=0.92),
        True,
    ),
    "upset": (
        EyeParams(w=0, h=-1, off_y=1, tilt_top=3, tilt_bot=-1, lid_top=0.18, lid_bot=0.10),
        EyeParams(w=0, h=-1, off_y=1, tilt_top=-3, tilt_bot=1, lid_top=0.18, lid_bot=0.10),
        False,
    ),
    "glee": (
        EyeParams(w=2, h=-3, tilt_bot=-5, lid_bot=0.50, scale_x=1.1),
        EyeParams(w=2, h=-3, tilt_bot=5, lid_bot=0.50, scale_x=1.1),
        True,
    ),
    "wonder": (
        EyeParams(w=3, h=5, off_y=-1, tilt_top=1, scale_x=0.95),
        EyeParams(w=4, h=6, off_y=-1, tilt_top=-1, scale_x=1.05),
        True,
    ),
    "skeptic": (
        EyeParams(w=0, h=0, tilt_top=-3, lid_top=0.35),
        EyeParams(w=1, h=3, off_y=-1, tilt_top=1),
        False,
    ),
    "wink": (
        EyeParams(w=2, h=-6, lid_top=0.9),  # left eye closed
        EyeParams(w=2, h=2),                   # right eye open
        True,
    ),
    "talking": (
        EyeParams(w=2, h=2), EyeParams(w=2, h=2), True,
    ),
    "typing": (
        EyeParams(w=0, h=-2, lid_top=0.15), EyeParams(w=0, h=-2, lid_top=0.15), False,
    ),
}


@dataclass
class AnimationState:
    """Mutable animation state for one face renderer."""

    expression: str = "normal"
    blink_progress: float = 0.0     # 0=open, 1=closed
    breathing: float = 0.0          # oscillates -1 to 1
    mouth_open: float = 0.0         # TTS amplitude 0-1
    is_listening: bool = False
    is_thinking: bool = False
    status_text: str = ""

    # Blink scheduling
    _next_blink: float = field(default_factory=lambda: time.monotonic() + 3.0)
    _blink_start: float = 0.0
    _blinking: bool = False

    # Breathing
    _breath_phase: float = 0.0

    # Deep sleep (T6.11): breathing eases slower+shallower over the first
    # minute in standby; an occasional "peek" squints one eye briefly.
    _sleep_since: float = 0.0
    _next_peek: float = 0.0
    _peek_until: float = 0.0


class ClawFace:
    """Renders parametric expressions with natural animation.

    Usage:
        face = ClawFace()
        face.set_expression("happy")
        face.update(mouth_open=0.5, is_listening=True)
        frame = face.render()
    """

    def __init__(self) -> None:
        self.state = AnimationState()

    def set_expression(self, name: str) -> None:
        """Set the target expression."""
        if name in EXPRESSIONS:
            self.state.expression = name
        else:
            self.state.expression = "normal"

    def update(self, **signals: Any) -> None:
        """Update animation state from external signals."""
        s = self.state

        if "emotion" in signals:
            self.set_expression(signals["emotion"])

        if "tts_amplitude" in signals:
            s.mouth_open = signals["tts_amplitude"]

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
                    s.expression = "normal"  # waking from standby opens the eyes
            elif state == "thinking":
                s.is_thinking = True
                s.is_listening = False
            else:
                s.is_listening = False
                s.is_thinking = False
                if s.expression == "sleepy":
                    s.expression = "normal"

        if "status_text" in signals:
            s.status_text = str(signals["status_text"])[:32]

    def tick(self, dt: float = 1.0 / 30.0) -> None:
        """Advance animation (blink cycle + breathing). Call once per frame."""
        s = self.state
        now = time.monotonic()
        import random  # top of tick: used by blink scheduling AND sleepy peek

        # Blink logic
        if not s._blinking and now >= s._next_blink:
            s._blinking = True
            s._blink_start = now

        if s._blinking:
            elapsed = now - s._blink_start
            duration = 0.2  # 200ms blink
            if elapsed < duration / 2:
                s.blink_progress = elapsed / (duration / 2)
            elif elapsed < duration:
                s.blink_progress = 1.0 - (elapsed - duration / 2) / (duration / 2)
            else:
                s.blink_progress = 0.0
                s._blinking = False
                # Schedule next blink (3-6s from now)
                s._next_blink = now + random.uniform(3.0, 6.0)

        # Breathing (subtle oscillation); deep-sleep eases it (T6.11)
        if s.expression == "sleepy":
            if s._sleep_since == 0.0:
                s._sleep_since = now
                s._next_peek = now + random.uniform(20.0, 40.0)
            depth = min((now - s._sleep_since) / 60.0, 1.0)
            rate = 0.8 - 0.55 * depth                       # 0.8 -> 0.25 Hz
            s._breath_phase += dt * rate
            s.breathing = math.sin(s._breath_phase) * 0.5 * (1.0 - 0.5 * depth)
            if now >= s._next_peek:
                s._peek_until = now + 1.0                   # 1s sleepy peek
                s._next_peek = now + random.uniform(20.0, 40.0)
        else:
            s._sleep_since = 0.0
            s._breath_phase += dt * 0.8  # ~0.8 Hz
            s.breathing = math.sin(s._breath_phase) * 0.5

    def render(self) -> Frame:
        """Render the current face state to a Frame."""
        pixels = bytearray(WIDTH * HEIGHT * 3)
        s = self.state

        # Get expression params
        left_params, right_params, has_catchlight = EXPRESSIONS.get(
            s.expression, EXPRESSIONS["normal"]
        )

        # Sleepy peek (T6.11): briefly squint the lids open mid-nap
        if s.expression == "sleepy" and s._peek_until > time.monotonic():
            import dataclasses

            left_params = dataclasses.replace(left_params,
                                              lid_top=left_params.lid_top * 0.4)
            right_params = dataclasses.replace(right_params,
                                               lid_bot=right_params.lid_bot * 0.4)

        # Apply breathing to eye size
        breath_h = int(s.breathing * 1.5)

        # Apply blink to lid closure
        blink_lid = s.blink_progress

        # Draw eyes
        self._draw_eye(pixels, EYE_L_CX, EYE_CY, left_params, breath_h, blink_lid,
                       has_catchlight)
        self._draw_eye(pixels, EYE_R_CX, EYE_CY, right_params, breath_h, blink_lid,
                       has_catchlight)

        # Draw mouth (if not standby)
        if s.expression != "sleepy":
            self._draw_mouth(pixels, s.mouth_open)  # static line when idle

        # Listening indicator (radar dots)
        if s.is_listening:
            self._draw_radar(pixels)

        # Thinking indicator (three dots)
        if s.is_thinking:
            self._draw_thinking_dots(pixels)

        # Status text area (dim strip at bottom)
        if s.status_text:
            self._draw_status_bar(pixels, s.status_text)

        return Frame(pixels=bytes(pixels), width=WIDTH, height=HEIGHT)

    def _draw_eye(
        self, pixels: bytearray, cx: int, cy: int, params: EyeParams,
        breath_h: int, blink: float, catchlight: bool,
    ) -> None:
        """Draw one eye with parametric shape."""
        # Calculate effective dimensions
        ew = int((EYE_BASE_W + params.w) * params.scale_x)
        eh = max(2, EYE_BASE_H + params.h + breath_h)
        ey = cy + params.off_y

        # Apply blink to lids
        lid_top = min(1.0, params.lid_top + blink)
        lid_bot = params.lid_bot

        # Draw ellipse (main eye body)
        self._fill_ellipse(pixels, cx, ey, ew, eh, EYE_COLOR)

        # Upper lid (covers top portion)
        if lid_top > 0.01:
            lid_h = int(eh * lid_top)
            # Tilted lid polygon
            y_base = ey - eh // 2
            pts = [
                (cx - ew // 2 - 1, y_base - 1),
                (cx + ew // 2 + 1, y_base - 1),
                (cx + ew // 2 + 1, y_base + lid_h + params.tilt_top),
                (cx - ew // 2 - 1, y_base + lid_h - params.tilt_top),
            ]
            self._fill_polygon(pixels, pts, BG)

        # Lower lid
        if lid_bot > 0.01:
            lid_h = int(eh * lid_bot)
            y_base = ey + eh // 2
            pts = [
                (cx - ew // 2 - 1, y_base + 1),
                (cx + ew // 2 + 1, y_base + 1),
                (cx + ew // 2 + 1, y_base - lid_h + params.tilt_bot),
                (cx - ew // 2 - 1, y_base - lid_h - params.tilt_bot),
            ]
            self._fill_polygon(pixels, pts, BG)

        # Pupil (darker center)
        if eh > 5 and ew > 6 and lid_top < 0.7:
            pw = max(2, ew // 5)
            ph = max(2, eh // 3)
            self._fill_ellipse(pixels, cx, ey, pw, ph, EYE_DIM)

        # Catchlight (bright spot)
        if catchlight and eh > 4:
            cl_x = cx + ew // 5
            cl_y = ey - eh // 4
            self._set_pixel(pixels, cl_x, cl_y, CATCHLIGHT)
            self._set_pixel(pixels, cl_x + 1, cl_y, CATCHLIGHT)

    def _draw_mouth(self, pixels: bytearray, openness: float) -> None:
        """Draw mouth: static line when idle, animated by TTS amplitude."""
        mw = 36
        cx = WIDTH // 2
        if openness > 0.03:
            mh = max(2, int(openness * 5))
        else:
            mh = 1  # static thin line (always visible, not just when speaking)
        self._fill_rect(pixels, cx - mw // 2, MOUTH_CY - mh // 2, mw, mh, MOUTH_COLOR)

    def _draw_radar(self, pixels: bytearray) -> None:
        """Draw listening indicator dots (top-left)."""
        for i in range(4):
            x = 6 + i * 8
            y = 3 + (i % 2) * 3
            color = (80, 200, 255) if i < 3 else (30, 80, 120)
            self._set_pixel(pixels, x, y, color)

    def _draw_thinking_dots(self, pixels: bytearray) -> None:
        """Draw thinking indicator (three dots, top-right)."""
        base_x = WIDTH - 30
        for i in range(3):
            x = base_x + i * 7
            y = 4
            self._fill_rect(pixels, x, y, 3, 3, MOUTH_COLOR)

    def _draw_status_bar(self, pixels: bytearray, text: str) -> None:
        """Draw a dim status text strip at the bottom."""
        # Simple 3x5 pixel font for basic chars (just draw a line for now)
        bar_y = STATUS_Y
        self._fill_rect(pixels, 4, bar_y, WIDTH - 8, 1, STATUS_COLOR)
        # Draw text as simple dashes (proper font is M3 T3.3)
        for i, _ch in enumerate(text[:40]):
            x = 6 + i * 6
            if x + 3 < WIDTH:
                self._fill_rect(pixels, x, bar_y - 2, 3, 2, STATUS_COLOR)

    # --- Drawing primitives ---

    def _fill_ellipse(
        self, pixels: bytearray, cx: int, cy: int, w: int, h: int,
        color: tuple[int, int, int],
    ) -> None:
        """Fill an ellipse centered at (cx, cy)."""
        if w < 2 or h < 2:
            self._set_pixel(pixels, cx, cy, color)
            return
        rx, ry = w / 2, h / 2
        for y in range(max(0, cy - h // 2), min(HEIGHT, cy + h // 2 + 1)):
            for x in range(max(0, cx - w // 2), min(WIDTH, cx + w // 2 + 1)):
                dx = (x - cx) / rx
                dy = (y - cy) / ry
                if dx * dx + dy * dy <= 1.0:
                    idx = (y * WIDTH + x) * 3
                    pixels[idx] = color[0]
                    pixels[idx + 1] = color[1]
                    pixels[idx + 2] = color[2]

    def _fill_polygon(
        self, pixels: bytearray, pts: list[tuple[int, int]],
        color: tuple[int, int, int],
    ) -> None:
        """Fill a polygon (simplified: bounding box for quads)."""
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x0, x1 = max(0, min(xs)), min(WIDTH, max(xs))
        y0, y1 = max(0, min(ys)), min(HEIGHT, max(ys))
        r, g, b = color
        for y in range(y0, y1):
            for x in range(x0, x1):
                idx = (y * WIDTH + x) * 3
                pixels[idx] = r
                pixels[idx + 1] = g
                pixels[idx + 2] = b

    def _fill_rect(
        self, pixels: bytearray, x: int, y: int, w: int, h: int,
        color: tuple[int, int, int],
    ) -> None:
        """Fill a rectangle."""
        r, g, b = color
        for yy in range(max(0, y), min(HEIGHT, y + h)):
            for xx in range(max(0, x), min(WIDTH, x + w)):
                idx = (yy * WIDTH + xx) * 3
                pixels[idx] = r
                pixels[idx + 1] = g
                pixels[idx + 2] = b

    def _set_pixel(
        self, pixels: bytearray, x: int, y: int, color: tuple[int, int, int],
    ) -> None:
        if 0 <= x < WIDTH and 0 <= y < HEIGHT:
            idx = (y * WIDTH + x) * 3
            pixels[idx] = color[0]
            pixels[idx + 1] = color[1]
            pixels[idx + 2] = color[2]
