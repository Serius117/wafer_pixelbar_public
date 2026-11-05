"""E2E real-engine smoke (W.11): synthesized speech -> real ASR
(faster-whisper CPU) -> real routing -> real LLM (OpenAI-compatible API,
default: Volcengine Ark deepseek-v4.1-flash via the opencode provider
config) -> assertions.

Validates the whole cognitive chain with zero hardware: the only simulated
pieces are the voiceprint gate (ScriptedVerifier) and TTS (CaptureTts).

Prerequisites (dev/home machine):
  - test audio:  temp_script/e2e_audio/*.wav  (gen: gen_voice.ps1 pattern)
  - ASR model:   models/asr/faster-whisper-small/  (hf-mirror download)
  - LLM key:     opencode config (provider.huoshan) or WAFER_LLM_* env

Run: .venv\\Scripts\\python.exe scripts\\e2e_real.py [--audio-dir DIR]
Exit 0 = all scenarios PASS. Prints a JSON report with latency numbers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_AUDIO_DIR = ROOT / "temp_script" / "e2e_audio"
DEFAULT_ASR_MODEL = ROOT / "models" / "asr" / "faster-whisper-small"
DEFAULT_QWEN_URL = "http://127.0.0.1:18001"
REPORT: dict = {"scenarios": [], "pass": False}

# qwen engine prerequisite -- start llama-server first:
#   llama-server -m models/asr/qwen3-asr-1.7b-q4/Qwen3-ASR-1.7B-Q4_K_M.gguf \
#       --mmproj models/asr/qwen3-asr-1.7b-q4/mmproj-Qwen3-ASR-1.7B-Q8_0.gguf \
#       --port 18001 --alias qwen3-asr -c 2048 -ngl 0


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise RuntimeError(f"{path.name}: expected 16k mono s16")
        return w.readframes(w.getnframes())


def check(name: str, ok: bool, detail: str = "") -> bool:
    REPORT["scenarios"].append({"name": name, "ok": ok, "detail": detail})
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")
    return ok


async def main() -> int:
    parser = argparse.ArgumentParser(description="real-engine E2E smoke")
    parser.add_argument("--audio-dir", type=Path, default=DEFAULT_AUDIO_DIR)
    parser.add_argument("--asr-engine", choices=["qwen", "whisper"], default="qwen",
                        help="qwen: llama-server + Qwen3-ASR (default); "
                             "whisper: faster-whisper local")
    parser.add_argument("--qwen-url", default=DEFAULT_QWEN_URL,
                        help="llama-server base URL (qwen engine)")
    parser.add_argument("--asr-model", type=Path, default=DEFAULT_ASR_MODEL,
                        help="faster-whisper model dir (whisper engine)")
    parser.add_argument("--json-out", type=Path, default=None,
                        help="also write the JSON report to this file")
    parser.add_argument("--with-opencode", action="store_true",
                        help="scenario 4: dispatch into the REAL opencode CLI "
                             "(dev machine with opencode installed; costs one "
                             "flash-model query)")
    args = parser.parse_args()

    # --- prerequisites ---------------------------------------------------
    from engines.asr import FasterWhisperAsr
    from engines.llm_api import OpenAiCompatLlm

    audio_dir: Path = args.audio_dir
    wav = {p.stem: read_wav(p) for p in sorted(audio_dir.glob("*.wav"))}
    if not wav:
        print(f"no audio files in {audio_dir} -- generate them first")
        return 1

    if args.asr_engine == "qwen":
        from engines.asr_qwen import QwenLlamaServerAsr

        asr = QwenLlamaServerAsr(
            url=args.qwen_url,
            hotwords=["派活", "小饼", "按钮", "登录"],
        )
        REPORT["asr_engine"] = "qwen3-asr-1.7b-q4 (llama-server)"
        print(f"ASR engine: qwen3-asr @ {args.qwen_url}")
    else:
        asr = FasterWhisperAsr(model_path=args.asr_model)
        REPORT["asr_engine"] = "faster-whisper-small (local)"
        print("ASR engine: faster-whisper-small")
    llm = OpenAiCompatLlm()  # resolves key from opencode config at runtime

    # --- scenario 1: real ASR transcribes the synthetic speech ------------
    print("\n[1] real ASR transcription")
    transcripts: dict[str, str] = {}
    asr_started = time.monotonic()
    for key, pcm in wav.items():
        text = await asr.transcribe(pcm)
        transcripts[key] = text
        print(f"    {key}: {text!r}  ({asr.last_transcribe_s:.2f}s)")
    REPORT["asr_total_s"] = round(time.monotonic() - asr_started, 2)
    REPORT["asr_per_call_s"] = round(REPORT["asr_total_s"] / len(wav), 2)

    # keyword assertions: transcription must be close enough to route on
    expect = {
        "chat_tired": ["累"],
        "task_dispatch": ["派活"],
        "task_amend": ["不对"],
        "task_cancel": ["算", "别"],
    }
    asr_ok = True
    for key, keywords in expect.items():
        hit = any(kw in transcripts.get(key, "") for kw in keywords)
        asr_ok = check(f"asr:{key}", hit, transcripts.get(key, "")) and asr_ok

    # --- scenario 2: real LLM answers with an emotion tag -----------------
    print("\n[2] real LLM chat turn")
    from core.bus.hub import InProcBus
    from core.gate.policy import GateConfig, SpeakerProfile
    from core.interfaces import Route
    from core.pipeline import ConversationPipeline, parse_emotion_tag
    from core.sim import CaptureTts, ScriptedVerifier

    bus = InProcBus()
    tts = CaptureTts()
    pipeline = ConversationPipeline(
        source=None,
        verifier=ScriptedVerifier([{"master": 0.75}]),
        asr=asr,
        llm=llm,
        tts=tts,
        bus=bus,
        profiles={
            "master": SpeakerProfile(
                speaker_id="master", name="主人", role="admin", priority=1
            )
        },
        gate_config=GateConfig(t_pass=0.62, t_gray=0.55),
    )
    # NOTE: production system prompt asks for the [emotion:xxx] prefix;
    # the pipeline's default is bare -- patch it for this E2E like WaferAgent does
    pipeline_llm_ok = True

    turn_started = time.monotonic()
    chat_route = None
    chat_first_token: float | None = None
    for attempt in range(3):  # transport drops happen; retry the turn
        chat_route = await pipeline.run_turn(wav["chat_tired"])
        if chat_route is not None:
            chat_first_token = llm.last_first_token_s  # THIS turn's token
            break
        print(f"    attempt {attempt + 1} degraded (rule 6), retrying...")
    chat_elapsed = time.monotonic() - turn_started
    llm_first = chat_first_token
    print(f"    route={chat_route} first_token={llm_first}s total={chat_elapsed:.2f}s")
    print(f"    tts.captured={tts.captured!r}")
    REPORT["chat"] = {
        "route": str(chat_route),
        "first_token_s": round(llm_first, 2) if llm_first else None,
        "total_s": round(chat_elapsed, 2),
        "reply": tts.captured[-1] if tts.captured else None,
    }

    pipeline_llm_ok = check("chat:route_is_chat", chat_route == Route.CHAT)
    pipeline_llm_ok = check(
        "chat:reply_spoken", bool(tts.captured) and len(tts.captured[-1]) > 0
    ) and pipeline_llm_ok

    # emotion tag: the default system prompt doesn't force it; probe directly
    from core.interfaces import Message

    reply = await llm.complete([
        Message(role="system", content=(
            "你是一个像素机器人助手。每次回复必须以 [emotion:情绪] 开头，"
            "情绪词从 happy/sad/angry/surprised/normal 里选，然后说一句话，20字以内。"
        )),
        Message(role="user", content="今天好累啊"),
    ])
    tag, rest = parse_emotion_tag(reply)
    print(f"    emotion probe: tag={tag!r} rest={rest!r} (first_token={llm.last_first_token_s}s)")
    REPORT["emotion_probe"] = {"tag": tag, "reply": reply[:60]}
    pipeline_llm_ok = check("chat:emotion_tag_format", tag is not None) and pipeline_llm_ok
    # note: measured on the CHAT turn (chat_first_token), not the probe above
    first = chat_first_token or 99.0
    pipeline_llm_ok = check("chat:llm_first_token_under_5s", first < 5.0) and pipeline_llm_ok

    # --- scenario 3: real transcript drives the task fast lane ------------
    print("\n[3] voice task dispatch (fast lane on real ASR output)")
    from bridge.server import Bridge

    bus2 = InProcBus()
    tts2 = CaptureTts()
    bridge = Bridge(
        bus=bus2,
        session_book_path=ROOT / "temp_script" / "e2e_sb.json",
        grace_seconds=999.0,  # hold: we drive flush manually
    )
    task_pipeline = ConversationPipeline(
        source=None,
        verifier=ScriptedVerifier([{"master": 0.75}] * 3),
        asr=asr,
        llm=llm,
        tts=tts2,
        bus=bus2,
        bridge=bridge,
        profiles={
            "master": SpeakerProfile(
                speaker_id="master", name="主人", role="admin", priority=1
            )
        },
    )

    events: list[tuple[str, dict]] = []
    bus2.subscribe("*", lambda t, p: events.append((t, dict(p))))

    r1 = await task_pipeline.run_turn(wav["task_dispatch"])
    dispatch_ok = check("task:fast_lane", r1 == Route.TASK_DIRECT,
                        transcripts["task_dispatch"])
    dispatch_ok = check("task:confirmation_spoken",
                        any("收到" in t for t in tts2.captured)) and dispatch_ok
    dispatch_ok = check("task:held_not_dispatched", bridge.has_work() and
                        not bridge.is_dispatched()) and dispatch_ok

    # amend inside grace window (real transcript "不对，是改按钮的颜色")
    r2 = await task_pipeline.run_turn(wav["task_amend"])
    amend_ok = check("task:amend_routed", r2 == Route.TASK_AMEND,
                     transcripts["task_amend"])
    amend_ok = check("task:amend_zero_cost", not any(
        t == "task.result" for t, _ in events)) and amend_ok
    amend_ok = check("task:cancel_event_for_amendment", any(
        t == "task.cancel" and p.get("reason") == "amendment" for t, p in events
    )) and amend_ok

    await bridge.flush()
    await asyncio.sleep(0.2)
    results = [p for t, p in events if t == "task.result"]
    amend_ok = check("task:amended_result", len(results) == 1 and
                     "按钮" in results[0].get("summary", "")) and amend_ok

    # cancel scenario: fresh task then verbal cancel
    await task_pipeline.run_turn(wav["task_dispatch"])
    r4 = await task_pipeline.run_turn(wav["task_cancel"])
    cancel_ok = check("task:cancel_routed", r4 == Route.TASK_CANCEL,
                      transcripts["task_cancel"])
    cancel_ok = check("task:cancel_event", any(
        t == "task.cancel" and p.get("reason") == "user_request" for t, p in events
    )) and cancel_ok
    await bridge.close()

    # --- verdict ------------------------------------------------------------
    REPORT["pass"] = bool(asr_ok and pipeline_llm_ok and dispatch_ok
                          and amend_ok and cancel_ok)
    print("\n" + json.dumps(REPORT, ensure_ascii=False, indent=2))
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(REPORT, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0 if REPORT["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
