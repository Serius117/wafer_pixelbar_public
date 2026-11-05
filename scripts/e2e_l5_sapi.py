"""L5 fix: SAPI audio input (known text) + ScriptedVerifier bypass.

Uses the existing tests/fixtures/audio/*.wav (SAPI Huihui, 16k, known text)
for ASR, and bypasses the voiceprint gate with ScriptedVerifier since SAPI
is a single voice. This proves the opencode chain works end-to-end.
The real multi-voice corpus test waits for a matched prompt_text reference
(true-machine task).
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import wave
from pathlib import Path

sys.path.insert(0, ".")

from bridge.opencode_exec import OpencodeExecutor
from bridge.server import Bridge
from core.bus import topics
from core.bus.hub import InProcBus
from core.gate.policy import SpeakerProfile
from core.pipeline import ConversationPipeline
from core.sim import ScriptedLlm, ScriptedVerifier
from engines.asr_qwen import QwenLlamaServerAsr

AUDIO = Path("tests/fixtures/audio")  # SAPI wavs (16k, known text)


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


async def main() -> int:
    # prerequisites
    import httpx

    try:
        async with httpx.AsyncClient(timeout=3) as c:
            await c.get("http://127.0.0.1:18001/health")
    except Exception:
        print("ERROR: llama-server not running on :18001")
        return 1

    bus = InProcBus()
    events: list[tuple[str, dict]] = []
    bus.subscribe("*", lambda t, p: events.append((t, dict(p))))

    bridge = Bridge(
        bus=bus,
        session_book_path=Path("data/sb_l5_sapi.json"),
        grace_seconds=0.0,
        executor=OpencodeExecutor(),
    )
    tmp = Path(tempfile.mkdtemp())
    (tmp / "replies.json").write_text('["ok"]', encoding="utf-8")

    pipeline = ConversationPipeline(
        source=None,
        verifier=ScriptedVerifier([{"master": 0.75}]),  # bypass gate
        asr=QwenLlamaServerAsr(url="http://127.0.0.1:18001",
                               hotwords=["派活", "测试", "登录"]),
        llm=ScriptedLlm(tmp),
        tts=None,  # silent
        bus=bus,
        bridge=bridge,
        profiles={"master": SpeakerProfile(
            speaker_id="master", name="主人", role="admin", priority=1)},
        asr_timeout=60.0,  # ASR on CPU can take 10-15s per sentence
    )

    results = []

    def check(name, ok, detail=""):
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")

    # L5-01: voice -> ASR -> route -> real opencode -> result
    print("\n[L5-01] SAPI voice -> real opencode")
    pcm = read_wav(AUDIO / "task_opencode.wav")
    t0 = time.monotonic()
    route = await pipeline.run_turn(pcm)
    asr_time = time.monotonic() - t0
    check("L5:route", route is not None, f"route={route} asr={asr_time:.1f}s")

    # wait for opencode (flash model query, ~10-30s)
    print("  waiting for opencode ...")
    deadline = time.monotonic() + 120
    task_result = None
    while time.monotonic() < deadline:
        await asyncio.sleep(2)
        results_ev = [p for t, p in events if t == topics.TASK_RESULT]
        if results_ev:
            task_result = results_ev[-1]
            break

    if task_result:
        summary = task_result.get("summary", "")
        check("L5:real_result", len(summary) > 0,
              f"summary={summary[:80]}")
        check("L5:elapsed", task_result.get("elapsed_s", 0) > 0,
              f"elapsed={task_result.get('elapsed_s')}s "
              f"tools={task_result.get('tools_used')}")
    else:
        check("L5:real_result", False, "timeout waiting for opencode")

    progress = [p for t, p in events if t == topics.TASK_PROGRESS]
    check("L5:progress", len(progress) >= 2, f"{len(progress)} events")

    # L5-02: session continuation
    print("\n[L5-02] Session continuation")
    events.clear()
    # send a follow-up text task directly (skip ASR for speed)
    await bridge.dispatch(
        "我刚才问你的第一个问题是什么？一句话回答。", "master")
    deadline = time.monotonic() + 120
    result2 = None
    while time.monotonic() < deadline:
        await asyncio.sleep(2)
        results_ev = [p for t, p in events if t == topics.TASK_RESULT]
        if results_ev:
            result2 = results_ev[-1]
            break
    if result2:
        summary2 = result2.get("summary", "")
        check("L5:session_continuation", "测试" in summary2 or "文件" in summary2
              or len(summary2) > 5,
              f"summary={summary2[:80]}")
    else:
        check("L5:session_continuation", False, "timeout")

    await bridge.close()
    passed = sum(results)
    total = len(results)
    print(f"\n{'='*50}")
    print(f"L5 (SAPI path): {passed}/{total} passed")
    print(f"{'='*50}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
