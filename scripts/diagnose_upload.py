"""Verbose slot-upload diagnostic: shows every ACK so we can see why a
category rejects the image ("图片上传失败").

Flow (same as the vendor app):
    0xEF 02 <cat> FF FF        enter the category
    0xEE (query state)         optional read-back
    0xEF 01 <cat> <opt> FF     select the upload slot
    0x17 start/chunks/end      with every device reply printed

TempoHub must be CLOSED; our stack stopped (`scripts/stop_all.py`).

    # one category, watch the screen
    .\\.venv\\Scripts\\python.exe scripts/diagnose_upload.py --category 2 --option 3

    # try option 0..7 in one category (10s each)
    .\\.venv\\Scripts\\python.exe scripts/diagnose_upload.py --category 2 --sweep-options

    # try category 1..8 once each
    .\\.venv\\Scripts\\python.exe scripts/diagnose_upload.py --category 1 --all-categories
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
from scripts.upload_slot import canvas_from_image, encode_canvases, frame_pattern  # noqa: E402

DEFAULT_PNG = ROOT / "logs" / "wafer_normal.png"


def _dump(raw: bytes) -> str:
    parsed = px.parse_response(raw)
    if parsed is None:
        return f"raw={raw.hex()[:40]}"
    cmd, payload = parsed
    return f"cmd=0x{cmd:02X} payload={bytes(payload).hex()}"


def _wait(dev, label: str, timeout: float = 1.5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = dev.read(px.REPORT_LEN, timeout_ms=120)
        if raw:
            print(f"   <- {label}: {_dump(raw)}", flush=True)
            return
    print(f"   <- {label}: (no reply)", flush=True)


def _upload(dev, blob: bytes) -> bool:
    dev.write(px.build_report(px.CMD_PIXEL_FILE,
                              px.payload_file_start(0, len(blob))))
    _wait(dev, "start")
    offset = index = 0
    while offset < len(blob):
        chunk = blob[offset:offset + 50]
        dev.write(px.build_report(px.CMD_PIXEL_FILE,
                                  px.payload_file_data(0, index, chunk)))
        offset += len(chunk)
        index += 1
        raw = dev.read(px.REPORT_LEN, timeout_ms=500)
        parsed = px.parse_response(raw) if raw else None
        if parsed is None:
            print(f"   !! chunk {index}: no ACK", flush=True)
            return False
        state = parsed[1][1] if len(parsed[1]) > 1 else None
        if state not in (px.STATE_NOTIFY_APP_SEND_NEXT_DATA,
                         px.STATE_NOTIFY_APP_CHECK_FINISH):
            print(f"   !! chunk {index}: state={state} "
                  f"payload={bytes(parsed[1]).hex()}", flush=True)
            return False
    dev.write(px.build_report(px.CMD_PIXEL_FILE, px.payload_file_end(0)))
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        raw = dev.read(px.REPORT_LEN, timeout_ms=200)
        if not raw:
            continue
        parsed = px.parse_response(raw)
        if parsed is None or parsed[0] != px.CMD_PIXEL_FILE:
            continue
        payload = parsed[1]
        print(f"   <- end: payload={bytes(payload).hex()}", flush=True)
        if len(payload) > 1 and payload[1] == px.STATE_NOTIFY_APP_CHECK_FINISH:
            return True
        return False
    print("   !!! end: no reply (commit rejected)", flush=True)
    return False


def run_slot(dev, category: int, option: int, blob: bytes) -> None:
    print(f"\n=== category {category} option {option} ===", flush=True)
    print("   -> 0xEF 02 (enter category)", flush=True)
    dev.write(px.build_report(
        px.CMD_PIXEL_SET, px.payload_scene_category(255, 255, 255, category)))
    _wait(dev, "enter")
    time.sleep(0.6)

    print("   -> 0xEE (state query)", flush=True)
    dev.write(px.build_report(px.CMD_PIXEL_STATE, []))
    _wait(dev, "state")

    print("   -> 0xEF 01 (select upload slot)", flush=True)
    dev.write(px.build_report(
        px.CMD_PIXEL_SET,
        px.payload_scene_option(255, 255, 255, category, option)))
    _wait(dev, "select")
    time.sleep(0.3)

    print("   -> 0x17 upload", flush=True)
    ok = _upload(dev, blob)
    print(f"   upload {'OK' if ok else 'FAILED'}", flush=True)
    time.sleep(2.5)

    print("   -> 0xEF 02 (switch back to show it)", flush=True)
    dev.write(px.build_report(
        px.CMD_PIXEL_SET, px.payload_scene_category(255, 255, 255, category)))
    _wait(dev, "switch")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--category", type=int, default=2)
    ap.add_argument("--option", type=int, default=3)
    ap.add_argument("--png", default=str(DEFAULT_PNG))
    ap.add_argument("--sweep-options", action="store_true")
    ap.add_argument("--all-categories", action="store_true")
    ap.add_argument("--hold", type=float, default=8.0)
    args = ap.parse_args(argv)

    path = Path(args.png)
    canvas = (canvas_from_image(path) if path.exists()
              else frame_pattern("dots")[0])
    blob = encode_canvases([canvas])

    dev = open_pixelbar_device()
    try:
        cats = ([args.category] if not args.all_categories else list(range(1, 9)))
        opts = ([args.option] if not args.sweep_options else list(range(8)))
        for cat in cats:
            for opt in opts:
                run_slot(dev, cat, opt, blob)
                print(f"   [look at the panel for {args.hold:.0f}s]", flush=True)
                time.sleep(args.hold)
    finally:
        dev.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
