"""OpenAI-compatible LLM engine over HTTP (E2E real-model testing, W.11).

Talks to any OpenAI-style /chat/completions endpoint with SSE streaming.
The production brain is local Ollama (engines/llm.py); this engine exists
so the full cognitive chain can be tested end-to-end on the dev machine
with a cloud flash model (user-approved: Volcengine Ark deepseek-v4.1-flash
via the opencode provider config) before hardware bring-up.

Credential resolution (never hardcoded, never committed):
    explicit args > env (WAFER_LLM_API_KEY / WAFER_LLM_BASE_URL /
    WAFER_LLM_MODEL) > the local opencode config
    (~/.config/opencode/opencode.json + ~/.local/share/opencode/auth.json).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from core.interfaces import Message

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "deepseek-v4.1-flash"
DEFAULT_TIMEOUT = 60.0

OPENCODE_CONFIGS = (
    Path.home() / ".config" / "opencode" / "opencode.json",
    Path.home() / ".config" / "opencode" / "opencode.jsonc",  # home machine
)
OPENCODE_AUTH = Path.home() / ".local" / "share" / "opencode" / "auth.json"


def _load_opencode_config() -> dict:
    """Load opencode.json / opencode.jsonc (best-effort JSONC: comments and
    trailing commas tolerated; strict JSON tried first)."""
    import json
    import re

    for path in OPENCODE_CONFIGS:
        if not path.exists():
            continue
        raw = path.read_text(encoding="utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        # JSONC best-effort: strip // and /* */ comments, drop trailing commas
        stripped = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
        stripped = re.sub(r"^\s*//.*$", "", stripped, flags=re.M)
        stripped = re.sub(r"//(?![^\"]*\"[^\"]*(?:\"[^\"]*\"[^\"]*)*$).*$", "",
                          stripped, flags=re.M)
        stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as e:
            raise FileNotFoundError(
                f"opencode config unreadable ({path.name}): {e}"
            ) from e
    raise FileNotFoundError("no opencode config found (json/jsonc)")


def load_opencode_provider(provider: str = "huoshan") -> tuple[str, str]:
    """Read (base_url, api_key) from the local opencode config.

    Raises FileNotFoundError when the config/auth files are absent (e.g. CI).
    The key is never logged.
    """
    import json

    cfg = _load_opencode_config()
    entry = (cfg.get("provider") or {}).get(provider)
    if not entry:
        raise FileNotFoundError(f"provider '{provider}' not in opencode config")
    options = entry.get("options") or {}
    base_url = options.get("baseURL") or entry.get("baseURL")
    api_key = options.get("apiKey")

    if not api_key:
        auth = json.loads(OPENCODE_AUTH.read_text(encoding="utf-8"))
        api_key = (auth.get(provider) or {}).get("key")
    if not base_url or not api_key:
        raise FileNotFoundError(
            f"opencode provider '{provider}' has no baseURL/apiKey"
        )
    return base_url, api_key


def resolve_llm_config(
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    provider: str = "huoshan",
) -> tuple[str, str, str]:
    """Config priority: args > env > opencode config."""
    base_url = base_url or os.environ.get("WAFER_LLM_BASE_URL")
    api_key = api_key or os.environ.get("WAFER_LLM_API_KEY")
    model = model or os.environ.get("WAFER_LLM_MODEL") or DEFAULT_MODEL
    if not base_url or not api_key:
        oc_base, oc_key = load_opencode_provider(provider)
        base_url = base_url or oc_base
        api_key = api_key or oc_key
    return base_url, api_key, model


def parse_sse_chunk(raw: str) -> list[dict[str, Any]]:
    """Parse one SSE chunk body into JSON events ('data: {...}' lines)."""
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            events.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return events


def extract_delta_content(event: dict[str, Any]) -> str:
    """Extract the streamed text delta from one chat.completions chunk."""
    choices = event.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    return str(delta.get("content") or "")


class OpenAiCompatLlm:
    """LlmEngine implementation over an OpenAI-compatible streaming API."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        provider: str = "huoshan",
    ) -> None:
        self._base_url, self._api_key, self._model = resolve_llm_config(
            base_url, api_key, model, provider
        )
        self._timeout = timeout
        self.last_first_token_s: float | None = None
        self.call_count = 0

    @property
    def model(self) -> str:
        return self._model

    def _mask_key(self) -> str:
        return f"{self._api_key[:4]}***{self._api_key[-4:]}" if self._api_key else "?"

    async def stream(self, messages: list[Message]) -> AsyncIterator[str]:
        """Stream chat completions, yielding content deltas."""
        import time

        import httpx

        url = f"{self._base_url.rstrip('/')}/chat/completions"
        body = {
            "model": self._model,
            "messages": [
                {"role": m.role, "content": m.content} for m in messages
            ],
            "stream": True,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        self.call_count += 1
        started = time.monotonic()
        first = True
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code != 200:
                    text = await resp.aread()
                    raise RuntimeError(
                        f"LLM API {resp.status_code}: {text.decode('utf-8', 'replace')[:200]}"
                    )
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    for event in parse_sse_chunk(line):
                        delta = extract_delta_content(event)
                        if delta:
                            if first:
                                self.last_first_token_s = time.monotonic() - started
                                first = False
                            yield delta

    async def complete(self, messages: list[Message]) -> str:
        """Non-streaming convenience: collect the full reply."""
        chunks: list[str] = []
        async for chunk in self.stream(messages):
            chunks.append(chunk)
        return "".join(chunks)
