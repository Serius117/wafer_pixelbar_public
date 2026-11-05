"""Export wafer expressions to burnable assets (TASK.SLOT.07).

What gets burned (protocol fact, D49): each frame is a 1bpp canvas
(256x32, list of 0/1) encoded into a 1064-byte vendor blob written to
flash via 0x17. PNG is only one possible input; this tool renders the
expressions straight from code (render/faces/wafer.py, RGB888) into
canvases -- no PNG intermediate is required for burning.

Multi-frame animation is fully supported by the device: burning N frames
makes the device loop them with the per-frame delay (default 100 ms).
This tool exploits that for blink animations (--blink).

Usage (run from repo root)
-------------------------
    # ASCII preview of all six expressions (no hardware)
    python scripts/export_expressions.py --preview

    # static PNGs (preview / manual burn via upload_slot.py --png)
    python scripts/export_expressions.py --out temp_script/expr

    # blink animation PNG frame sequences
    python scripts/export_expressions.py --out temp_script/expr --blink

    # direct burn, all six categories (hardware; close TempoHub first)
    python scripts/export_expressions.py --burn

    # burn a single expression, slower blink cadence
    python scripts/export_expressions.py --burn --only happy --open-frames 30

    # preview the EXACT burn frames as animated GIF (device truth)
    python scripts/export_expressions.py --gif temp_script/preview --blink
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from render.faces.wafer import WaferFace  # noqa: E402

# One expression per PixelBar category (D49 architecture, 2026-10-26):
# runtime switch = single 0xEF 02 command (~2ms); never switch options.
EXPRESSION_CATEGORY: dict[str, int] = {
    "normal": 2,
    "happy": 3,
    "sad": 4,
    "angry": 5,
    "thinking": 6,
    "celebrate": 7,
}

W, H = 256, 32
THRESHOLD = 50          # 1bpp threshold, same as console compare view
SETTLE_TICKS = 12       # advance breathing/animation to a steady state
BLINK_FRAMES = 4        # closed-eye frames appended after the open frames
DEFAULT_OPEN_FRAMES = 20
DEFAULT_DELAY = 100     # ms per frame on device

# Expressions with built-in parametric animations (wafer.py _anim_t):
# captured by consecutive tick(dt) renders instead of blink-frame repeats.
# Frame count * dt must equal the animation period for a seamless loop.
ANIMATED_FRAMES: dict[str, tuple[int, float]] = {"celebrate": (24, 0.1)}


def _frame_to_canvas(frame) -> list[int]:
    """RGB888 Frame -> 1bpp canvas (8192 ints, 1 = lit)."""
    px, fw, fh = frame.pixels, frame.width, frame.height
    if (fw, fh) != (W, H):
        raise RuntimeError(f"unexpected frame size {fw}x{fh}")
    canvas: list[int] = []
    for y in range(H):
        base = y * W * 3
        for x in range(W):
            i = base + x * 3
            canvas.append(1 if max(px[i], px[i + 1], px[i + 2]) > THRESHOLD
                          else 0)
    return canvas


# --- rendering ---------------------------------------------------------------

def _settle(face: WaferFace, ticks: int = SETTLE_TICKS,
            dt: float = 1.0 / 30.0) -> None:
    for _ in range(ticks):
        face.tick(dt)


def _force_blink(face: WaferFace, on: bool) -> None:
    st = face.state
    st._blinking = on
    st._blink_start = 0.0
    st._next_blink = float("inf")


def render_canvas(expression: str, blinking: bool = False) -> list[int]:
    """Render one expression to a 1bpp canvas (8192 ints, 1 = lit)."""
    face = WaferFace()
    face.set_expression(expression)
    if face.state.expression != expression:
        raise ValueError(f"unknown expression: {expression}")
    _settle(face)
    if blinking:
        _force_blink(face, True)
    return _frame_to_canvas(face.render())


def _suppress_blink(face: WaferFace) -> None:
    st = face.state
    st._blinking = False
    st._next_blink = float("inf")


def render_animated_frames(expression: str, n_frames: int = 24,
                           dt: float = 0.1) -> list[list[int]]:
    """Capture a parametric animation by consecutive tick(dt) renders.

    The animation clock (_anim_t) is reset to 0 first, so frame N shows
    the face at t = N*dt; with n_frames*dt equal to the animation period
    the captured loop is seamless (frame n == frame 0).
    """
    face = WaferFace()
    face.set_expression(expression)
    if face.state.expression != expression:
        raise ValueError(f"unknown expression: {expression}")
    _settle(face)
    _suppress_blink(face)
    face.state._anim_t = 0.0
    frames: list[list[int]] = []
    for _ in range(n_frames):
        frames.append(_frame_to_canvas(face.render()))
        face.tick(dt)
    return frames


def render_blink_frames(expression: str,
                        open_frames: int = DEFAULT_OPEN_FRAMES,
                        ) -> list[list[int]]:
    """Animation frames for burning: parametric animation if the
    expression has one, otherwise the blink loop (open + closed frames).

    Blink-proof expressions (angry chevrons / thinking monocle) render
    identically open vs closed and degrade to a single static frame.
    """
    if expression in ANIMATED_FRAMES:
        n, dt = ANIMATED_FRAMES[expression]
        return render_animated_frames(expression, n_frames=n, dt=dt)
    open_c = render_canvas(expression, blinking=False)
    closed_c = render_canvas(expression, blinking=True)
    if open_c == closed_c:
        return [open_c]
    return [open_c] * open_frames + [closed_c] * BLINK_FRAMES


# --- output helpers ----------------------------------------------------------

def _canvas_to_png(canvas: list[int], path: Path) -> None:
    from PIL import Image

    img = Image.new("1", (W, H), 0)
    img.putdata(canvas)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def _canvases_to_gif(canvases: list[list[int]], path: Path, scale: int = 4,
                     delay_ms: int = 100) -> None:
    """Write the burn frame sequence as a looping GIF (device truth).

    One GIF frame per burn frame at the device per-frame delay -- what
    you see here is exactly what the PixelBar plays. Scale is nearest-
    neighbour so the 1bpp pixels stay crisp.
    """
    from PIL import Image

    imgs = []
    for c in canvases:
        img = Image.new("L", (W, H), 0)
        img.putdata([255 if v else 0 for v in c])
        if scale > 1:
            img = img.resize((W * scale, H * scale), Image.NEAREST)
        imgs.append(img)
    path.parent.mkdir(parents=True, exist_ok=True)
    imgs[0].save(path, save_all=True, append_images=imgs[1:],
                 duration=delay_ms, loop=0)


def _preview(canvas: list[int], title: str) -> None:
    print(f"--- {title} ({sum(canvas)} lit px) ---")
    step = 2  # 2 horizontal px per char -> 128 cols
    for y in range(H):
        row = []
        for cx in range(0, W, step):
            lit = any(canvas[y * W + x] for x in range(cx, cx + step))
            row.append("#" if lit else ".")
        print("".join(row))


UNSAFE_X = set(range(0, 10)) | set(range(128, 138))


def _clip_loss(frames: list[list[int]]) -> int:
    """Pixels that fall into the top-band page-edge blind columns."""
    count = 0
    for canvas in frames:
        for i, value in enumerate(canvas):
            if not value:
                continue
            x, y = i % W, i // W
            if ((y < 8) or (16 <= y < 24)) and x in UNSAFE_X:
                count += 1
    return count


def _shift_down(frames: list[list[int]], dy: int) -> list[list[int]]:
    if dy <= 0:
        return frames
    out = []
    for canvas in frames:
        moved = [0] * (W * H)
        for i, value in enumerate(canvas):
            if value:
                x, y = i % W, i // W
                if y + dy < H:
                    moved[(y + dy) * W + x] = 1
        out.append(moved)
    return out


def avoid_top_edge_clip(frames: list[list[int]]) -> list[list[int]]:
    """Nudge the animation down if it hits the top-band blind columns.

    Rows 0-7 / 16-23 cannot display x in [0,9] and [128,137] (the +10 panel
    compensation pushes those page-local columns out of the frame). Moving
    content down into the bottom band avoids losing it (2026-10-26: sad's
    mouth lost its right half at x=128-137).
    """
    if _clip_loss(frames) == 0:
        return frames
    for dy in range(1, 7):
        shifted = _shift_down(frames, dy)
        if _clip_loss(shifted) == 0:
            return shifted
    return frames


def _burn(expression: str, frames: list[list[int]], delay: int,
          safe_area: bool, dry_run: bool) -> None:
    from scripts.upload_slot import default_option, fit_safe_area_frames, upload_canvases

    if safe_area:
        frames = fit_safe_area_frames(frames)
    frames = avoid_top_edge_clip(frames)
    category = EXPRESSION_CATEGORY[expression]
    if dry_run:
        total = 1064 * len(frames)
        print(f"[dry-run] {expression}: {len(frames)} frame(s), "
              f"category {category}, option {default_option(category)}, "
              f"{total} B, delay {delay} ms")
        return
    upload_canvases(frames, category=category, delay=delay,
                    switch=True)
    print(f"burned {expression}: {len(frames)} frame(s) -> category "
          f"{category} (switched to it)")


# --- CLI ---------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--preview", action="store_true",
                    help="ASCII preview of all expressions (no hardware)")
    ap.add_argument("--out", type=Path, default=None,
                    help="directory for PNG export")
    ap.add_argument("--gif", type=Path, default=None,
                    help="directory for animated-GIF export of the exact "
                         "burn frames (device-truth preview)")
    ap.add_argument("--scale", type=int, default=4,
                    help="GIF upscale factor, nearest-neighbour "
                         "(default 4 -> 1024x128)")
    ap.add_argument("--blink", action="store_true",
                    help="blink animation (open frames + closed frames)")
    ap.add_argument("--open-frames", type=int, default=DEFAULT_OPEN_FRAMES,
                    help=f"open-eye frames before each blink "
                         f"(default {DEFAULT_OPEN_FRAMES})")
    ap.add_argument("--burn", action="store_true",
                    help="burn directly via 0x17 (hardware; close TempoHub)")
    ap.add_argument("--only", choices=sorted(EXPRESSION_CATEGORY),
                    default=None, help="restrict to one expression")
    ap.add_argument("--delay", type=int, default=DEFAULT_DELAY,
                    help=f"per-frame delay in ms (default {DEFAULT_DELAY})")
    ap.add_argument("--safe-area", action="store_true",
                    help="fit content into x [10,245] before burn")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be burned, touch no device")
    args = ap.parse_args()

    names = [args.only] if args.only else list(EXPRESSION_CATEGORY)
    if not (args.preview or args.out or args.gif or args.burn
            or args.dry_run):
        ap.print_help()
        return 2

    for name in names:
        # expressions with built-in parametric animations always burn
        # animated (the animation IS the expression), --blink only adds
        # blink loops to the other expressions
        animated = name in ANIMATED_FRAMES
        frames = (render_blink_frames(name, args.open_frames)
                  if args.blink or animated
                  else [render_canvas(name)])
        if args.preview:
            _preview(frames[-1], name if not (args.blink or animated)
                     else f"{name} (last frame)")
        if args.out:
            if (args.blink or animated) and len(frames) > 1:
                for i, c in enumerate(frames):
                    _canvas_to_png(c, args.out / f"expr_{name}_f{i:02d}.png")
                print(f"{name}: {len(frames)} frames -> "
                      f"{args.out / f'expr_{name}_f*.png'}")
            else:
                _canvas_to_png(frames[0], args.out / f"expr_{name}.png")
                print(f"{name}: 1 frame -> "
                      f"{args.out / f'expr_{name}.png'}")
        if args.gif:
            gif_path = args.gif / f"expr_{name}.gif"
            _canvases_to_gif(frames, gif_path, args.scale, args.delay)
            print(f"{name}: {len(frames)} frames -> {gif_path} "
                  f"(@{args.delay} ms, x{args.scale})")
        if args.burn or args.dry_run:
            _burn(name, frames, args.delay, args.safe_area, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
