"""TEST.SLOT.13~15: export_expressions tool (TASK.SLOT.07) unit tests.

Covers: canvas rendering from wafer face code, blink animation frame
sequences, PNG export, and dry-run burn planning. No hardware needed.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "export_expressions", ROOT / "scripts" / "export_expressions.py")
ex = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ex)

EXPRESSIONS = ["normal", "happy", "sad", "angry", "thinking", "celebrate"]


class TestExportExpressions:

    def test_13_render_all_expressions(self):
        """TEST.SLOT.13: all six expressions -> valid 1bpp canvases."""
        for name in EXPRESSIONS:
            canvas = ex.render_canvas(name)
            assert len(canvas) == 256 * 32
            assert set(canvas) <= {0, 1}
            assert 0 < sum(canvas) < 256 * 32  # some lit, not full

    def test_13_unknown_expression_raises(self):
        """TEST.SLOT.13: unknown expression name -> ValueError."""
        with pytest.raises(ValueError):
            ex.render_canvas("bogus")

    def test_14_blink_frames_multiframe(self):
        """TEST.SLOT.14: normal blink = 20 open + 4 closed frames."""
        frames = ex.render_blink_frames("normal", open_frames=20)
        assert len(frames) == 24
        assert frames[0] == frames[19]   # open frames identical
        assert frames[20] == frames[23]  # closed frames identical
        assert frames[0] != frames[20]   # blink actually changes pixels
        # closed eyes light fewer pixels than open ones
        assert sum(frames[20]) < sum(frames[0])

    def test_14_blinkproof_expression_static(self):
        """TEST.SLOT.14: angry (chevron shut eyes) degrades to 1 frame."""
        frames = ex.render_blink_frames("angry")
        assert len(frames) == 1

    def test_15_png_export(self, tmp_path):
        """TEST.SLOT.15: PNG export writes loadable image files."""
        canvas = ex.render_canvas("happy")
        out = tmp_path / "expr_happy.png"
        ex._canvas_to_png(canvas, out)
        assert out.exists()
        from PIL import Image

        with Image.open(out) as img:
            assert img.size == (256, 32)
            assert list(img.convert("L").getdata()) == \
                [255 if v else 0 for v in canvas]

    def test_15_dry_run_burn_plan(self, capsys):
        """TEST.SLOT.15: dry-run burn prints plan, touches no device."""
        frames = ex.render_canvas("celebrate")
        ex._burn("celebrate", [frames], delay=100,
                 safe_area=False, dry_run=True)
        out = capsys.readouterr().out
        assert "celebrate" in out
        assert "category 7" in out
        assert "1064 B" in out

    def test_15_category_map_complete(self):
        """TEST.SLOT.15: every expression maps to its own category 2-7."""
        cats = [ex.EXPRESSION_CATEGORY[name] for name in EXPRESSIONS]
        assert sorted(cats) == [2, 3, 4, 5, 6, 7]

    def test_16_celebrate_animation_loop(self):
        """TEST.SLOT.16: celebrate = 24-frame seamless parametric loop."""
        frames = ex.render_blink_frames("celebrate")
        assert len(frames) == 24
        longer = ex.render_animated_frames("celebrate", n_frames=25)
        assert longer[:24] == frames       # capture is deterministic
        assert longer[24] == longer[0]     # frame 25 == frame 0: seamless

    def test_17_star_pulse(self):
        """TEST.SLOT.17: star eyes grow and shrink (pulse 5..9)."""
        frames = ex.render_animated_frames("celebrate")

        def eye_px(c):
            return sum(c[y * 256 + x] for y in range(3, 18)
                       for x in range(92, 165))

        counts = [eye_px(f) for f in frames]
        assert max(counts) > min(counts)    # eyes visibly pulse
        assert counts[3] > counts[9]        # t=0.3s big vs t=0.9s small

    def test_18_star_rain(self):
        """TEST.SLOT.18: 星星雨 falls continuously, no cannons remain."""
        frames = ex.render_animated_frames("celebrate")

        def region_px(c, x0, x1, y0=0, y1=32):
            return sum(c[y * 256 + x] for y in range(y0, y1)
                       for x in range(x0, x1))

        # cannons removed: bottom corners stay clean at former flash frames
        assert frames[0][27 * 256 + 25] == 0
        assert frames[12][27 * 256 + 231] == 0
        # rain always visible on both sides of the face, every frame
        for i, c in enumerate(frames):
            assert region_px(c, 0, 80) > 0, f"left rain missing at frame {i}"
            assert region_px(c, 176, 256) > 0, f"right rain missing {i}"
        # new stars keep entering from the top strip throughout the loop
        top_frames = sum(1 for c in frames if region_px(c, 0, 256, 0, 3) > 0)
        assert top_frames >= 12

    def test_19_gif_export(self, tmp_path):
        """TEST.SLOT.19: burn frames -> looping GIF (device-truth preview)."""
        frames = ex.render_blink_frames("celebrate")
        out = tmp_path / "expr_celebrate.gif"
        ex._canvases_to_gif(frames, out, scale=2, delay_ms=100)
        assert out.exists()
        from PIL import Image

        with Image.open(out) as img:
            assert getattr(img, "is_animated", False)
            assert img.n_frames == 24
            assert img.size == (256 * 2, 32 * 2)

    def test_20_top_edge_clip_avoided(self):
        """TEST.SLOT.20b: no lit pixel in the top-band blind columns.

        celebrate (full-screen star rain) is best-effort: stars cross the
        seam continuously and a few pixels there are imperceptible.
        """
        blind = set(range(0, 10)) | set(range(128, 138))
        for name in [e for e in EXPRESSIONS if e != "celebrate"]:
            frames = ex.avoid_top_edge_clip(
                ex.render_blink_frames(name, open_frames=4))
            bad = 0
            for canvas in frames:
                for i, v in enumerate(canvas):
                    if not v:
                        continue
                    x, y = i % 256, i // 256
                    if ((y < 8) or (16 <= y < 24)) and x in blind:
                        bad += 1
            assert bad == 0, f"{name}: {bad} pixels in blind columns"
