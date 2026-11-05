"""Regenerate tests/fixtures/asr_cache.json (VE2E V.4).

Transcribes every corpus wav with the real qwen3-asr engine (production
hotwords) and writes the sha1(pcm16k) -> transcript cache that D1's
MappedAsr reads. Run this whenever the corpus wavs change or the ASR
model is upgraded, then rerun `pytest tests/test_ve2e.py`.

Usage:
  .venv\\Scripts\\python.exe scripts/regen_asr_cache.py [--dry-run]

Prereq: llama-server on :18001 (auto-started if missing).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

ASR_MODEL = ROOT / "models" / "asr" / "qwen3-asr-1.7b-q4"
LLAMA_EXE = ROOT / "tools" / "llama" / "llama-server.exe"
CORPUS_DIR = ROOT / "tests" / "fixtures" / "corpus"
CACHE_PATH = ROOT / "tests" / "fixtures" / "asr_cache.json"

# keep in sync with the production hotword set (see TEST_DESIGN §14 (hotword set))
HOTWORDS = ["派活", "小饼", "项目", "字幕", "音乐", "测试", "统计"]

# expected texts come from gen_voice_corpus.py CORPUS (master entries)
from scripts.gen_voice_corpus import CORPUS  # noqa: E402

EXPECTED = {f"{role}_{suffix}": text
            for (role, suffix), text in CORPUS.items()}


def read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


def ensure_llama() -> subprocess.Popen | None:
    import httpx
    try:
        if httpx.get("http://127.0.0.1:18001/health", timeout=2).status_code == 200:
            return None
    except Exception:
        pass
    proc = subprocess.Popen([
        str(LLAMA_EXE), "-m", str(ASR_MODEL / "Qwen3-ASR-1.7B-Q4_K_M.gguf"),
        "--mmproj", str(ASR_MODEL / "mmproj-Qwen3-ASR-1.7B-Q8_0.gguf"),
        "--port", "18001", "--alias", "qwen3-asr", "--host", "127.0.0.1",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 180
    with httpx.Client(timeout=3) as c:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("llama-server died during startup")
            try:
                if c.get("http://127.0.0.1:18001/health").status_code == 200:
                    print(f"[svc] llama-server started (pid {proc.pid})")
                    return proc
            except Exception:
                pass
            time.sleep(1.5)
    raise RuntimeError("llama-server unhealthy after 180s")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print transcriptions, keep the cache unchanged")
    args = parser.parse_args()

    from engines.asr_qwen import QwenLlamaServerAsr

    llama = ensure_llama()
    asr = QwenLlamaServerAsr(url="http://127.0.0.1:18001", hotwords=HOTWORDS)

    old = json.loads(CACHE_PATH.read_text(encoding="utf-8")) \
        if CACHE_PATH.exists() else {}
    cache: dict[str, str] = {}
    changed: list[str] = []

    wavs = sorted(CORPUS_DIR.glob("master_*.wav"))
    for wav in wavs:
        if wav.stem not in EXPECTED:
            continue  # e.g. enroll clips are gate data, not turn inputs
        pcm = read_pcm(wav)
        key = hashlib.sha1(pcm).hexdigest()
        text = await asr.transcribe(pcm)
        cache[key] = text
        if old.get(key) != text:
            changed.append(wav.stem)
        print(f"  {wav.stem}: {text}")

    print(f"\n{len(cache)} entries, {len(changed)} changed vs old cache")
    if changed:
        print(f"changed: {', '.join(changed)}")
        print("NOTE: D1 expectations may shift -- rerun "
              "`pytest tests/test_ve2e.py` after this.")

    if not args.dry_run:
        CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"written -> {CACHE_PATH}")

    if llama is not None:
        llama.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
