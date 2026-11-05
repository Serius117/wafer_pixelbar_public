"""PixelBar scene-slot uploader -- library + CLI (E-plan / TASK.SCOUT.01).

Library
-------
    from scripts.upload_slot import upload_frames, upload_canvases

    # PNG / GIF files -> slot (category 1 = 游戏, option 3)
    upload_frames(["face.png"], category=1, option=3)
    upload_frames(["f0.png", "f1.png"], category=1, option=3, switch=True)

    # Already-binarised canvases (list[list[int]], 256*32, 1 = lit)
    upload_canvases(canvases, category=1, option=3)

Protocol (captured from TempoHub, 2026-10-25)
    select slot : 0xEF 01 FF FF FF 00 01 <category> <option> FF
    upload      : 0x17 [0, 0, size_be32] -> device asks for data -> chunks
    switch      : 0xEF 02 FF FF FF 00 01 <category> FF FF
Unit header per 1064-byte frame: 01 00 <unit*4> 00 <delay> 00 + 14 zeros;
size = 1064 * frames; chunk <= 50 bytes.

Display mapping (calibrated on PID 0xA160, 2026-10-26)
    * polarity: bit=1 = lit (normal)
    * rows: LSB first, 1:1 with canvas rows
    * columns: top pages (0/1) need +10 offset (content shifted left) because
      the panel shifts the top bands 10px right; bottom pages need none
    * physical left margin ~5px on every page
    * safe area: x in [10, 245] (236 x 32) for content that must survive
      both bands

TempoHub must be CLOSED (exclusive HID); run `scripts/stop_all.py` first.

CLI
---
    .\\.venv\\Scripts\\python.exe scripts/upload_slot.py --png face.png --category 1 --option 3
    .\\.venv\\Scripts\\python.exe scripts/upload_slot.py --png a.png --png b.png --safe-area
    .\\.venv\\Scripts\\python.exe scripts/upload_slot.py --gif anim.gif --max-frames 12
    .\\.venv\\Scripts\\python.exe scripts/upload_slot.py --pattern grid --dry-run
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from render.sinks.pixelbar import open_pixelbar_device  # noqa: E402
from render.sinks.pixelbar import protocol as px  # noqa: E402

W, H = 256, 32
SAFE_X0, SAFE_X1 = 10, 245           # inclusive, verified safe for both bands
FRAME_BYTES = 1064
DEFAULT_DELAY = 100
DEFAULT_TOP_SHIFT = 10

# Per-category user-image slot (captured from TempoHub, 2026-10-26).
# Categories 2-7 are the expression scenes; the vendor app overwrites a
# specific template index in each of them (they are NOT all 0).
CATEGORY_OPTION: dict[int, int] = {
    2: 10,   # normal
    3: 9,    # happy
    4: 3,    # sad
    5: 0,    # angry
    6: 0,    # thinking
    7: 15,   # celebrate
}


def default_option(category: int, fallback: int = 0) -> int:
    return CATEGORY_OPTION.get(category, fallback)


# --- canvas helpers ---------------------------------------------------------

def blank() -> list[int]:
    return [0] * (W * H)


def _set(canvas: list[int], x: int, y: int, value: int = 1) -> None:
    if 0 <= x < W and 0 <= y < H:
        canvas[y * W + x] = value


def canvas_from_image(path: Path, safe_area: bool = False,
                      threshold: int = 96) -> list[int]:
    from PIL import Image

    with Image.open(path) as img:
        gray = img.convert("L").resize((W, H))
        pixels = list(gray.getdata())
    canvas = [1 if value > threshold else 0 for value in pixels]
    return fit_safe_area(canvas) if safe_area else canvas


def canvas_list_from_gif(path: Path, max_frames: int = 12,
                         safe_area: bool = False,
                         threshold: int = 96) -> list[list[int]]:
    from PIL import Image, ImageSequence

    frames: list[list[int]] = []
    with Image.open(path) as img:
        for frame in ImageSequence.Iterator(img):
            gray = frame.convert("L").resize((W, H))
            canvas = [1 if v > threshold else 0 for v in gray.getdata()]
            frames.append(fit_safe_area(canvas) if safe_area else canvas)
            if len(frames) >= max_frames:
                break
    return frames


def fit_safe_area(canvas: list[int]) -> list[int]:
    """Scale/centre ONE canvas into x in [SAFE_X0, SAFE_X1]."""
    return fit_safe_area_frames([canvas])[0]


def fit_safe_area_frames(frames: list[list[int]]) -> list[list[int]]:
    """Fit an animation into the safe area with ONE shared transform.

    Fitting each frame independently makes open/closed (or any frames with a
    different bounding box) jump around: the blink frame gets its own scale
    and centre. Use the union bbox of all frames so the animation stays
    registered.
    """
    xs: list[int] = []
    ys: list[int] = []
    for canvas in frames:
        for i, value in enumerate(canvas):
            if value:
                xs.append(i % W)
                ys.append(i // W)
    if not xs:
        return frames
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    from PIL import Image

    width = SAFE_X1 - SAFE_X0 + 1
    crop_w, crop_h = x1 - x0 + 1, y1 - y0 + 1
    scale = min(width / crop_w, H / crop_h, 1.0)  # shrink only
    new_w, new_h = max(1, int(crop_w * scale)), max(1, int(crop_h * scale))
    paste_x = SAFE_X0 + (width - new_w) // 2
    paste_y = (H - new_h) // 2

    out_frames: list[list[int]] = []
    for canvas in frames:
        img = Image.new("L", (W, H), 0)
        pix = img.load()
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                if canvas[y * W + x]:
                    pix[x, y] = 255
        crop = img.crop((x0, y0, x1 + 1, y1 + 1))
        crop = crop.resize((new_w, new_h), Image.NEAREST)
        out = Image.new("L", (W, H), 0)
        out.paste(crop, (paste_x, paste_y))
        out_frames.append([1 if out.getpixel((x, y)) >= 128 else 0
                           for y in range(H) for x in range(W)])
    return out_frames


def frame_pattern(name: str) -> list[list[int]]:
    """Built-in calibration/test patterns."""
    if name == "dots":
        canvas = blank()
        for x in (8, 64, 128, 192, 248):
            _set(canvas, x, 8)
        _set(canvas, 128, 24)
        return [canvas]
    if name == "grid":
        canvas = blank()
        for x in range(0, W, 32):
            for y in range(H):
                _set(canvas, x, y)
        for x in range(W):
            _set(canvas, x, 0)
            _set(canvas, x, H - 1)
        for x in range(0, W, 16):
            _set(canvas, x, 1)
            _set(canvas, x, 2)
        for y in range(H):
            _set(canvas, W // 2, y)
        for x in range(W // 2 - 8, W // 2 + 9):
            _set(canvas, x, H // 2)
        return [canvas]
    if name == "demo":
        frames = []
        for step in range(3):
            canvas = blank()
            x0 = 20 + step * 60
            for y in range(8, 24):
                for x in range(x0, min(W, x0 + 40)):
                    _set(canvas, x, y)
            for x in range(4, W - 4):
                _set(canvas, x, 2)
                _set(canvas, x, 29)
            frames.append(canvas)
        return frames
    pairs = {
        "cal-left": [(16, 3), (16, 11)],
        "cal-right": [(144, 3), (144, 11)],
        "cal-unit1": [(16, 19), (16, 27)],
    }
    if name in pairs:
        canvas = blank()
        for x, y in pairs[name]:
            _set(canvas, x, y)
        return [canvas]
    raise ValueError(f"unknown pattern: {name}")


# --- encoding ---------------------------------------------------------------

def _frame_header(unit_index: int, delay: int, category: int) -> bytes:
    """Unit header: <category> 00 <unit_index*4> 00 <delay> 00 + 14 zeros.

    ``unit_index`` is the GLOBAL unit number inside the uploaded file
    (frame 0 -> units 0,1; frame 1 -> units 2,3; vendor capture shows the
    byte sequence 00,04,08,0C,10,14). The first byte is the target category.
    """
    return bytes([category & 0xFF, 0x00, (unit_index * 4) & 0xFF, 0x00,
                  delay & 0xFF, 0x00] + [0] * 14)


def encode_canvas(canvas: list[int], delay: int = DEFAULT_DELAY,
                  shift: int = 0, top_shift: int = DEFAULT_TOP_SHIFT,
                  invert: bool = False, msb: bool = False,
                  category: int = 1, frame_index: int = 0) -> bytes:
    """Encode one 256x32 canvas into a 1064-byte frame."""
    out = bytearray()
    for unit in range(2):
        out += _frame_header(frame_index * 2 + unit, delay, category)
        for page in range(4):
            xbase = 0 if page in (0, 2) else 128
            ybase = unit * 16 + (0 if page in (0, 1) else 8)
            offset = shift + (top_shift if page in (0, 1) else 0)
            for x in range(128):
                byte = 0
                sx = x + offset
                if 0 <= sx < 128:
                    base = ybase * W + xbase + sx
                    for bit in range(8):
                        row = bit if not msb else 7 - bit
                        lit = bool(canvas[base + row * W])
                        if lit != invert:
                            byte |= 1 << bit
                out.append(byte)
    return bytes(out)


def encode_canvases(canvases: list[list[int]], delay: int = DEFAULT_DELAY,
                    shift: int = 0, top_shift: int = DEFAULT_TOP_SHIFT,
                    invert: bool = False, msb: bool = False,
                    category: int = 1) -> bytes:
    return b"".join(
        encode_canvas(c, delay, shift, top_shift, invert, msb, category, i)
        for i, c in enumerate(canvases))


# --- device I/O -------------------------------------------------------------

def _read_state(dev, command: int, first_byte: int | None,
                timeout: float = 3.0) -> list[int] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = dev.read(px.REPORT_LEN, timeout_ms=100)
        parsed = px.parse_response(raw)
        if parsed is None:
            continue
        cmd, payload = parsed
        if cmd != command:
            continue
        if first_byte is not None and (not payload or payload[0] != first_byte):
            continue
        return payload
    return None


def _select_slot(dev, category: int, option: int) -> None:
    dev.write(px.build_report(
        px.CMD_PIXEL_SET,
        px.payload_scene_option(255, 255, 255, category, option)))
    time.sleep(0.2)


def set_scene(category: int, device=None) -> None:
    """Switch to a scene category (0=clock, 1=game, ... 8=spectrum)."""
    own = device is None
    dev = device or open_pixelbar_device()
    try:
        dev.write(px.build_report(
            px.CMD_PIXEL_SET,
            px.payload_scene_category(255, 255, 255, category)))
    finally:
        if own:
            dev.close()


def select_template(category: int, option: int, device=None) -> None:
    """Show template/slot ``option`` of ``category`` (vendor 0xEF 02 packet).

    Note: 0xEF 01 only selects the *upload target* (screen goes to "uploading"),
    while 0xEF 02 with template=<option> actually displays the slot.
    """
    own = device is None
    dev = device or open_pixelbar_device()
    try:
        dev.write(px.build_report(
            px.CMD_PIXEL_SET,
            px.payload_scene(255, 255, 255, category, option)))
        time.sleep(0.3)
    finally:
        if own:
            dev.close()


def _upload_blob(dev, data: bytes) -> None:
    size = len(data)
    dev.write(px.build_report(px.CMD_PIXEL_FILE,
                              px.payload_file_start(0, size)))
    reply = _read_state(dev, px.CMD_PIXEL_FILE, 0)
    if reply is None or len(reply) < 2:
        raise RuntimeError("no file-start ACK (TempoHub still open?)")
    chunk_size = 50
    if len(reply) > 3:
        requested = (reply[2] << 8) | reply[3]
        if 0 < requested < chunk_size:
            chunk_size = requested
    offset = 0
    index = 0
    retries = 0
    while offset < size:
        chunk = data[offset:offset + chunk_size]
        dev.write(px.build_report(px.CMD_PIXEL_FILE,
                                  px.payload_file_data(0, index, chunk)))
        reply = _read_state(dev, px.CMD_PIXEL_FILE, 0, timeout=5.0)
        if reply is None or len(reply) < 2:
            retries += 1
            if retries <= 3:
                print(f"   retry chunk {index} ({retries}/3)", flush=True)
                time.sleep(0.5 + 0.5 * retries)
                continue
            raise RuntimeError(f"no ACK for chunk {index}")
        retries = 0
        offset += len(chunk)
        index += 1
        state = reply[1]
        if state == px.STATE_NOTIFY_APP_CHECK_FINISH:
            return
        if state == px.STATE_NOTIFY_APP_SEND_NEXT_DATA:
            continue
        if state == px.STATE_NOTIFY_APP_CURSOR_ERROR:
            if len(reply) > 3:
                index = (reply[2] << 8) | reply[3]
                offset = index * chunk_size
            continue
        raise RuntimeError(f"transfer failed (state={state})")
    dev.write(px.build_report(px.CMD_PIXEL_FILE, px.payload_file_end(0)))
    while True:
        reply = _read_state(dev, px.CMD_PIXEL_FILE, 0, timeout=5.0)
        if reply is None or len(reply) < 2:
            raise RuntimeError("no final ACK")
        if reply[1] == px.STATE_NOTIFY_APP_CHECK_FINISH:
            return
        if reply[1] == px.STATE_NOTIFY_APP_SEND_NEXT_DATA:
            continue
        raise RuntimeError(f"final failed (state={reply[1]})")


def upload_canvases(canvases: list[list[int]], category: int,
                    option: int | None = None,
                    delay: int = DEFAULT_DELAY, shift: int = 0,
                    top_shift: int = DEFAULT_TOP_SHIFT, invert: bool = False,
                    msb: bool = False, switch: bool = True, settle: float = 2.0,
                    device=None) -> int:
    """Upload canvases as one animated slot and optionally switch to it.

    ``settle`` waits after the final ACK before switching: the device finishes
    its flash commit asynchronously, and switching too early shows "图片上传
    失败" (observed 2026-10-26 when uploading many slots back-to-back).

    Returns the number of frames uploaded.
    """
    if option is None:
        option = default_option(category)
    blob = encode_canvases(canvases, delay, shift, top_shift, invert, msb,
                           category)
    own = device is None
    dev = device or open_pixelbar_device()
    try:
        # Enter the target category first: the device only accepts the slot
        # selector (0xEF 01) for the scene it is currently showing (captured
        # TempoHub flow: 0xEF 02 <cat>, then 0xEF 01 <cat> <opt>).
        dev.write(px.build_report(
            px.CMD_PIXEL_SET,
            px.payload_scene_category(255, 255, 255, category)))
        time.sleep(0.5)
        _select_slot(dev, category, option)
        _upload_blob(dev, blob)
        if settle > 0:
            time.sleep(settle)
        if switch:
            dev.write(px.build_report(
                px.CMD_PIXEL_SET,
                px.payload_scene(255, 255, 255, category, option)))
            time.sleep(0.8)
    finally:
        if own:
            dev.close()
    return len(canvases)


def upload_frames(paths: list[Path], category: int, option: int,
                  safe_area: bool = False, **kwargs) -> int:
    """Upload PNG frames (one per path) as an animated slot."""
    canvases = [canvas_from_image(Path(p), safe_area=safe_area) for p in paths]
    return upload_canvases(canvases, category, option, **kwargs)


def upload_gif(path: Path, category: int, option: int, max_frames: int = 12,
               safe_area: bool = False, **kwargs) -> int:
    canvases = canvas_list_from_gif(path, max_frames, safe_area)
    return upload_canvases(canvases, category, option, **kwargs)


# --- CLI --------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import json

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--category", type=int, default=1)
    ap.add_argument("--option", type=int, default=None,
                    help="slot inside the category (default: per-category "
                         "captured slot; game 0-7)")
    ap.add_argument("--delay", type=int, default=DEFAULT_DELAY)
    ap.add_argument("--png", action="append", default=None)
    ap.add_argument("--gif", default=None)
    ap.add_argument("--max-frames", type=int, default=12)
    ap.add_argument("--pattern", choices=["dots", "grid", "demo", "cal-left",
                                          "cal-right", "cal-unit1"],
                    default=None)
    ap.add_argument("--safe-area", action="store_true",
                    help="fit content into x in [10,245]")
    ap.add_argument("--shift", type=int, default=0)
    ap.add_argument("--top-shift", type=int, default=DEFAULT_TOP_SHIFT)
    ap.add_argument("--msb", action="store_true")
    ap.add_argument("--invert", action="store_true")
    ap.add_argument("--no-switch", action="store_true")
    ap.add_argument("--select", type=int, default=None, metavar="OPT",
                    help="only select/show template OPT of --category, no upload")
    ap.add_argument("--settle", type=float, default=2.0,
                    help="seconds to wait after upload before switching")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    try:
        import psutil
        if not args.dry_run and any(
                "TempoHub" in (p.info["name"] or "")
                for p in psutil.process_iter(["name"])):
            print("先关闭 TempoHub（它独占 HID）", flush=True)
            return 2
    except ImportError:
        pass

    if args.select is not None:
        select_template(args.category, args.select)
        print(f"selected category {args.category} template {args.select}",
              flush=True)
        return 0

    if args.pattern:
        canvases = frame_pattern(args.pattern)
    elif args.gif:
        canvases = canvas_list_from_gif(Path(args.gif), args.max_frames,
                                        args.safe_area)
    elif args.png:
        canvases = [canvas_from_image(Path(p), args.safe_area)
                    for p in args.png]
    else:
        print("give --pattern, --gif or --png", flush=True)
        return 2

    blob = encode_canvases(canvases, args.delay, args.shift, args.top_shift,
                           args.invert, args.msb, args.category)
    print(f"frames={len(canvases)} bytes={len(blob)} -> "
          f"category={args.category} option={args.option} "
          f"delay={args.delay}ms", flush=True)

    if args.dry_run:
        out = ROOT / "logs" / f"slot_upload_{time.strftime('%H%M%S')}.bin"
        out.write_bytes(blob)
        print(json.dumps({"dry_run": True, "bin": str(out),
                          "frames": len(canvases), "bytes": len(blob)},
                         ensure_ascii=False), flush=True)
        return 0

    upload_canvases(canvases, args.category, args.option, args.delay,
                    args.shift, args.top_shift, args.invert, args.msb,
                    switch=not args.no_switch, settle=args.settle)
    print(f"uploaded {len(canvases)} frame(s) to category "
          f"{args.category} option {args.option}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
