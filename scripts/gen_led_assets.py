"""Generate LED image assets (assets/led/): celebration GIF + test PNG.

One-off asset generation for the L2 layer (T6.10). Regenerate freely:
.venv\\Scripts\\python.exe scripts/gen_led_assets.py
"""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw

OUT_DIR = Path(__file__).parent.parent / "assets" / "led"
W, H = 256, 32


def make_celebrate_gif() -> None:
    """Confetti burst: 12 frames of falling colored dots."""
    rng = random.Random(42)
    colors = [(255, 80, 80), (255, 200, 60), (80, 255, 120),
              (80, 160, 255), (255, 120, 255)]
    drops = [
        {"x": rng.uniform(0, W), "y": rng.uniform(-H, 0),
         "v": rng.uniform(6, 14), "c": rng.choice(colors)}
        for _ in range(24)
    ]
    frames = []
    for _ in range(12):
        img = Image.new("RGB", (W, H), (0, 0, 0))
        d = ImageDraw.Draw(img)
        for drop in drops:
            y = drop["y"] % (H + 4)
            d.ellipse([drop["x"] - 1, y - 1, drop["x"] + 1, y + 1], fill=drop["c"])
            drop["y"] += drop["v"]
        frames.append(img)
    frames[0].save(OUT_DIR / "celebrate.gif", save_all=True,
                   append_images=frames[1:], duration=100, loop=0)


def make_test_png() -> None:
    """A simple test pattern: green border + white center dot."""
    img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W - 1, H - 1], outline=(0, 200, 80))
    d.ellipse([W // 2 - 4, H // 2 - 4, W // 2 + 4, H // 2 + 4],
              fill=(255, 255, 255))
    img.save(OUT_DIR / "test_pattern.png")


if __name__ == "__main__":
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    make_celebrate_gif()
    make_test_png()
    for p in sorted(OUT_DIR.iterdir()):
        print(f"{p.name}: {p.stat().st_size} bytes")
