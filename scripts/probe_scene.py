"""Switch PixelBar scenes (pure 0xEF 0x02 packets) -- E-plan recon.

IMPORTANT: frames starting with 0x01 enter the *upload* mode (firmware shows
"图片上传中"); pure scene switching uses 0x02 <R G B> 00 01 <category> FF FF.

Make sure TempoHub is CLOSED (it owns the HID device) and our stack is stopped
(`python scripts/stop_all.py`).

    .\\.venv\\Scripts\\python.exe scripts/probe_scene.py
    .\\.venv\\Scripts\\python.exe scripts/probe_scene.py --dwell 5
    .\\.venv\\Scripts\\python.exe scripts/probe_scene.py --category 6
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from render.sinks.pixelbar import open_pixelbar_device  # noqa: E402
from render.sinks.pixelbar import protocol as px  # noqa: E402

LABELS = ["时钟", "游戏", "打工", "读书", "猫咪", "狗狗", "热梗", "赛博", "频谱"]


def _open_device():
    return open_pixelbar_device()


def _send(dev, payload: list[int]) -> None:
    report = px.build_report(px.CMD_PIXEL_SET, payload)
    dev.write(report)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dwell", type=float, default=5.0)
    ap.add_argument("--category", type=int, default=None,
                    help="only this category (0-8); default sweeps all")
    ap.add_argument("--rgb", default="255,255,255")
    args = ap.parse_args(argv)

    try:
        import psutil
        if any("TempoHub" in (p.info["name"] or "")
               for p in psutil.process_iter(["name"])):
            print("TempoHub 正在运行：先关掉它（它独占 HID）", flush=True)
            return 2
    except ImportError:
        pass

    r, g, b = (int(v) for v in args.rgb.split(","))
    cats = [args.category] if args.category is not None else list(range(9))
    try:
        dev = _open_device()
    except Exception as exc:  # noqa: BLE001
        print(f"打不开设备：{exc}（TempoHub 关了？栈停了吗？）", flush=True)
        return 1

    sent = []
    try:
        for cat in cats:
            label = LABELS[cat] if 0 <= cat < len(LABELS) else "?"
            print(f"-> category {cat} （预期：{label}），保持 {args.dwell:.0f}s",
                  flush=True)
            _send(dev, px.payload_scene_category(r, g, b, cat))
            sent.append({"category": cat, "expected": label})
            time.sleep(args.dwell)
        print("-> 回到时钟（category 0）", flush=True)
        _send(dev, px.payload_scene_category(r, g, b, 0))
    finally:
        dev.close()

    out = ROOT / "logs" / f"scene_sweep_{time.strftime('%H%M%S')}.json"
    out.write_text(json.dumps({"rgb": [r, g, b], "sent": sent},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "log": str(out)}, ensure_ascii=False),
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

