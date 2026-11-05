"""Guided mic capture -> OLV end-to-end check (stage 4, hands-off after speech).

The script tells you exactly what to say, records it the way the browser does
(WASAPI), then pushes the same wav through the OLV websocket and prints the
verdict (transcript / gate / reply / audio frames). No further interaction.

    .\\.venv\\Scripts\\python.exe scripts/record_utterance.py
    .\\.venv\\Scripts\\python.exe scripts/record_utterance.py --seconds 6
    .\\.venv\\Scripts\\python.exe scripts/record_utterance.py --no-push   # record only

Artifacts: logs/utterance_<ts>.wav (native) + logs/utterance_<ts>16k.wav.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

ROOT = Path(__file__).resolve().parents[1]
WS_DEFAULT = "ws://127.0.0.1:12393/client-ws"
WAKE_CLIP = ROOT / "tests" / "fixtures" / "corpus" / "master_wake.wav"


def _stdout_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def pick_device() -> tuple[int, dict]:
    hostapis = sd.query_hostapis()
    devices = sd.query_devices()
    wasapi = next((i for i, api in enumerate(hostapis)
                   if "WASAPI" in api["name"]), None)
    candidates = []
    for idx, dev in enumerate(devices):
        if dev["max_input_channels"] <= 0:
            continue
        if "PixelBar" not in dev["name"] and "Halo" not in dev["name"]:
            continue
        if wasapi is not None and dev["hostapi"] != wasapi:
            continue
        candidates.append((idx, dev))
    if not candidates:
        default = sd.query_devices(kind="input")
        idx = int(default["index"])
        return idx, devices[idx]
    candidates.sort(key=lambda pair: abs(pair[1]["default_samplerate"] - 48000))
    return candidates[0]


def dbfs(block: np.ndarray) -> float:
    data = block.astype(np.float64)
    rms = math.sqrt(float(np.mean(data * data))) if data.size else 0.0
    return 20 * math.log10(rms / 32768.0) if rms > 0 else -120.0


def record(device: int, rate: int, channels: int, seconds: float,
           say: str) -> np.ndarray:
    print("=" * 60, flush=True)
    print("请说：", flush=True)
    print(f"    「{say}」", flush=True)
    print("=" * 60, flush=True)
    for left in (3, 2, 1):
        print(f"  {left}...", flush=True)
        time.sleep(1)
    print("  ● 录音中，开始说吧", flush=True)
    audio = sd.rec(int(seconds * rate), samplerate=rate, channels=channels,
                   dtype="int16", device=device)
    sd.wait()
    print("  ■ 结束", flush=True)
    return audio


def to16k(data: np.ndarray, rate: int) -> np.ndarray:
    if data.ndim > 1:
        energy = np.mean(data.astype(np.float64) ** 2, axis=0)
        mono = data[:, int(np.argmax(energy))]
    else:
        mono = data
    if rate == 16000:
        return mono.astype(np.int16)
    n_out = int(round(mono.size * 16000 / rate))
    x_out = np.linspace(0, mono.size - 1, n_out)
    return np.interp(x_out, np.arange(mono.size), mono).astype(np.int16)


def write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.astype(np.int16).tobytes())


def chunks(path: Path) -> list[list[float]]:
    with wave.open(str(path)) as handle:
        pcm = handle.readframes(handle.getnframes())
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    return [samples[i:i + 4096].tolist() for i in range(0, len(samples), 4096)]


async def push(ws_url: str, wav16: Path, wake: bool, timeout: float) -> dict:
    import websockets

    events: list[str] = []
    timeline: list[dict] = []
    reply: list[str] = []
    audio = 0
    transcript = ""
    sent_end = 0.0
    async with websockets.connect(ws_url, max_size=2 ** 22) as ws:
        await asyncio.sleep(2)
        if wake and WAKE_CLIP.exists():
            for chunk in chunks(WAKE_CLIP):
                await ws.send(json.dumps({"type": "mic-audio-data",
                                          "audio": chunk}))
            await ws.send(json.dumps({"type": "mic-audio-end"}))
            print("[wake] 已发送语料唤醒片段，等 4s", flush=True)
            await asyncio.sleep(4)
        for chunk in chunks(wav16):
            await ws.send(json.dumps({"type": "mic-audio-data",
                                      "audio": chunk}))
        await ws.send(json.dumps({"type": "mic-audio-end"}))
        began = time.monotonic()
        sent_end = time.time()
        while time.monotonic() - began < timeout:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except TimeoutError:
                if audio and reply:
                    break
                continue
            if isinstance(raw, (bytes, bytearray)):
                audio += 1
                timeline.append({"t": round(time.monotonic() - began, 3),
                                 "wall": round(time.time(), 3),
                                 "type": "binary-audio", "bytes": len(raw)})
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            kind = msg.get("type")
            events.append(kind)
            timeline.append({"t": round(time.monotonic() - began, 3),
                             "wall": round(time.time(), 3), "type": kind})
            print(f"[{time.monotonic() - began:5.1f}s] {kind}", flush=True)
            if kind == "user-input-transcription":
                transcript = msg.get("text", "")
                print(f"  TRANSCRIPT: {transcript}", flush=True)
            if kind == "audio":
                audio += 1
            display = msg.get("display_text")
            if isinstance(display, dict) and display.get("text"):
                reply.append(str(display["text"]))
            if kind == "error":
                print(f"  ERROR: {msg}", flush=True)
    verdict = {
        "ok": bool(transcript) and bool(audio),
        "transcript": transcript,
        "reply": "".join(reply)[:120],
        "audio_frames": audio,
        "sent_end_wall": round(sent_end, 3),
        "timeline": timeline,
        "event_types": sorted(set(events)),
    }
    return verdict


def main(argv: list[str] | None = None) -> int:
    _stdout_utf8()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--say", default="小饼小饼，今天天气怎么样？")
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--ws", default=WS_DEFAULT)
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--no-wake", action="store_true",
                    help="skip the corpus wake clip (session must be awake)")
    ap.add_argument("--from-wav", default=None,
                    help="skip recording and push this 16k mono wav instead")
    ap.add_argument("--save-json", default=None,
                    help="write the verdict (with timeline) to this JSON file")
    ap.add_argument("--timeout", type=float, default=90.0)
    args = ap.parse_args(argv)

    if args.from_wav:
        verdict = asyncio.run(push(args.ws, Path(args.from_wav),
                                   not args.no_wake, args.timeout))
        if args.save_json:
            Path(args.save_json).write_text(
                json.dumps(verdict, ensure_ascii=False, indent=2),
                encoding="utf-8")
        print(json.dumps(verdict, ensure_ascii=False, indent=2), flush=True)
        return 0 if verdict["ok"] else 1

    idx, dev = pick_device()
    rate = int(dev["default_samplerate"])
    channels = max(1, int(dev["max_input_channels"]))
    print(f"device: [{idx}] {dev['name']}  {rate} Hz  "
          f"{channels} channel(s)  WASAPI", flush=True)
    audio = record(idx, rate, channels, args.seconds, args.say)

    stamp = time.strftime("%H%M%S")
    out = ROOT / "logs" / f"utterance_{stamp}.wav"
    out16 = ROOT / "logs" / f"utterance_{stamp}16k.wav"
    write_wav(out, audio, rate)
    samples16 = to16k(audio, rate)
    write_wav(out16, samples16, 16000)

    peak = float(np.max(np.abs(audio))) / 32768.0
    rms = float(np.sqrt(np.mean((audio.astype(np.float64) / 32768.0) ** 2)))
    per_sec = [round(dbfs(audio[int(i * rate):int((i + 1) * rate)]), 1)
               for i in range(int(args.seconds))]
    stats = {"wav": str(out), "peak": round(peak, 3),
             "rms_dbfs": round(20 * math.log10(rms) if rms > 0 else -120, 1),
             "per_sec_dbfs": per_sec}
    print(json.dumps(stats, ensure_ascii=False), flush=True)
    if peak <= 0.05:
        print("采集失败：峰值过低，没录到声音（检查麦克风/静音键）", flush=True)
        return 1
    if args.no_push:
        return 0

    verdict = asyncio.run(push(args.ws, out16, not args.no_wake, args.timeout))
    verdict["speech"] = stats
    if args.save_json:
        Path(args.save_json).write_text(
            json.dumps(verdict, ensure_ascii=False, indent=2),
            encoding="utf-8")
    print(json.dumps(verdict, ensure_ascii=False, indent=2), flush=True)
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
