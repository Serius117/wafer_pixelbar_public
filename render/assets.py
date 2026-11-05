"""LED image assets (L2 layer, T6.10 / D10).

Static PNGs and animated GIFs live in assets/led/<name>.png|gif and are
rendered at full canvas size (256x32). The render loop draws them via
RenderEventBridge's image override (highest display priority), e.g. a
celebration GIF when a task succeeds, festival easter eggs, etc.
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.interfaces import Frame

logger = logging.getLogger(__name__)

WIDTH = 256
HEIGHT = 32
ASSETS_DIR = Path("assets/led")


def _pil_to_frame(image) -> Frame:
    """Convert a PIL image (already sized) to a Frame."""
    if image.mode != "RGB":
        image = image.convert("RGB")
    return Frame(pixels=image.tobytes(), width=WIDTH, height=HEIGHT)


def load_asset_frames(path: Path) -> list[Frame]:
    """Load a PNG (1 frame) or GIF (N frames) into Frames."""
    from PIL import Image

    with Image.open(path) as img:
        if getattr(img, "is_animated", False):
            frames = []
            for i in range(img.n_frames):
                img.seek(i)
                frames.append(_pil_to_frame(img.resize((WIDTH, HEIGHT))))
            return frames
        return [_pil_to_frame(img.resize((WIDTH, HEIGHT)))]


class AssetLibrary:
    """Loads and caches LED image assets by name."""

    def __init__(self, assets_dir: Path = ASSETS_DIR) -> None:
        self._dir = Path(assets_dir)
        self._cache: dict[str, list[Frame]] = {}

    def frames(self, name: str) -> list[Frame]:
        """Frames for an asset name; empty list when absent (caller falls
        back to the face -- a missing easter egg must never break rendering)."""
        if name in self._cache:
            return self._cache[name]
        for ext in (".gif", ".png"):
            path = self._dir / f"{name}{ext}"
            if path.exists():
                try:
                    frames = load_asset_frames(path)
                    self._cache[name] = frames
                    return frames
                except Exception:
                    logger.exception("failed to load LED asset %s", path)
                    self._cache[name] = []
                    return []
        logger.debug("LED asset not found: %s", name)
        self._cache[name] = []
        return []
