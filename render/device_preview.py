"""Console mirror of the real PixelBar slot/text display."""
from __future__ import annotations

from core.interfaces import Frame
from render.clockface import clock_frame, text_frame
from render.faces.wafer import WaferFace
from render.slotface import (
    CAT_ANGRY,
    CAT_CELEBRATE,
    CAT_CLOCK,
    CAT_GAME,
    CAT_HAPPY,
    CAT_NORMAL,
    CAT_SAD,
    CAT_THINKING,
)

CAT_EXPRESSION = {
    CAT_NORMAL: "normal",
    CAT_HAPPY: "happy",
    CAT_SAD: "sad",
    CAT_ANGRY: "angry",
    CAT_THINKING: "thinking",
    CAT_CELEBRATE: "celebrate",
}


class DevicePreview:
    """Produce the same logical screen the device is currently showing.

    The real PixelBar has no frame channel in slot mode: it is showing a
    native clock (0), native game scene (1), or one of the burned expression
    categories (2-7).  The console mirrors that category instead of running
    its own independent face loop.
    """

    def __init__(self, slotface) -> None:  # noqa: ANN001
        self._slotface = slotface
        self._face = WaferFace()
        self._last_expression = ""

    @property
    def category(self) -> int:
        return int(self._slotface.current_category)

    def frame(self) -> Frame:
        cat = self.category
        if cat == CAT_CLOCK:
            return clock_frame()
        if cat == CAT_GAME:
            return text_frame("GAME", color=(255, 190, 80), scale=3)
        expression = CAT_EXPRESSION.get(cat)
        if expression is None:
            return clock_frame()
        if expression != self._last_expression:
            self._face.set_expression(expression)
            self._last_expression = expression
        self._face.tick(0.1)
        return self._face.render()
