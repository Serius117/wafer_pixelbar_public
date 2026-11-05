"""Fix: transcribe reference audio with our ASR, then re-generate corpus
with CORRECT prompt_text for CosyVoice zero-shot.

Step 1: Send ref_spk*.wav through qwen3-asr → get real text
Step 2: Re-generate all 16 corpus files with correct prompt_text per voice
"""

import asyncio
import json
import sys
import wave
from pathlib import Path

import httpx
import numpy as np

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

CORPUS = ROOT / "tests" / "fixtures" / "corpus"

ASR_URL = "http://127.0.0.1:18001"
COSYVOICE_PY = (ROOT / "third_party" / "CosyVoice" / ".venv" / "Scripts"
                / "python.exe")
MODEL_DIR = ROOT / "models" / "tts" / "cosyvoice2-0.5b"


def read_wav_16k(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000
        return w.readframes(w.getnframes())


async def transcribe(pcm: bytes) -> str:
    """ASR via llama-server."""
    from engines.asr_qwen import parse_qwen_asr_output, pcm_to_wav_b64

    b64 = pcm_to_wav_b64(pcm)
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{ASR_URL}/v1/chat/completions",
            json={
                "model": "qwen3-asr",
                "messages": [{"role": "user", "content": [
                    {"type": "input_audio",
                     "input_audio": {"data": f"data:audio/wav;base64,{b64}",
                                     "format": "wav"}},
                ]}],
                "max_tokens": 256, "temperature": 0.0, "stream": False,
            })
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"]
        return parse_qwen_asr_output(raw)


async def main() -> int:
    # Step 1: transcribe each reference
    print("Step 1: Transcribing reference audio with ASR ...")
    prompt_texts = {}
    for ref in sorted(CORPUS.glob("ref_spk*.wav")):
        pcm = read_wav_16k(ref)
        text = await transcribe(pcm)
        prompt_texts[ref.stem] = text
        print(f"  {ref.stem}: {text!r}")

    # Save for step 2
    out = ROOT / "tests" / "fixtures" / "corpus" / "prompt_texts.json"
    out.write_text(json.dumps(prompt_texts, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\nSaved to {out}")

    # Step 2: generate a test synthesis with correct prompt_text
    print("\nStep 2: Test synthesis with correct prompt_text ...")
    import subprocess

    ref = CORPUS / "ref_spk0.wav"
    prompt_text = prompt_texts["ref_spk0"]
    test_text = "今天天气真不错，我们出去走走吧"
    result = subprocess.run(  # noqa: S603
        [str(COSYVOICE_PY), "-c", f"""
import sys
sys.path.insert(0, r'{ROOT / "third_party" / "CosyVoice"}')
sys.path.insert(0, r'{ROOT / "third_party" / "CosyVoice" / "third_party" / "Matcha-TTS"}')
from cosyvoice.cli.cosyvoice import CosyVoice2
import torchaudio
model = CosyVoice2(r'{MODEL_DIR}', load_jit=False, load_trt=False)
for out in model.inference_zero_shot(
    '{test_text}', '{prompt_text}', r'{ref}', stream=False
):
    torchaudio.save(r'{ROOT / "temp_script" / "test_fixed_prompt.wav"}',
                    out['tts_speech'], 24000)
    print('saved')
    break
"""],
        capture_output=True, text=True, timeout=300,
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )
    if "saved" in result.stdout:
        print("  synthesis OK -> temp_script/test_fixed_prompt.wav")
    else:
        print(f"  synthesis FAILED: {result.stderr[-200:]}")
        return 1

    # Step 3: ASR roundtrip on the fixed synthesis
    print("\nStep 3: ASR roundtrip on fixed synthesis ...")
    wav_path = ROOT / "temp_script" / "test_fixed_prompt.wav"
    with wave.open(str(wav_path), "rb") as w:
        sr = w.getframerate()
        pcm_data = w.readframes(w.getnframes())
    samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float64)
    if sr != 16000:
        old_len = len(samples)
        new_len = int(old_len * 16000 / sr)
        resampled = np.interp(
            np.linspace(0, old_len - 1, new_len),
            np.arange(old_len, dtype=np.float64), samples)
        pcm16 = np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()
    else:
        pcm16 = pcm_data
    transcript = await transcribe(pcm16)
    print(f"  transcript: {transcript!r}")
    print(f"  expected:   {test_text!r}")

    hit = "天气" in transcript and "不错" in transcript
    print(f"\n{'PASS' if hit else 'FAIL'}: roundtrip "
          f"{'intelligible' if hit else 'still garbled'}")
    return 0 if hit else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
