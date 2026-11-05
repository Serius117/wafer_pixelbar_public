"""FaceManager: runtime-switchable face styles (F.1).

Holds the active face renderer and handles switching via:
  - console action `face_switch {style: "claw"|"mochi"|"bubble"|"mech"}`
  - bus event `face.switch {style}`
"""

from __future__ import annotations

import logging
from typing import Any

from core.interfaces import Frame

logger = logging.getLogger(__name__)

STYLES = ("wafer", "mochi", "bubble", "mech", "claw")
DEFAULT_STYLE = "wafer"


class FaceManager:
    """Wraps one active face; `update/tick/render` delegate to it.

    The active face can be swapped at runtime without restarting the
    render process. All faces implement the same duck-typed interface:
        update(**signals) / tick(dt) / render() -> Frame / set_expression(name)
    """

    def __init__(self, style: str = DEFAULT_STYLE) -> None:
        self._faces: dict[str, Any] = {}
        self._style = style
        self._active = self._create(style)

    @property
    def style(self) -> str:
        return self._style

    @property
    def active(self) -> Any:
        return self._active

    @staticmethod
    def available_styles() -> list[str]:
        return list(STYLES)

    def _create(self, style: str) -> Any:
        if style == "wafer":
            from render.faces.wafer import WaferFace
            return WaferFace()
        elif style == "claw":
            from render.claw_face import ClawFace
            return ClawFace()
        elif style == "mochi":
            from render.faces.mochi import MochiFace
            return MochiFace()
        elif style == "bubble":
            from render.faces.bubble import BubbleFace
            return BubbleFace()
        elif style == "mech":
            from render.faces.mech import MechFace
            return MechFace()
        raise ValueError(f"unknown face style: {style}")

    def switch(self, style: str) -> bool:
        """Switch to another face style. Returns True if switched."""
        if style not in STYLES:
            logger.warning("unknown face style: %s (available: %s)", style, STYLES)
            return False
        if style == self._style:
            return True  # already active

        # cache instances so switching back preserves state
        if style not in self._faces:
            self._faces[style] = self._create(style)

        self._style = style
        self._active = self._faces[style]
        logger.info("face style switched to: %s", style)
        return True

    # --- delegate to active face ---

    def set_expression(self, name: str) -> None:
        self._active.set_expression(name)

    def update(self, **signals: Any) -> None:
        self._active.update(**signals)

    def tick(self, dt: float = 1.0 / 30.0) -> None:
        self._active.tick(dt)

    def render(self) -> Frame:
        return self._active.render()

    # convenience for RenderEventBridge compatibility
    @property
    def state(self):
        return getattr(self._active, "state", None)
