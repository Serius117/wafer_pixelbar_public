"""Real wake-word engine: sherpa-onnx KWS (T6.3, D31).

Implements the WakeWordEngine protocol over the wenetspeech 3.3M zipformer
transducer. Custom words are pinyin-text configuration (zero training,
D31); the production keyword file lives next to the model.

The four usage traps found during T6.1 (see DEVLOG) are mandatory:
  1. feed float32 samples normalized to [-1, 1] (never raw int16)
  2. 0.66s of tail padding after the audio
  3. call input_finished() before decoding
  4. reset_stream() right after a hit
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from core.interfaces import WakeResult

logger = logging.getLogger(__name__)

DEFAULT_KWS_DIR = Path("models/wakeword/sherpa-kws-wenetspeech")
DEFAULT_KEYWORDS = DEFAULT_KWS_DIR / "keywords.txt"

TAIL_PAD_SECONDS = 0.66  # trap #2


def pcm_to_float32(pcm: bytes):
    """16-bit s16le mono PCM -> float32 [-1, 1] (trap #1)."""
    import numpy as np

    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


class SherpaKwsEngine:
    """WakeWordEngine backed by sherpa-onnx keyword spotting.

    `detect(pcm)` answers "did THIS segment contain the wake word" -- the
    session shell calls it per VAD segment while asleep (standby).
    """

    def __init__(
        self,
        model_dir: Path = DEFAULT_KWS_DIR,
        keywords_file: Path | None = None,
        sample_rate: int = 16000,
        num_threads: int = 1,
        keywords_threshold: float = 0.25,
    ) -> None:
        self._model_dir = Path(model_dir)
        self._keywords_file = Path(keywords_file or (model_dir / "keywords.txt"))
        self._sample_rate = sample_rate
        self._kws = None
        self._num_threads = num_threads
        self._threshold = keywords_threshold
        self.last_detect_s: float | None = None
        self.call_count = 0

    def _ensure_engine(self):
        if self._kws is None:
            import sherpa_onnx  # lazy: engine dependency stays out of core

            d = self._model_dir

            # glob so BOTH model generations work (wenetspeech epoch-12 and
            # the zh-en 2025-12 epoch-13, incl. chunk-8/chunk-16 variants)
            def _pick(prefix: str) -> Path | None:
                hits = sorted(d.glob(f"{prefix}-*.onnx"))
                # prefer int8 (small, CPU) and chunk-16 (accuracy) for the
                # same prefix generation
                int8 = [p for p in hits if ".int8." in p.name]
                pool = int8 or hits
                chunk16 = [p for p in pool if "chunk-16" in p.name]
                return (chunk16 or pool or [None])[0]

            enc = _pick("encoder")
            dec = _pick("decoder")
            joi = _pick("joiner")
            for f in (enc, dec, joi, d / "tokens.txt", self._keywords_file):
                if f is None or not f.exists():
                    raise FileNotFoundError(f"KWS asset missing: {f}")
            self._kws = sherpa_onnx.KeywordSpotter(
                tokens=str(d / "tokens.txt"),
                encoder=str(enc),
                decoder=str(dec),
                joiner=str(joi),
                keywords_file=str(self._keywords_file),
                num_threads=self._num_threads,
                keywords_score=1.5,
                keywords_threshold=self._threshold,
                provider="cpu",
            )
            logger.info("sherpa-onnx KWS loaded (keywords=%s)", self._keywords_file)
        return self._kws

    def detect(self, pcm: bytes) -> WakeResult:
        """Check one audio segment for the wake word."""
        started = time.monotonic()
        self.call_count += 1
        try:
            kws = self._ensure_engine()
            import numpy as np

            samples = pcm_to_float32(pcm)
            if samples.size == 0:
                return WakeResult(hit=False, score=0.0)

            stream = kws.create_stream()
            stream.accept_waveform(self._sample_rate, samples)
            tail = np.zeros(int(TAIL_PAD_SECONDS * self._sample_rate), dtype=np.float32)
            stream.accept_waveform(self._sample_rate, tail)
            stream.input_finished()  # trap #3

            hit = False
            keyword = ""
            while kws.is_ready(stream):
                kws.decode_stream(stream)
                result = kws.get_result(stream)
                if result:
                    hit = True
                    keyword = result
                    kws.reset_stream(stream)  # trap #4
                    break
            if hit:
                logger.info("wake word detected: %s", keyword)
            # D48: pass the hit keyword through ("" on miss)
            return WakeResult(hit=hit, score=1.0 if hit else 0.0,
                              keyword=keyword)
        finally:
            self.last_detect_s = time.monotonic() - started
