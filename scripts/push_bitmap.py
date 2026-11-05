"""PixelBar bitmap push + display calibration tool (TASK.SLOT.07).

Three modes:

  push an arbitrary image      --image foo.png [--invert]
  per-block calibration        --calibrate blocks [--block N]
    (block N (1-8) only by default: one image per run -- that block LIT
     with 4 dark corner dots + center dot + a unary block-number notch;
     the lit background shows the block's physical extent -- count from
     the bezel to the lit edge to read the absolute offset. Iterate a
     single block with --block N until its alignment is confirmed)
  per-dot calibration          --calibrate dots [--block N] [--dot M]
    (dot M (1-5: 1=左上 2=右上 3=左下 4=右下 5=中心) on a fully lit
     screen -- zero ambiguity, the most rigorous pass. Without --dot it
     cycles all five anchors; with --block N --dot M it is exactly ONE
     image for careful inspection of a single position)

  --dry-run  write encoded .bin + PNG previews to logs/calibration/
             instead of touching hardware (dev machine)
  --hold S   seconds each image stays on screen (default 6)

Calibration method notes (GUIDE §7.4): the mapping is a per-block
column offset constant, NOT a global warp; bright background + dark
dots is the recommended polarity for eyeballing (block extent visible,
dots countable). Expected geometry is printed to the console in sync
with every image so the observer knows what "correct" looks like.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.interfaces import Frame  # noqa: E402

WIDTH, HEIGHT = 256, 32
LIT = (255, 255, 255)
DARK = (0, 0, 0)

# block k -> (row0, col0) of its 128x8 region (unit = k // 4, page = k % 4;
# page 0=left/top 1=right/top 2=left/bottom 3=right/bottom of the unit)


def block_origin(block: int) -> tuple[int, int]:
    unit, page = block // 4, block % 4
    row0 = unit * 16 + (0 if page < 2 else 8)
    col0 = 0 if page % 2 == 0 else 128
    return row0, col0


BLOCK_NAMES = ["unit0·左上", "unit0·右上", "unit0·左下", "unit0·右下",
               "unit1·左上", "unit1·右上", "unit1·左下", "unit1·右下"]

# dot anchors inside a block: 4 corners (exact extremes) + center
DOT_ANCHORS = [(0, 0), (127, 0), (0, 7), (127, 7), (63, 3)]
DOT_NAMES = ["左上", "右上", "左下", "右下", "中心"]


def layout_text(current_1based: int | None = None) -> str:
    """ASCII map of the 8-block screen layout; the block being calibrated
    (1-8) is highlighted as [N]. Block numbering matches --block."""

    def cell(n: int) -> str:
        s = f"[{n}]" if n == current_1based else f" {n} "
        return f"{s:^9}"

    lines = [
        "屏幕 256×32 分块布局（8 块 = 2 unit 上下半屏 × 4 page 左右×上下带）：",
        "",
        "         +---------+---------+",
        f"行  0-7  |{cell(1)}|{cell(2)}|  unit0·上带 (page 0/1)",
        f"行  8-15 |{cell(3)}|{cell(4)}|  unit0·下带 (page 2/3)",
        "         +---------+---------+",
        f"行 16-23 |{cell(5)}|{cell(6)}|  unit1·上带 (page 0/1)",
        f"行 24-31 |{cell(7)}|{cell(8)}|  unit1·下带 (page 2/3)",
        "         +---------+---------+",
        "           列 0-127   列 128-255",
        "",
        "编号对照：1 unit0·左上 行0-7 列0-127   2 unit0·右上 行0-7 列128-255",
        "         3 unit0·左下 行8-15 列0-127  4 unit0·右下 行8-15 列128-255",
        "         5 unit1·左上 行16-23 列0-128 6 unit1·右上 行16-23 列128-255",
        "         7 unit1·左下 行24-31 列0-127 8 unit1·右下 行24-31 列128-255",
    ]
    if current_1based is not None:
        b = current_1based - 1
        r0, c0 = block_origin(b)
        lines.append(
            f"\n>>> 当前标定：块 {current_1based}（{BLOCK_NAMES[b]}，"
            f"行 {r0}-{r0 + 7}，列 {c0}-{c0 + 127}）<<<")
    return "\n".join(lines)


def _canvas() -> list[list[tuple[int, int, int]]]:
    return [[DARK] * WIDTH for _ in range(HEIGHT)]


def _set(px_: list, row: int, col: int, color: tuple) -> None:
    if 0 <= row < HEIGHT and 0 <= col < WIDTH:
        px_[row][col] = color


def frame_block_cal(block: int) -> Frame:
    """One block lit, dark corner+center dots, unary block notch."""
    cv = _canvas()
    r0, c0 = block_origin(block)
    for r in range(r0, r0 + 8):
        for c in range(c0, c0 + 128):
            cv[r][c] = LIT
    for (dx, dy) in DOT_ANCHORS:  # dark dots
        _set(cv, r0 + dy, c0 + dx, DARK)
    # unary block number: (block+1) dark pixel pairs on the top row,
    # starting at col c0+10 (clear of the corner dot)
    for i in range(block + 1):
        _set(cv, r0, c0 + 10 + i * 2, DARK)
        _set(cv, r0 + 1, c0 + 10 + i * 2, DARK)
    return Frame(pixels=_flatten(cv), width=WIDTH, height=HEIGHT)


def frame_dot_cal(block: int, dot_index: int) -> Frame:
    """Whole screen lit, ONE dark dot at the block's anchor."""
    cv = [[LIT] * WIDTH for _ in range(HEIGHT)]
    r0, c0 = block_origin(block)
    dx, dy = DOT_ANCHORS[dot_index]
    cv[r0 + dy][c0 + dx] = DARK
    return Frame(pixels=_flatten(cv), width=WIDTH, height=HEIGHT)


def _flatten(cv) -> bytes:
    out = bytearray()
    for row in cv:
        for color in row:
            out.extend(color)
    return bytes(out)


def frame_from_image(path: Path, invert: bool = False) -> Frame:
    """Load an image, resize-free: must already be 256x32."""
    from PIL import Image

    img = Image.open(path).convert("L")
    if img.size != (WIDTH, HEIGHT):
        raise ValueError(f"image must be {WIDTH}x{HEIGHT}, got {img.size}")
    out = bytearray()
    for y in range(HEIGHT):
        for x in range(WIDTH):
            v = img.getpixel((x, y))
            on = (v < 128) if invert else (v >= 128)
            out.extend(LIT if on else DARK)
    return Frame(pixels=bytes(out), width=WIDTH, height=HEIGHT)


def _describe_block(block: int) -> str:
    """User-facing block numbers are 1-based (matching --block and the
    unary notch count drawn inside the pattern)."""
    r0, c0 = block_origin(block)
    return (f"块 {block + 1}（{BLOCK_NAMES[block]}）行{r0}-{r0+7} "
            f"列{c0}-{c0+127}：应亮该 128×8 区域（其余 7 块全暗），"
            f"四角+中心 5 个暗点，顶部 {block+1} 组双点缺口=块号；"
            f"肉眼判读①从屏幕左/上边框数到亮区边缘=绝对偏移 "
            f"②5 个暗点是否在亮区四角+中心=内部映射")


def _describe_dot(block: int, dot_index: int) -> str:
    r0, c0 = block_origin(block)
    dx, dy = DOT_ANCHORS[dot_index]
    return (f"块 {block + 1}（{BLOCK_NAMES[block]}）· "
            f"点 {dot_index + 1}/5 {DOT_NAMES[dot_index]}："
            f"全屏亮，唯一暗点应在 行{r0+dy} 列{c0+dx}；"
            f"肉眼判读——暗点实际在行几列几，与预期差即该块偏移")

def _describe_image(path: Path, invert: bool) -> str:
    """Detailed echo for --image pushes: polarity + compensation caveat."""
    polarity = "暗像素=LED 亮（--invert）" if invert else "亮像素(≥128)=LED 亮"
    return (f"图片 {path.name}（{WIDTH}×{HEIGHT}，{polarity}）："
            f"核对内容位置/偏移。注意现有 ±10px 补偿的代价——"
            f"图片最左 10 列不显示、屏幕最右 10 列常空，属预期非错位")


def _invert_frame(frame: Frame) -> Frame:
    """Swap LIT/DARK for calibration images (--invert)."""
    out = bytearray(len(frame.pixels))
    for i in range(0, len(frame.pixels), 3):
        out[i:i + 3] = bytes(DARK) if frame.pixels[i] >= 128 else bytes(LIT)
    return Frame(pixels=bytes(out), width=frame.width, height=frame.height)


def _push_frames(frames: list[tuple[str, Frame]], hold: float,
                 dry_run: bool, out_dir: Path) -> int:
    if dry_run:
        from render.sinks.pixelbar import protocol as px

        out_dir.mkdir(parents=True, exist_ok=True)
        for i, (desc, frame) in enumerate(frames):
            data = px.encode_mono_bitmap(frame.pixels, frame.width,
                                         frame.height)
            (out_dir / f"{i:02d}.bin").write_bytes(data)
            try:  # preview PNG scaled x4 (PIL present in main venv)
                from PIL import Image

                img = Image.new("1", (WIDTH, HEIGHT))
                for y in range(HEIGHT):
                    for x in range(WIDTH):
                        img.putpixel(
                            (x, y),
                            frame.pixels[(y * WIDTH + x) * 3] >= 128)
                img.resize((WIDTH * 4, HEIGHT * 4), Image.NEAREST).save(
                    out_dir / f"{i:02d}.png")
            except Exception:  # noqa: BLE001 -- preview is optional
                pass
            print(f"[dry-run {i:02d}] {desc}")
        print(f"\ndry-run complete: {len(frames)} encoded files -> {out_dir}")
        return 0

    from render.sinks.pixelbar import PixelBarSink

    sink = PixelBarSink()
    sink.open()
    try:
        for i, (desc, frame) in enumerate(frames):
            print(f"\n[{i + 1}/{len(frames)}] {desc}")
            print("  （肉眼判读：亮区起点/暗点位置是否符合上述预期）")
            sink.push(frame)
            time.sleep(hold)
    finally:
        sink.close()
    print("\ncalibration sequence done")
    return 0


def _print_record_template(blocks: list[int]) -> None:
    """Print a fill-in table for eyeball measurements (GUIDE §7.4)."""
    print("\n标定记录表（肉眼看屏时顺手填写，测完发回即更新补偿常数）：")
    print("块 | 预期区域          | 亮区左缘距左框 | 亮区上缘距上框 "
          "| 暗点位置正确? | 备注")
    for b in blocks:
        r0, c0 = block_origin(b)
        print(f"{b + 1} | 行{r0}-{r0 + 7} 列{c0}-{c0 + 127}"
              f"{' ' * (11 - len(str(r0)) * 2)}| ____ 列"
              f"       | ____ 行       | 四角+中心?    |")
    print("判定基准：偏移 = 亮区边缘距边框的列/行数（0=贴齐）；"
          "暗点应落在亮区的四角+中心（否则该块内部映射有误）")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", type=Path, help="push one image (256x32)")
    ap.add_argument("--invert", action="store_true",
                    help="image polarity: dark pixels = LED on")
    ap.add_argument("--calibrate", choices=["blocks", "dots"],
                    help="calibration sequence (GUIDE §7.4 method)")
    ap.add_argument("--block", type=int, metavar="N", default=None,
                    help="calibrate ONLY block N (1-8, see layout map); "
                         "repeatable per-block reruns for iterating")
    ap.add_argument("--dot", type=int, metavar="M", default=None,
                    help="with --calibrate dots: show ONLY dot M (1-5: "
                         "1=左上 2=右上 3=左下 4=右下 5=中心) instead of "
                         "cycling all five -- one image for careful "
                         "inspection; combine with --block N for a single "
                         "block+dot image")
    ap.add_argument("--hold", type=float, default=6.0,
                    help="seconds per image on screen (default 6)")
    ap.add_argument("--dry-run", action="store_true",
                    help="encode to logs/calibration/ instead of hardware")
    args = ap.parse_args(argv)

    if sum(bool(x) for x in (args.image, args.calibrate)) != 1:
        ap.error("choose exactly one of --image / --calibrate")
    if args.block is not None and not 1 <= args.block <= 8:
        ap.error("--block must be 1-8")
    if args.dot is not None:
        if args.calibrate != "dots":
            ap.error("--dot requires --calibrate dots")
        if not 1 <= args.dot <= 5:
            ap.error("--dot must be 1-5 (1=左上 2=右上 3=左下 4=右下 5=中心)")

    blocks = ([args.block - 1] if args.block is not None
              else list(range(8)))
    dots = ([args.dot - 1] if args.dot is not None
            else list(range(len(DOT_ANCHORS))))
    current_1based = args.block  # None = full sweep (no highlight)

    if args.image:
        frames = [(_describe_image(args.image, args.invert),
                   frame_from_image(args.image, args.invert))]
    elif args.calibrate == "blocks":
        print(layout_text(current_1based))
        frames = [(_describe_block(b), frame_block_cal(b))
                  for b in blocks]
    else:
        print(layout_text(current_1based))
        frames = [(_describe_dot(b, d), frame_dot_cal(b, d))
                  for b in blocks for d in dots]

    if args.calibrate:
        _print_record_template(blocks)

    if args.calibrate and args.invert:
        frames = [(f"{desc} [inverted]", _invert_frame(frame))
                  for desc, frame in frames]

    out_dir = ROOT / "logs" / "calibration"
    return _push_frames(frames, args.hold, args.dry_run, out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
