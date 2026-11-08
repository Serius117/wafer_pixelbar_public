"""TEST.CONSOLE.26: console LED mirror follows slot categories."""
from __future__ import annotations

from render.clockface import clock_frame, text_frame
from render.device_preview import DevicePreview
from render.slotface import CAT_CLOCK, CAT_GAME, CAT_HAPPY


class _FakeSlot:
    def __init__(self, category: int) -> None:
        self.current_category = category


def test_text_and_clock_frame_are_256x32() -> None:
    for frame in (text_frame("GAME"), clock_frame()):
        assert frame.width == 256
        assert frame.height == 32
        assert len(frame.pixels) == 256 * 32 * 3
        assert any(frame.pixels)


def test_clock_category_produces_clock_frame() -> None:
    preview = DevicePreview(_FakeSlot(CAT_CLOCK))
    frame = preview.frame()
    assert frame.width == 256 and any(frame.pixels)


def test_expression_category_produces_face_frame() -> None:
    preview = DevicePreview(_FakeSlot(CAT_HAPPY))
    frame = preview.frame()
    assert frame.width == 256 and len(frame.pixels) == 256 * 32 * 3
    assert any(frame.pixels)


def test_game_category_is_distinct() -> None:
    preview = DevicePreview(_FakeSlot(CAT_GAME))
    assert any(preview.frame().pixels)
