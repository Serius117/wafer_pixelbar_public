"""Home acceptance test: verifies hardware touchpoints on the real machine.

Run at home after `git pull`, with TempoHub closed (HID is exclusive).

Checks
------
1. HID device detection      -- PID-agnostic (0xA106 / 0xA160), control iface
2. LED text channel          -- 0xE8 push + ACK, then restore the clock
3. Mood light channel        -- 0x6A query + 0x6B write ACK
4. Audio output device       -- PixelBar speaker present on the system
5. Microphone + level        -- default input, 3s recording, RMS in dBFS
6. LLM server                -- Ollama :11434 or llama-server :8080
7. OLV audio server          -- :12393

Usage: .venv\\Scripts\\python.exe scripts\\accept_home.py [--mic-seconds 3]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

RESULTS: list[dict] = []
MIC_SECONDS = 3.0
SAMPLE_RATE = 16000


class Warn(Exception):
    """Non-fatal acceptance problem (reported as WARN, exit code unaffected)."""


def check(name: str, func) -> None:
    print(f"\n{'=' * 50}\nCHECK: {name}\n{'=' * 50}")
    try:
        detail = func()
        RESULTS.append({"name": name, "status": "PASS", "detail": str(detail)})
        print(f"  PASS: {detail}")
    except Warn as w:
        RESULTS.append({"name": name, "status": "WARN", "detail": str(w)})
        print(f"  WARN: {w}")
    except Exception as e:  # noqa: BLE001 -- acceptance must report, not crash
        RESULTS.append({"name": name, "status": "FAIL", "detail": str(e)})
        print(f"  FAIL: {e}")


# --- HID -------------------------------------------------------------------

def check_hid_device() -> str:
    """PixelBar HID present, control collection discoverable (any PID)."""
    import hid

    from render.sinks.pixelbar import discover_pixelbar

    target = discover_pixelbar(hid, 0x2D99, None)
    devices = [d for d in hid.enumerate() if d["vendor_id"] == 0x2D99]
    return (f"PID 0x{target.get('product_id', 0):04X}, "
            f"interface {target.get('interface_number', '?')}, "
            f"usage_page 0x{target.get('usage_page', 0):04X} "
            f"({len(devices)} HID path(s))")


def check_led_text() -> str:
    """Push text over 0xE8 and put the native clock back afterwards."""
    from render.sinks.pixelbar.text import PixelBarTextSink

    sink = PixelBarTextSink()
    sink.open()
    try:
        ok = sink.show_text("WAFER OK", (120, 200, 255))
        if not ok:
            raise RuntimeError("text push not acknowledged")
        time.sleep(2.0)
        restored = sink.restore_clock()
        if not restored:
            raise RuntimeError("clock restore not acknowledged")
        return "text acknowledged, clock restored"
    finally:
        sink.close()


def check_mood_light() -> str:
    """0x6A query + one idempotent 0x6B write (master switch on)."""
    from render.sinks.pixelbar.mood import PixelBarMoodSink

    sink = PixelBarMoodSink()
    sink.open()
    try:
        state = sink.mood_state()
        if not state:
            raise RuntimeError("0x6A query got no reply")
        if not sink.set_mood_switch(True):
            raise RuntimeError("0x6B write not acknowledged")
        zones = len(state.get("groups", []))
        return (f"light_array_index=0x{state['light_array_index']:02X}, "
                f"{zones} zones, switch write ACKed")
    finally:
        sink.close()


# --- audio -----------------------------------------------------------------

def _audio_names(kind: str) -> list[str]:
    import sounddevice as sd

    key = "max_output_channels" if kind == "output" else "max_input_channels"
    return [d["name"] for d in sd.query_devices() if d[key] > 0]


def check_audio_output() -> str:
    names = _audio_names("output")
    import sounddevice as sd

    default = sd.query_devices(kind="output")["name"]
    hits = [n for n in names if any(k in n.lower()
                                   for k in ("pixelbar", "halo", "edifier"))]
    if not hits:
        raise RuntimeError(
            f"PixelBar speaker not found. Default output: {default}; "
            f"outputs: {names}")
    return f"default output: {default}; PixelBar: {hits[0]}"


def check_microphone() -> str:
    import numpy as np
    import sounddevice as sd

    from scripts.enroll import pick_mic_device, record_sample

    names = _audio_names("input")
    device = pick_mic_device()
    default = (sd.query_devices(device)["name"] if device is not None
               else sd.query_devices(kind="input")["name"])
    print(f"  输入设备：{default}（{'WASAPI' if device is not None else '默认'}）",
          flush=True)
    print("  接下来测试麦克风：请说一句自然中文，例如「小饼小饼，测试一二三」。",
          flush=True)
    for left in (3, 2, 1):
        print(f"  {left}...", flush=True)
        time.sleep(1)
    print(f"  现在可以开始说话了：录音 {MIC_SECONDS:.0f} 秒，说完不用按键，自动结束。",
          flush=True)
    pcm = record_sample(MIC_SECONDS, sample_rate=SAMPLE_RATE, device=device)
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0
    rms = math.sqrt(float(np.mean(samples ** 2))) if samples.size else 0.0
    dbfs = 20 * math.log10(rms + 1e-12)
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    detail = (f"capture mic: {default} ({len(names)} input(s)); "
              f"rms {dbfs:.1f} dBFS, peak {peak:.3f}")
    if peak < 0.005:
        raise Warn(f"{detail}（几乎静音：检查麦克风，或离音箱近一点、说大声一点）")
    if peak >= 0.99:
        print("  提示：峰值接近 1.0（削波），请把 Windows 输入音量调低到约 70–80 再测。",
              flush=True)
    return detail


# --- services --------------------------------------------------------------

def check_llm_server() -> str:
    import httpx

    endpoints = (
        ("ollama", "http://127.0.0.1:11434/api/tags"),
        ("llama-server", "http://127.0.0.1:8080/v1/models"),
    )
    reachable = []
    for name, url in endpoints:
        try:
            resp = httpx.get(url, timeout=3.0)
            if resp.status_code == 200:
                body = resp.json()
                models = body.get("models", []) if name == "ollama" else \
                    body.get("data", [])
                reachable.append(f"{name} ({len(models)} model(s))")
        except Exception:  # noqa: BLE001 -- probing multiple endpoints
            continue
    if not reachable:
        raise RuntimeError(
            "no local LLM server on :11434 (ollama) or :8080 (llama-server); "
            "see TASKS R.12")
    return "; ".join(reachable)


def check_olv_server() -> str:
    import httpx

    resp = httpx.get("http://127.0.0.1:12393/", timeout=3.0)
    return f"OLV responding (status={resp.status_code})"


def main(argv: list[str] | None = None) -> int:
    global MIC_SECONDS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mic-seconds", type=float, default=3.0)
    args = parser.parse_args(argv)
    MIC_SECONDS = args.mic_seconds

    print("=" * 60)
    print("wafer-pixel 家机验收测试")
    print("开始前请退出 EDIFIER TempoHub（HID 独占）。")
    print(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    check("HID Device Detection", check_hid_device)
    check("LED Text Channel (0xE8)", check_led_text)
    check("Mood Light Channel (0x6A/0x6B)", check_mood_light)
    check("Audio Output Device", check_audio_output)
    check("Microphone + Level", check_microphone)
    check("Local LLM Server", check_llm_server)
    check("OLV Audio Server", check_olv_server)

    passed = sum(1 for r in RESULTS if r["status"] == "PASS")
    warned = sum(1 for r in RESULTS if r["status"] == "WARN")
    failed = sum(1 for r in RESULTS if r["status"] == "FAIL")

    print(f"\n{'=' * 60}")
    print(f"汇总：{passed} 通过 / {warned} 警告 / {failed} 失败 / 共 {len(RESULTS)} 项")
    print("=" * 60)
    for r in RESULTS:
        print(f"  [{r['status']:<4}] {r['name']}: {r['detail'][:100]}")
    if failed:
        print("结论：有失败项，先按上面的 FAIL 排查，不要继续后续语音测试。")
    elif warned:
        print("结论：基本通过；WARN 项按提示补测（例如麦克风音量/静音）。")
    else:
        print("结论：全部通过，可以继续语音/唤醒测试。")

    report_path = Path(__file__).parent.parent / "logs" / "accept_home_report.json"
    report_path.parent.mkdir(exist_ok=True)
    report_path.write_text(json.dumps(
        {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
         "results": RESULTS}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  报告已保存：{report_path}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
