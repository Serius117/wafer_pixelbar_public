"""Shared DashScope HTTP helpers for the CosyVoice v3.5-flash adapter.

No SDK dependency: the project already ships httpx, but the TTS server must
also be able to run under a bare interpreter, so this module uses stdlib
urllib only.  Nothing here logs the API key.

Endpoints (Beijing; docs 2026-10-31):
  clone: https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/customization
  tts:   https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGION = "cn-beijing"
API_KEY_ENV = "DASHSCOPE_API_KEY"
WORKSPACE_ID_ENV = "DASHSCOPE_WORKSPACE_ID"
VOICE_ID_ENV = "DASHSCOPE_VOICE_ID"


def load_env_file(path: Path | str | None = None) -> dict[str, str]:
    """Load KEY=VALUE pairs from .env without overwriting real env vars."""
    env_path = Path(path) if path is not None else ROOT / ".env"
    loaded: dict[str, str] = {}
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return loaded
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip().strip('"').strip("'")
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


def require_env(name: str, value: str | None = None) -> str:
    resolved = value or os.environ.get(name, "")
    if not resolved.strip():
        raise RuntimeError(
            f"missing {name}; set it in the environment or repo-root .env")
    return resolved.strip()


def api_base(workspace_id: str, region: str = DEFAULT_REGION) -> str:
    workspace_id = (workspace_id or "").strip().rstrip("/")
    if not workspace_id:
        raise RuntimeError("missing WorkspaceId")
    if workspace_id.startswith(("http://", "https://")):
        return workspace_id
    return f"https://{workspace_id}.{region}.maas.aliyuncs.com"


def tts_endpoint(workspace_id: str, region: str = DEFAULT_REGION) -> str:
    return (f"{api_base(workspace_id, region)}"
            "/api/v1/services/audio/tts/SpeechSynthesizer")


def clone_endpoint(workspace_id: str, region: str = DEFAULT_REGION) -> str:
    return (f"{api_base(workspace_id, region)}"
            "/api/v1/services/audio/tts/customization")


def _error_detail(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return text[:400] or "(empty body)"
    code = obj.get("code") or obj.get("request_id") or "error"
    message = obj.get("message") or obj.get("msg") or text
    return f"{code}: {str(message)[:400]}"


def _open(req: urllib.request.Request, timeout: float) -> tuple[bytes, str]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"DashScope HTTP {exc.code}: {_error_detail(exc.read())}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"DashScope unreachable: {exc}") from exc


def post_raw(url: str, payload: dict, api_key: str,
             timeout: float = 60.0) -> tuple[bytes, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    return _open(req, timeout)


def post_json(url: str, payload: dict, api_key: str,
              timeout: float = 60.0) -> dict:
    raw, _ = post_raw(url, payload, api_key, timeout)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"DashScope returned non-JSON: {raw[:200]!r}") from exc
    if not isinstance(obj, dict):
        raise RuntimeError(f"DashScope returned unexpected JSON: {obj!r}")
    code = obj.get("code")
    if code not in (None, "", 200, "200") and "output" not in obj:
        raise RuntimeError(f"DashScope error {code}: {obj.get('message', '')}")
    return obj


def get_bytes(url: str, timeout: float = 60.0, api_key: str = "") -> bytes:
    headers = {"Accept": "audio/wav, application/octet-stream"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)
    raw, _ = _open(req, timeout)
    return raw


def decode_data_uri(value: str) -> bytes:
    payload = value.split(",", 1)[1] if "," in value else value
    try:
        return base64.b64decode(payload)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("invalid Base64 audio payload") from exc


def audio_bytes(response: dict, timeout: float = 60.0,
                api_key: str = "") -> bytes:
    """Extract wav bytes from a non-streaming SpeechSynthesizer response."""
    output = response.get("output") or {}
    audio = output.get("audio") or {}
    if isinstance(audio, str):
        audio = {"url": audio}
    data = str(audio.get("data") or "").strip()
    if data:
        return decode_data_uri(data)
    url = str(audio.get("url") or "").strip()
    if url:
        return get_bytes(url, timeout=timeout, api_key=api_key)
    raise RuntimeError("DashScope response has no audio URL/data")
