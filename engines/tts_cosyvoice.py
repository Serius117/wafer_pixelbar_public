"""CosyVoice TTS engine over the cosyvoice_server HTTP API (TTS-03, D32).

The heavy CosyVoice stack lives in its own venv behind
`scripts/cosyvoice_server.py` (D32: its requirements would pin numpy/torch
versions and pollute the main environment). This engine is the wafer-side
TtsEngine: HTTP call, wav bytes out.

Failure semantics: connection errors / non-200 raise RuntimeError ->
the pipeline's rule-6 degradation handles them (LED error + notify);
a silent engine never blocks the conversation loop (D21: 要么说话要么沉默).
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

from core.interfaces import TtsEngine  # noqa: F401  (protocol marker)

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:9880"


class CosyVoiceTts:
    """TtsEngine backed by the cosyvoice_server process."""

    def __init__(self, url: str = DEFAULT_URL, timeout: float = 120.0) -> None:
        self._url = url.rstrip("/")
        self._timeout = timeout
        self.last_speak_s: float | None = None
        self.call_count = 0

    async def speak(self, text: str) -> AsyncIterator[bytes]:
        """Synthesize one line; yields the complete wav (24k s16le mono)."""
        text = (text or "").strip()
        if not text:
            return  # nothing to say (rule: never emit empty audio)

        started = time.monotonic()
        self.call_count += 1

        import httpx

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._url}/synthesize",
                    json={"text": text},
                )
        except httpx.HTTPError as e:
            raise RuntimeError(f"cosyvoice server unreachable: {e}") from e

        if resp.status_code != 200:
            raise RuntimeError(
                f"cosyvoice server {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        wav = resp.content
        if not wav.startswith(b"RIFF"):
            raise RuntimeError("cosyvoice server returned non-wav payload")

        self.last_speak_s = time.monotonic() - started
        logger.info("tts synthesized %d chars -> %.1fs wav bytes=%d",
                    len(text), self.last_speak_s, len(wav))
        yield wav
