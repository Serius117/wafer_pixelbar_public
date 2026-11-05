r"""Probe every PixelBar input endpoint and channel with the same utterance.

Why: Windows exposes the same USB microphone through MME, DirectSound, WASAPI
and WDM-KS, with different channel counts. A wrong endpoint or channel looks
exactly like "the mic is silent". This script records each endpoint separately
and reports per-channel peak/RMS, so we can pick the working path instead of
guessing.

Usage:
  .\.venv\Scripts\python.exe scripts/diagnose_mic.py --list
  .\.venv\Scripts\python.exe scripts\diagnose_mic.py --seconds 3
"""

from __future__ import annotations

import argparse
import math
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs" / "mic_probe"


def pixelbar_devices() -> list[tuple[int, dict]]:
    import sounddevice as sd

    out = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] <= 0:
            continue
        name = str(dev["name"])
        if "PixelBar" in name or "Halo" in name:
            out.append((idx, dev))
    return out


def _db(rms: float) -> float:
    return 20 * math.log10(max(rms, 1e-12))


def _record_one(idx: int, dev: dict, seconds: float) -> list[dict]:
    import sounddevice as sd

    rate = int(dev["default_samplerate"])
    channels = max(1, int(dev["max_input_channels"]))
    print("\n" + "=" * 64, flush=True)
    print(f"端点 index={idx}  hostapi={dev['hostapi']}  "
          f"channels={channels}  rate={rate}", flush=True)
    print(f"请说：小饼小饼（录 {seconds:.1f} 秒，看到“开始”后立刻说）",
          flush=True)
    for left in (3, 2, 1):
        print(f"  {left}...", flush=True)
        time.sleep(1)
    print("  开始。", flush=True)
    audio = sd.rec(int(seconds * rate), samplerate=rate, channels=channels,
                   dtype="int16", device=idx)
    sd.wait()
    print("  结束。", flush=True)
    data = np.asarray(audio)
    if data.ndim == 1:
        data = data.reshape(-1, 1)
    rows = []
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    for ch in range(data.shape[1]):
        pcm = data[:, ch]
        peak = float(np.max(np.abs(pcm))) / 32768.0
        rms = float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2))) / 32768.0
        path = LOG_DIR / f"idx{idx}_ch{ch}.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(pcm.astype("<i2").tobytes())
        rows.append({"channel": ch, "peak": round(peak, 4),
                     "rms_dbfs": round(_db(rms), 1), "wav": str(path)})
        print(f"  ch{ch}: peak={peak:.4f}  rms={_db(rms):6.1f} dBFS  "
              f"-> {path}", flush=True)
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)

    import sounddevice as sd

    devices = pixelbar_devices()
    print("host API:", {i: a["name"] for i, a in
                        enumerate(sd.query_hostapis())})
    print("PixelBar input endpoints:")
    for idx, dev in devices:
        print(f"  idx={idx} hostapi={dev['hostapi']} "
              f"channels={dev['max_input_channels']} "
              f"rate={dev['default_samplerate']:.0f} {dev['name']!r}")
    if args.list:
        return 0
    if not devices:
        print("没有找到 PixelBar 输入端点。", file=sys.stderr)
        return 2

    results = {}
    for idx, dev in devices:
        try:
            results[idx] = _record_one(idx, dev, args.seconds)
        except Exception as exc:  # noqa: BLE001 -- keep probing others
            print(f"  idx={idx} FAILED: {exc}", file=sys.stderr, flush=True)
            results[idx] = []

    print("\n" + "=" * 64)
    best = None
    for idx, rows in results.items():
        if not rows:
            continue
        for row in rows:
            score = (row["peak"], row["rms_dbfs"])
            if best is None or score > best[0]:
                best = (score, idx, row)
    if best:
        _, idx, row = best
        print(f"最佳：idx={idx} ch={row['channel']} peak={row['peak']} "
              f"rms={row['rms_dbfs']} dBFS")
    else:
        print("所有端点都没有可用电平。下一步应走 OLV 浏览器 getUserMedia。")
    print(f"证据目录：{LOG_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
