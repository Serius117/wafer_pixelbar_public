"""TEST.CAL.01~03: calibration pattern generator (TASK.SLOT.07).

Geometry + dry-run tests for scripts/push_bitmap.py -- no hardware
needed (the encode path itself is golden-tested in the sink tests).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from scripts.push_bitmap import (  # noqa: E402
    DOT_ANCHORS,
    HEIGHT,
    WIDTH,
    block_origin,
    frame_block_cal,
    frame_dot_cal,
)


def _lit(frame, row: int, col: int) -> bool:
    return frame.pixels[(row * WIDTH + col) * 3] >= 128


class TestBlockGeometry:
    def test_01_block_origins_cover_screen_exactly_once(self):
        """TEST.CAL.01: the 8 block regions partition the 256x32 screen
        (unit = top/bottom half, page = left/right x top/bottom band)."""
        cells = set()
        for b in range(8):
            r0, c0 = block_origin(b)
            region = {(r, c) for r in range(r0, r0 + 8)
                      for c in range(c0, c0 + 128)}
            assert not (cells & region), f"block {b} overlaps another"
            cells |= region
        assert len(cells) == WIDTH * HEIGHT, "blocks must tile the screen"
        assert block_origin(0) == (0, 0)
        assert block_origin(1) == (0, 128)      # unit0 右上
        assert block_origin(2) == (8, 0)        # unit0 左下
        assert block_origin(7) == (24, 128)     # unit1 右下

    def test_02_block_pattern_lit_with_dark_dots(self):
        """TEST.CAL.02: block pattern = exactly its region lit, 5 dark
        anchor dots + block-number notch inside, rest of screen dark."""
        for b in range(8):
            frame = frame_block_cal(b)
            r0, c0 = block_origin(b)
            for r in range(HEIGHT):
                for c in range(WIDTH):
                    in_block = r0 <= r < r0 + 8 and c0 <= c < c0 + 128
                    expect_lit = in_block
                    if in_block:
                        for dx, dy in DOT_ANCHORS:
                            if (r, c) == (r0 + dy, c0 + dx):
                                expect_lit = False
                        i = (c - c0 - 10) // 2 if c >= c0 + 10 else -1
                        if i in range(b + 1) and (c - c0 - 10) % 2 == 0 \
                                and r in (r0, r0 + 1):
                            expect_lit = False
                    assert _lit(frame, r, c) == expect_lit, \
                        f"block {b} pixel ({r},{c}) wrong"

    def test_03_dot_pattern_single_dark_dot(self):
        """TEST.CAL.03: dot pattern = whole screen lit, exactly ONE dark
        pixel at the block's anchor position."""
        for b in range(8):
            for d, (dx, dy) in enumerate(DOT_ANCHORS):
                frame = frame_dot_cal(b, d)
                r0, c0 = block_origin(b)
                dark = [(r, c) for r in range(HEIGHT)
                        for c in range(WIDTH) if not _lit(frame, r, c)]
                assert dark == [(r0 + dy, c0 + dx)], \
                    f"block {b} dot {d}: dark={dark}"


class TestDryRun:
    def test_dry_run_encodes_all(self, tmp_path, capsys, monkeypatch):
        """--dry-run blocks writes 8 encoded .bin files (1064B each)."""
        import scripts.push_bitmap as pb
        from scripts.push_bitmap import main

        monkeypatch.setattr(pb, "ROOT", tmp_path, raising=False)
        code = main(["--calibrate", "blocks", "--dry-run"])
        assert code == 0
        out = tmp_path / "logs" / "calibration"
        bins = sorted(out.glob("*.bin"))
        assert len(bins) == 8
        assert all(p.stat().st_size == 1064 for p in bins)
        assert "块 8" in capsys.readouterr().out  # 1-based user numbering


class TestBlockSelection:
    def test_04_block_filter_single_image(self, tmp_path, capsys, monkeypatch):
        """TEST.CAL.04: --calibrate blocks --block 3 -> exactly ONE image
        for block 3 (unit0·左下), with the layout map printed and the
        block highlighted."""
        import scripts.push_bitmap as pb
        from scripts.push_bitmap import main

        monkeypatch.setattr(pb, "ROOT", tmp_path, raising=False)
        code = main(["--calibrate", "blocks", "--block", "3", "--dry-run"])
        assert code == 0
        out = tmp_path / "logs" / "calibration"
        assert len(list(out.glob("*.bin"))) == 1
        text = capsys.readouterr().out
        assert "块 3" in text and "unit0·左下" in text
        assert "[3]" in text, "layout map must highlight the current block"
        assert ">>> 当前标定：块 3" in text
        assert "标定记录表" in text and "亮区左缘距左框" in text, \
            "record template must be printed for careful eyeballing"
        assert "其余 7 块全暗" in text, "block echo must state the rest is dark"
        assert "肉眼判读①" in text, "block echo must carry the read-out guide"

    def test_05_block_dot_mode_five_images(self, tmp_path, capsys,
                                           monkeypatch):
        """TEST.CAL.05: --calibrate dots --block 3 -> exactly 5 dot images
        (corners + center), all described as block 3."""
        import scripts.push_bitmap as pb
        from scripts.push_bitmap import main

        monkeypatch.setattr(pb, "ROOT", tmp_path, raising=False)
        code = main(["--calibrate", "dots", "--block", "3", "--dry-run"])
        assert code == 0
        out = tmp_path / "logs" / "calibration"
        assert len(list(out.glob("*.bin"))) == 5
        text = capsys.readouterr().out
        assert text.count("块 3（unit0·左下）") == 5
        for name in ("左上", "右上", "左下", "右下", "中心"):
            assert name in text

    def test_06_single_dot_selection(self, tmp_path, capsys, monkeypatch):
        """TEST.CAL.06: --calibrate dots --block 3 --dot 2 -> exactly ONE
        image (block 3, 右上 corner); --dot without dots mode errors."""
        import scripts.push_bitmap as pb
        from scripts.push_bitmap import main

        monkeypatch.setattr(pb, "ROOT", tmp_path, raising=False)
        code = main(["--calibrate", "dots", "--block", "3",
                     "--dot", "2", "--dry-run"])
        assert code == 0
        out = tmp_path / "logs" / "calibration"
        assert len(list(out.glob("*.bin"))) == 1
        text = capsys.readouterr().out
        assert "点 2/5 右上" in text, "--dot 2 = user-facing dot 2 = 右上"

        # --dot without --calibrate dots is a usage error
        import pytest

        with pytest.raises(SystemExit):
            main(["--calibrate", "blocks", "--dot", "2", "--dry-run"])

    def test_layout_map_lists_all_blocks(self):
        """Layout map shows all 8 numbers; highlight only when current."""
        from scripts.push_bitmap import layout_text

        plain = layout_text()
        for n in range(1, 9):
            assert f" {n} " in plain
        assert "[5]" not in plain
        marked = layout_text(5)
        grid_row = next(ln for ln in marked.splitlines()
                        if "unit1·上带" in ln)
        assert "[5]" in grid_row, "grid cell must show [5]"
        assert " 5 " not in grid_row.replace("[5]", " "), \
            "the plain 5 must be replaced by [5] in the grid row"
        assert ">>> 当前标定：块 5" in marked
