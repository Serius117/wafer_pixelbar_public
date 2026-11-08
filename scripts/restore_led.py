"""Best-effort LED teardown: put the PixelBar back on its native clock.

Used by ``stop_all.py`` after the render process is killed, because Windows
TerminateProcess does not run Python ``finally`` blocks.  Also safe to run
manually when the last expression is stuck.

    python scripts/restore_led.py
    python scripts/restore_led.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def restore() -> dict:
    result: dict[str, object] = {"text": False, "slot": False}

    try:
        from render.sinks.pixelbar.text import PixelBarTextSink

        sink = PixelBarTextSink()
        sink.open()
        try:
            result["text"] = bool(sink.restore_clock())
        finally:
            sink.close()
    except Exception as exc:  # noqa: BLE001 -- teardown must not raise
        result["text_error"] = str(exc)[:160]

    try:
        from render.slotface import _PixelBarSlotSink

        sink = _PixelBarSlotSink()
        sink.open()
        try:
            sink.restore_clock()
            result["slot"] = True
        finally:
            sink.close()
    except Exception as exc:  # noqa: BLE001 -- teardown must not raise
        result["slot_error"] = str(exc)[:160]

    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    result = restore()
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif result["text"] or result["slot"]:
        print("LED clock restored")
    else:
        print("LED clock not restored (device absent or HID busy)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
