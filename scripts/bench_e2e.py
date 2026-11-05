"""Latency benchmark: simulator mode (T3.7).

Measures per-turn pipeline latency using fixture engines.
Real-engine latency requires home hardware.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.bus.hub import InProcBus
from core.gate.policy import SpeakerProfile
from core.pipeline import ConversationPipeline
from core.sim import CaptureTts, FixtureAsr, FixtureVerifier, ScriptedLlm

NUM_TURNS = 20


async def async_benchmark() -> dict:
    fixture_dir = Path(__file__).parent.parent / "tests" / "bench_fixtures"
    fixture_dir.mkdir(exist_ok=True)
    (fixture_dir / "voiceprints.json").write_text('{"master": 0.75}', encoding="utf-8")
    (fixture_dir / "transcripts.json").write_text('["你好"]', encoding="utf-8")
    (fixture_dir / "replies.json").write_text('["[emotion:neutral] 好的"]', encoding="utf-8")

    bus = InProcBus()
    tts = CaptureTts()
    profile = SpeakerProfile(speaker_id="master", name="主人", role="admin", priority=1)
    pipeline = ConversationPipeline(
        source=None,
        verifier=FixtureVerifier(fixture_dir),
        asr=FixtureAsr(fixture_dir),
        llm=ScriptedLlm(fixture_dir),
        tts=tts,
        bus=bus,
        profiles={"master": profile},
    )

    latencies = []
    for _ in range(NUM_TURNS):
        t0 = time.perf_counter()
        await pipeline.run_turn(b"\x00" * 100)
        latencies.append((time.perf_counter() - t0) * 1000)

    latencies.sort()
    return {
        "num_turns": NUM_TURNS,
        "p50_ms": round(statistics.median(latencies), 3),
        "p95_ms": round(latencies[int(len(latencies) * 0.95)], 3),
        "mean_ms": round(statistics.mean(latencies), 3),
        "min_ms": round(latencies[0], 3),
        "max_ms": round(latencies[-1], 3),
    }


if __name__ == "__main__":
    print("Running latency benchmark (simulator mode)...")
    result = asyncio.run(async_benchmark())
    print(json.dumps(result, indent=2))

    report_path = Path(__file__).parent.parent / "logs" / "bench_e2e_sim.json"
    report_path.parent.mkdir(exist_ok=True)
    report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\nReport saved: {report_path}")
