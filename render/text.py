"""Simple 3x5 pixel font for status text on the 256x32 LED display."""

from __future__ import annotations

from render.claw_face import STATUS_COLOR

WIDTH = 256
HEIGHT = 32

# 3x5 font: each char is 5 rows of 3 bits (top to bottom)
FONT_3X5: dict[str, list[int]] = {
    "A": [0b010, 0b101, 0b111, 0b101, 0b101],
    "B": [0b110, 0b101, 0b110, 0b101, 0b110],
    "C": [0b011, 0b100, 0b100, 0b100, 0b011],
    "D": [0b110, 0b101, 0b101, 0b101, 0b110],
    "E": [0b111, 0b100, 0b110, 0b100, 0b111],
    "F": [0b111, 0b100, 0b110, 0b100, 0b100],
    "G": [0b011, 0b100, 0b101, 0b101, 0b011],
    "H": [0b101, 0b101, 0b111, 0b101, 0b101],
    "I": [0b111, 0b010, 0b010, 0b010, 0b111],
    "J": [0b001, 0b001, 0b001, 0b101, 0b010],
    "K": [0b101, 0b101, 0b110, 0b101, 0b101],
    "L": [0b100, 0b100, 0b100, 0b100, 0b111],
    "M": [0b101, 0b111, 0b111, 0b101, 0b101],
    "N": [0b101, 0b111, 0b111, 0b111, 0b101],
    "O": [0b010, 0b101, 0b101, 0b101, 0b010],
    "P": [0b110, 0b101, 0b110, 0b100, 0b100],
    "Q": [0b010, 0b101, 0b101, 0b111, 0b011],
    "R": [0b110, 0b101, 0b110, 0b101, 0b101],
    "S": [0b011, 0b100, 0b010, 0b001, 0b110],
    "T": [0b111, 0b010, 0b010, 0b010, 0b010],
    "U": [0b101, 0b101, 0b101, 0b101, 0b111],
    "V": [0b101, 0b101, 0b101, 0b010, 0b010],
    "W": [0b101, 0b101, 0b111, 0b111, 0b101],
    "X": [0b101, 0b101, 0b010, 0b101, 0b101],
    "Y": [0b101, 0b101, 0b010, 0b010, 0b010],
    "Z": [0b111, 0b001, 0b010, 0b100, 0b111],
    "0": [0b111, 0b101, 0b101, 0b101, 0b111],
    "1": [0b010, 0b110, 0b010, 0b010, 0b111],
    "2": [0b111, 0b001, 0b111, 0b100, 0b111],
    "3": [0b111, 0b001, 0b011, 0b001, 0b111],
    "4": [0b101, 0b101, 0b111, 0b001, 0b001],
    "5": [0b111, 0b100, 0b111, 0b001, 0b111],
    "6": [0b111, 0b100, 0b111, 0b101, 0b111],
    "7": [0b111, 0b001, 0b001, 0b001, 0b001],
    "8": [0b111, 0b101, 0b111, 0b101, 0b111],
    "9": [0b111, 0b101, 0b111, 0b001, 0b111],
    ":": [0b000, 0b010, 0b000, 0b010, 0b000],
    ".": [0b000, 0b000, 0b000, 0b000, 0b010],
    "-": [0b000, 0b000, 0b111, 0b000, 0b000],
    "_": [0b000, 0b000, 0b000, 0b000, 0b111],
    " ": [0b000, 0b000, 0b000, 0b000, 0b000],
    "%": [0b101, 0b001, 0b010, 0b100, 0b101],
    "!": [0b010, 0b010, 0b010, 0b000, 0b010],
    "?": [0b110, 0b001, 0b010, 0b000, 0b010],
    "/": [0b001, 0b001, 0b010, 0b100, 0b100],
}

CHAR_WIDTH = 4  # 3 pixels + 1 gap
CHAR_HEIGHT = 5


def draw_text(
    pixels: bytearray,
    text: str,
    x: int,
    y: int,
    color: tuple[int, int, int] = STATUS_COLOR,
) -> int:
    """Draw text using the 3x5 pixel font. Returns the end x position."""
    cx = x
    for ch in text.upper():
        glyph = FONT_3X5.get(ch)
        if glyph is None:
            cx += CHAR_WIDTH
            continue
        for row, bits in enumerate(glyph):
            for col in range(3):
                if bits & (1 << (2 - col)):
                    px_x = cx + col
                    px_y = y + row
                    if 0 <= px_x < WIDTH and 0 <= px_y < HEIGHT:
                        idx = (px_y * WIDTH + px_x) * 3
                        pixels[idx] = color[0]
                        pixels[idx + 1] = color[1]
                        pixels[idx + 2] = color[2]
        cx += CHAR_WIDTH
    return cx


def get_text_width(text: str) -> int:
    """Calculate the pixel width of a text string."""
    return len(text) * CHAR_WIDTH
