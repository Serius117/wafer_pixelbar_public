"""Ollama LLM engine: direct streaming client (engines/llm.py).

Connects to a local Ollama server and streams chat completions.
Zero OLV dependency — this is a pure engine wrapper (D18 placement rule).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence

import httpx

from core.interfaces import Message

logger = logging.getLogger(__name__)


class OllamaLlm:
    """Streams chat completions from a local Ollama server.

    Implements the LlmEngine protocol: stream(messages) -> AsyncIterator[str].
    """

    def __init__(
        self,
        model: str = "qwen3.5-9b:q4",
        ctx: int = 8192,
        base_url: str = "http://127.0.0.1:11434",
        temperature: float = 0.8,
        think: bool = False,
    ) -> None:
        self._model = model
        self._ctx = ctx
        self._base_url = base_url.rstrip("/")
        self._temperature = temperature
        self._think = think

    async def stream(self, messages: Sequence[Message]) -> AsyncIterator[str]:
        """Yield response text chunks from Ollama's chat API.

        Uses the /api/chat endpoint with stream=true.
        """
        payload = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": True,
            "options": {
                "temperature": self._temperature,
                "num_ctx": self._ctx,
                "think": self._think,
            },
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST",
                f"{self._base_url}/api/chat",
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                        # Ollama chat stream format:
                        # {"message": {"content": "..."}, "done": false}
                        content = chunk.get("message", {}).get("content", "")
                        if content:
                            yield content
                        if chunk.get("done", False):
                            break
                    except json.JSONDecodeError:
                        logger.warning("Ollama stream: malformed JSON line")
