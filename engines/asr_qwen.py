"""Qwen3-ASR engine over llama-server (W.11 probe winner).

Head-to-head on 2026-10-16 (5 synthetic SAPI sentences, CPU):
    qwen3-asr-0.6b Q8: 0.82s/sentence avg, 9/9 router keywords, clean
                       simplified-Chinese output, hotword support
    faster-whisper small: 3.35s avg, 8/9 (登陆 vs 登录), mixed scripts

Deployment: run llama-server with the GGUF + mmproj (audio projection),
e.g. (borrowed from the realtime_subtitle project, proven there):

    llama-server -m Qwen3-ASR-0.6B-Q8_0.gguf \
        --mmproj mmproj-Qwen3-ASR-0.6B-Q8_0.gguf \
        --port 8001 --alias qwen3-asr

GPU sizing (RTX 4070S, work mode shares VRAM with Qwen3.5-9B + CosyVoice):
    0.6B Q8  fully offloaded: ~1.2-1.5GB   (recommended default)
    1.7B Q8  fully offloaded: ~2.6-3.0GB   (game mode / quiet room OK,
                                            work mode blows the 12GB budget)
    1.7B Q8  CPU-only:        ~0GB VRAM but ~2-3x slower (over budget)

This client implements the AsrEngine protocol (16 kHz mono s16 PCM in,
simplified-Chinese text out). Output parsing handles all three observed
Qwen3-ASR formats (see realtime_subtitle/src/asr_parse.py, R5 findings).
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
import time
import wave

logger = logging.getLogger(__name__)

# --- output parsing (borrowed & trimmed from realtime_subtitle R5) ---------

_PREFIX_LANG_RE = re.compile(
    r"^\s*language\s*[:=]?\s*([A-Za-z\u4e00-\u9fff\-_ ]{2,24}?)\s*"
    r"(?:<\s*/?\s*asr_text\s*/?\s*>|\|\s*asr_text\s*\|)",
    re.I,
)
_LANG_TAG_RE = re.compile(r"^\s*<\|\s*([A-Za-z\-_]+)\s*\|>\s*")
_ASR_TEXT_TAG_RE = re.compile(r"<\s*/?\s*asr_text\s*/?\s*>", re.I)
_ANY_TAG_RE = re.compile(r"<\|[^|>]{0,24}\|>")


def parse_qwen_asr_output(raw: str) -> str:
    """Normalize any observed Qwen3-ASR output format to plain text.

    Handles: 'language Chinese<asr_text>正文' / '<|zh|>正文' / bare text,
    and strips residual special tokens (im_end etc. leak in because the
    server decodes with skip_special_tokens=False for the asr_text marker).
    """
    if not raw:
        return ""
    s = str(raw).strip()
    m = _PREFIX_LANG_RE.match(s)
    if m:
        s = s[m.end():]
    else:
        s = _LANG_TAG_RE.sub("", s, count=1)
    s = _ASR_TEXT_TAG_RE.sub("", s)
    s = _ANY_TAG_RE.sub("", s)
    return s.strip().strip("\"'“”‘’").strip()


def pcm_to_wav_b64(pcm: bytes, sample_rate: int = 16000) -> str:
    """Wrap raw s16le mono PCM into a wav container, base64-encoded."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def build_payload(model: str, b64: str, hotwords: list[str]) -> dict:
    """Build the chat/completions payload with the mtmd audio part."""
    content: list[dict] = [{
        "type": "input_audio",
        "input_audio": {"data": f"data:audio/wav;base64,{b64}", "format": "wav"},
    }]
    if hotwords:
        content.append({
            "type": "text",
            "text": "Hotwords: " + ", ".join(hotwords[:24]),
        })
    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 256,
        "temperature": 0.0,
        "stream": False,
    }


class QwenLlamaServerAsr:
    """AsrEngine backed by a llama-server hosting Qwen3-ASR GGUF.

    Hotwords (trigger words + the robot's name) are passed with every
    request -- the killer feature for our keyword-routing fast lane.
    """

    def __init__(
        self,
        url: str = "http://127.0.0.1:8001",
        model: str = "qwen3-asr",
        hotwords: list[str] | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._url = url.rstrip("/")
        self._model = model
        self._hotwords = list(hotwords) if hotwords else []
        self._timeout = timeout
        self.last_transcribe_s: float | None = None
        self.call_count = 0

    def _transcribe_blocking(self, pcm: bytes) -> str:
        """Sync HTTP call (run in a worker thread via to_thread)."""
        import httpx

        payload = build_payload(self._model, pcm_to_wav_b64(pcm), self._hotwords)
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(f"{self._url}/v1/chat/completions", json=payload)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"qwen-asr server {resp.status_code}: {resp.text[:200]}"
                )
            raw = resp.json()["choices"][0]["message"]["content"]
        return parse_qwen_asr_output(raw)

    async def transcribe(self, pcm: bytes) -> str:
        started = time.monotonic()
        self.call_count += 1
        text = await asyncio.to_thread(self._transcribe_blocking, pcm)
        self.last_transcribe_s = time.monotonic() - started
        return text
