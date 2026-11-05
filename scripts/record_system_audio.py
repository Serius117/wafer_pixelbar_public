"""Record whatever Windows is playing (WASAPI loopback) to a wav file.

Useful for saving audio that a website only plays (no download button).
Plays nothing itself -- start it, then play the clip in the browser.

    # default: 10s, default speakers, 48k stereo
    .\\.venv\\Scripts\\python.exe scripts/record_system_audio.py

    # list endpoints, then pick one explicitly
    .\\.venv\\Scripts\\python.exe scripts/record_system_audio.py --list
    .\\.venv\\Scripts\\python.exe scripts/record_system_audio.py --seconds 15 --device 1

Output: logs/system_audio_<HHMMSS>.wav + JSON stats.
Requires Windows (WASAPI loopback via the `soundcard` package).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "logs"


def _speakers():
    import soundcard as sc

    return sc.all_speakers()


def list_devices() -> int:
    default = None
    try:
        import soundcard as sc

        default = sc.default_speaker()
    except Exception:  # noqa: BLE001
        pass
    print(f"{'idx':>3}  name")
    for idx, spk in enumerate(_speakers()):
        mark = " (default)" if default is not None and spk.id == default.id else ""
        print(f"{idx:>3}  {spk.name}{mark}")
    return 0


def dbfs(data) -> float:
    import numpy as np

    if data.size == 0:
        return -120.0
    rms = math.sqrt(float(np.mean(np.square(data, dtype=np.float64))))
    return 20 * math.log10(rms) if rms > 0 else -120.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--device", type=int, default=None,
                    help="speaker index from --list (default: system default)")
    ap.add_argument("--rate", type=int, default=48000)
    ap.add_argument("--channels", type=int, default=2)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    if args.list:
        return list_devices()

    try:
        import numpy as np
        import soundcard as sc
    except ImportError as exc:
        print(json.dumps({"error": f"missing package: {exc}"}), flush=True)
        return 1

    speakers = sc.all_speakers()
    if not speakers:
        print(json.dumps({"error": "no speakers found"}), flush=True)
        return 1
    if args.device is not None:
        if not 0 <= args.device < len(speakers):
            print(json.dumps({"error": f"--device out of range 0..{len(speakers) - 1}"}),
                  flush=True)
            return 1
        speaker = speakers[args.device]
    else:
        speaker = sc.default_speaker() or speakers[0]

    mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
    if mic is None:
        print(json.dumps({"error": f"no loopback device for {speaker.name}"}),
              flush=True)
        return 1

    out = Path(args.out) if args.out else (
        LOGS / f"system_audio_{time.strftime('%H%M%S')}.wav")
    LOGS.mkdir(exist_ok=True)

    print(f"loopback: {speaker.name}  {args.rate} Hz  {args.channels} ch",
          flush=True)
    print(f"现在去网页上播放音频（{args.seconds:.0f}s 后自动结束）…", flush=True)
    with mic.recorder(samplerate=args.rate, channels=args.channels) as rec:
        data = rec.record(numframes=int(args.seconds * args.rate))
    sd = np.asarray(data, dtype=np.float32)
    pcm = (np.clip(sd, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(str(out), "wb") as handle:
        handle.setnchannels(sd.shape[1] if sd.ndim > 1 else 1)
        handle.setsampwidth(2)
        handle.setframerate(args.rate)
        handle.writeframes(pcm.tobytes())

    mono = sd.mean(axis=1) if sd.ndim > 1 else sd
    per_sec = [round(dbfs(mono[int(s * args.rate):int((s + 1) * args.rate)]), 1)
               for s in range(int(args.seconds))]
    peak = float(np.max(np.abs(sd))) if sd.size else 0.0
    stats = {"wav": str(out), "seconds": args.seconds, "rate": args.rate,
             "channels": int(pcm.shape[1]) if pcm.ndim > 1 else 1,
             "peak": round(peak, 3), "rms_dbfs": round(dbfs(mono), 1),
             "per_sec_dbfs": per_sec}
    print(json.dumps(stats, ensure_ascii=False, indent=2), flush=True)
    if peak <= 0.01:
        print("警告：几乎无声音（网页静音？系统输出不是所选端点？）", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
