"""Real ASR engine: faster-whisper (CPU int8 on dev, GPU at home).

Implements the AsrEngine protocol (core/interfaces.py). Input is raw
16 kHz mono s16le PCM bytes (the same shape the whole pipeline uses);
transcription runs in a worker thread so the event loop never blocks.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MODEL_DIR = Path("models/asr/faster-whisper-base")

# Lazy singleton for traditional->simplified conversion: whisper's Chinese
# output mixes scripts ("不對" vs "不对") and every downstream keyword
# matcher (router cues, exit words, composer fillers) expects simplified.
_t2s_converter = None


def to_simplified(text: str) -> str:
    """Normalize mixed-script Chinese ASR output to simplified.

    Uses opencc when available; falls back to the raw text (logged once)
    so CI without the dependency still works.
    """
    global _t2s_converter
    if _t2s_converter is None:
        try:
            from opencc import OpenCC

            _t2s_converter = OpenCC("t2s")
        except ImportError:
            logger.warning("opencc not installed -- ASR text stays mixed-script")
            _t2s_converter = False
    if _t2s_converter is False:
        return text
    return _t2s_converter.convert(text)


class FasterWhisperAsr:
    """AsrEngine backed by faster-whisper."""

    def __init__(
        self,
        model_path: str | Path = DEFAULT_MODEL_DIR,
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "zh",
    ) -> None:
        self._model_path = str(model_path)
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._model = None
        self.last_transcribe_s: float | None = None
        self.call_count = 0

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # lazy heavy import

            logger.info(
                "loading faster-whisper: %s (%s/%s)",
                self._model_path, self._device, self._compute_type,
            )
            self._model = WhisperModel(
                self._model_path,
                device=self._device,
                compute_type=self._compute_type,
            )
        return self._model

    def _transcribe_sync(self, pcm: bytes) -> str:
        import numpy as np

        # ASR-04: empty segment short-circuits BEFORE model load
        if not pcm or len(pcm) < 2:
            return ""
        model = self._ensure_model()
        wav = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if wav.size == 0:
            return ""
        segments, _info = model.transcribe(
            wav,
            language=self._language,
            beam_size=1,
            vad_filter=False,  # segments are already VAD-cut upstream
        )
        raw = "".join(seg.text for seg in segments).strip()
        # whisper zh output mixes traditional/simplified -- normalize
        return to_simplified(raw)

    async def transcribe(self, pcm: bytes) -> str:
        """Transcribe 16 kHz mono s16 PCM to text (thread-offloaded)."""
        started = time.monotonic()
        self.call_count += 1
        text = await asyncio.to_thread(self._transcribe_sync, pcm)
        self.last_transcribe_s = time.monotonic() - started
        return text
