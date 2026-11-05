"""TEST.SLOT.10: upload_slot library encoding rules (no hardware).

Covers the vendor-format frame layout, polarity, per-page top shift and the
safe-area fitter.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "upload_slot", ROOT / "scripts" / "upload_slot.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


slot = _load()


def _canvas_with(x: int, y: int) -> list[int]:
    canvas = [0] * (slot.W * slot.H)
    canvas[y * slot.W + x] = 1
    return canvas


def test_frame_layout_and_size() -> None:
    blob = slot.encode_canvas([0] * (slot.W * slot.H), delay=100,
                              top_shift=0)
    assert len(blob) == slot.FRAME_BYTES
    assert blob[0:6] == bytes([0x01, 0x00, 0x00, 0x00, 0x64, 0x00])
    assert blob[532:538] == bytes([0x01, 0x00, 0x04, 0x00, 0x64, 0x00])


def test_header_first_byte_is_target_category() -> None:
    blob = slot.encode_canvas([0] * (slot.W * slot.H), category=6)
    assert blob[0] == 6
    assert blob[532] == 6


def test_default_option_per_category() -> None:
    assert slot.default_option(2) == 10
    assert slot.default_option(3) == 9
    assert slot.default_option(4) == 3
    assert slot.default_option(5) == 0
    assert slot.default_option(6) == 0
    assert slot.default_option(7) == 15
    assert slot.default_option(1) == 0


def test_multiframe_unit_index_is_sequential() -> None:
    blank = [0] * (slot.W * slot.H)
    blob = slot.encode_canvases([blank, blank], category=3)
    heads = [blob[i * 532:i * 532 + 6] for i in range(4)]
    assert [h[0] for h in heads] == [3, 3, 3, 3]
    assert [h[2] for h in heads] == [0x00, 0x04, 0x08, 0x0C]


def test_polarity_normal_bit1_is_lit() -> None:
    canvas = _canvas_with(0, 0)
    normal = slot.encode_canvas(canvas, top_shift=0)
    assert normal[20] & 0x01  # page 0, byte 0, bit 0
    negative = slot.encode_canvas(canvas, top_shift=0, invert=True)
    assert not (negative[20] & 0x01)


def test_top_shift_moves_top_pages_only() -> None:
    top = slot.encode_canvas(_canvas_with(0, 0), top_shift=0)
    assert top[20] & 0x01              # page 0, byte 0
    clipped = slot.encode_canvas(_canvas_with(0, 0), top_shift=10)
    assert not any(clipped[20:20 + 128])  # canvas x=0 is pushed out
    moved = slot.encode_canvas(_canvas_with(10, 0), top_shift=10)
    assert moved[20] & 0x01            # canvas x=10 lands on byte 0
    bottom = slot.encode_canvas(_canvas_with(0, 8), top_shift=10)
    page2 = 20 + 2 * 128               # unit0 header + page 2
    assert bottom[page2] & 0x01        # bottom page unaffected


def test_safe_area_keeps_content_inside() -> None:
    canvas = _canvas_with(0, 5)
    fitted = slot.fit_safe_area(canvas)
    lit_x = [i % slot.W for i, v in enumerate(fitted) if v]
    assert lit_x and min(lit_x) >= slot.SAFE_X0
    assert max(lit_x) <= slot.SAFE_X1


def test_vendor_header_has_delay_byte() -> None:
    blob = slot.encode_canvas([0] * (slot.W * slot.H), delay=42)
    assert blob[4] == 42
    assert blob[532 + 4] == 42


def test_safe_area_frames_share_one_transform() -> None:
    """Blink frames must not be re-centred independently (2026-10-26 bug)."""
    open_c = [0] * (slot.W * slot.H)
    open_c[5 * slot.W + 100] = 1
    open_c[20 * slot.W + 100] = 1
    blink_c = [0] * (slot.W * slot.H)
    blink_c[20 * slot.W + 100] = 1
    fitted = slot.fit_safe_area_frames([open_c, blink_c])
    open_x = [i % slot.W for i, v in enumerate(fitted[0]) if v]
    blink_x = [i % slot.W for i, v in enumerate(fitted[1]) if v]
    assert blink_x and blink_x[0] == open_x[0]
