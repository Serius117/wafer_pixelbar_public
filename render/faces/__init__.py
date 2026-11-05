"""Face style system: 4 switchable face renderers (F group).

All faces implement the same interface:
    update(**signals)  — receive emotion/state/amplitude signals
    tick(dt)           — advance animation
    render() -> Frame  — produce a 256×32 RGB frame
    set_expression(name) — switch expression

The FaceManager holds the active face and handles runtime switching
via the `face.switch` bus event or console action.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

WIDTH = 256
HEIGHT = 32

BG = (0, 0, 0)


def blank_pixels() -> bytearray:
    return bytearray(WIDTH * HEIGHT * 3)


def set_pixel(pixels: bytearray, x: int, y: int, color: tuple) -> None:
    if 0 <= x < WIDTH and 0 <= y < HEIGHT:
        idx = (y * WIDTH + x) * 3
        pixels[idx], pixels[idx + 1], pixels[idx + 2] = color


def fill_rect(pixels: bytearray, x: int, y: int, w: int, h: int,
              color: tuple) -> None:
    for yy in range(max(0, y), min(HEIGHT, y + h)):
        for xx in range(max(0, x), min(WIDTH, x + w)):
            set_pixel(pixels, xx, yy, color)


def fill_circle(pixels: bytearray, cx: int, cy: int, radius: float,
                color: tuple) -> None:
    r2 = radius * radius
    for dy in range(-int(radius) - 1, int(radius) + 2):
        for dx in range(-int(radius) - 1, int(radius) + 2):
            if dx * dx + dy * dy <= r2:
                set_pixel(pixels, cx + dx, cy + dy, color)


def fill_ellipse(pixels: bytearray, cx: int, cy: int, rx: float, ry: float,
                 color: tuple) -> None:
    for dy in range(-int(ry) - 1, int(ry) + 2):
        for dx in range(-int(rx) - 1, int(rx) + 2):
            if (dx * dx) / (rx * rx) + (dy * dy) / (ry * ry) <= 1.0:
                set_pixel(pixels, cx + dx, cy + dy, color)
