"""Tiny 256x32 clock/text renderer for the console device mirror."""
from __future__ import annotations

import time

from core.interfaces import Frame
from render.faces import HEIGHT, WIDTH, blank_pixels, set_pixel

DIGITS = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    ":": ("00000", "00100", "00100", "00000", "00100", "00100", "00000"),
    " ": ("00000",) * 7,
}
GLYPHS = {
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01110"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
}


def _draw_text(pixels: bytearray, text: str, scale: int = 3,
               color: tuple[int, int, int] = (220, 240, 255)) -> None:
    glyphs = [DIGITS.get(ch, GLYPHS.get(ch, DIGITS[" "])) for ch in text]
    gap = 1
    text_w = (len(glyphs) * 5 + max(0, len(glyphs) - 1) * gap) * scale
    text_h = 7 * scale
    x0 = (WIDTH - text_w) // 2
    y0 = (HEIGHT - text_h) // 2
    for gi, glyph in enumerate(glyphs):
        gx = x0 + gi * (5 + gap) * scale
        for row, bits in enumerate(glyph):
            for col, bit in enumerate(bits):
                if bit != "1":
                    continue
                for yy in range(scale):
                    for xx in range(scale):
                        set_pixel(pixels, gx + col * scale + xx,
                                  y0 + row * scale + yy, color)


def text_frame(text: str, color: tuple[int, int, int] = (220, 240, 255),
               scale: int = 3) -> Frame:
    pixels = blank_pixels()
    _draw_text(pixels, text, scale=scale, color=color)
    return Frame(pixels=bytes(pixels))


def clock_frame(now: float | None = None) -> Frame:
    text = time.strftime("%H:%M", time.localtime(now))
    return text_frame(text, color=(220, 240, 255), scale=4)
