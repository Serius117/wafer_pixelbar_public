"""MiniMax T2A v2 TTS server with the wafer HTTP contract (TASK.PROVIDER.02).

Same endpoints as scripts/gpt_sovits_server.py and dashscope_tts_server.py:

    GET  /health            -> 200 when the client is configured
    POST /synthesize        -> audio/wav
    POST /synthesize/frames -> u32 BE length + wav, one complete frame

MiniMax's synchronous API returns hex-encoded WAV (or a URL when
output_format=url); this adapter decodes it and keeps OLV/wafer consumers
unchanged. The request shape follows the 2026 MiniMax T2A v2 docs:
POST {base_url}/v1/t2a_v2 with Authorization: Bearer <key>.

Configuration is environment-only for the key:
  MINIMAX_API_KEY, optional MINIMAX_GROUP_ID
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engines.provider_config import load_env_file  # noqa: E402

DEFAULT_BASE_URL = "https://api.minimax.cn"
DEFAULT_MODEL = "speech-2.8-hd"
DEFAULT_VOICE_ID = "Chinese_crisp_podcaster_nv1"
API_KEY_ENV = "MINIMAX_API_KEY"
GROUP_ID_ENV = "MINIMAX_GROUP_ID"


def _error_detail(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return text[:400] or "(empty body)"
    base = obj.get("base_resp") or {}
    code = base.get("status_code") or obj.get("code") or "error"
    message = base.get("status_msg") or obj.get("message") or text
    return f"{code}: {str(message)[:400]}"


class MiniMaxTts:
    """Thin MiniMax T2A v2 client; raises RuntimeError on failure."""

    def __init__(
        self,
        *,
        api_key: str,
        voice_id: str = DEFAULT_VOICE_ID,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        endpoint: str = "",
        group_id: str = "",
        sample_rate: int = 32000,
        audio_format: str = "wav",
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise RuntimeError("missing MINIMAX_API_KEY")
        if not voice_id:
            raise RuntimeError("missing MiniMax voice_id")
        if audio_format != "wav":
            raise RuntimeError("MiniMax adapter currently requires WAV output")
        self._api_key = api_key
        self._voice_id = voice_id
        self._model = model
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._endpoint = endpoint or f"{self._base_url}/v1/t2a_v2"
        self._group_id = group_id.strip()
        self._sample_rate = int(sample_rate)
        self._format = audio_format
        self._timeout = float(timeout)
        self._lock = threading.Lock()
        self.last_speak_s: float | None = None
        self.call_count = 0

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def _request(self, payload: dict) -> tuple[bytes, str]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._group_id:
            headers["GroupId"] = self._group_id
        req = urllib.request.Request(
            self._endpoint, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"MiniMax HTTP {exc.code}: {_error_detail(exc.read())}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"MiniMax unreachable: {exc}") from exc

    def _decode_audio(self, value: str) -> bytes:
        value = (value or "").strip()
        if not value:
            raise RuntimeError("MiniMax response has no audio")
        if value.startswith(("http://", "https://")):
            return self._download(value)
        if value.startswith("data:"):
            value = value.split(",", 1)[1]
        try:
            return bytes.fromhex(value)
        except (ValueError, TypeError):
            try:
                return base64.b64decode(value)
            except (ValueError, TypeError) as exc:
                raise RuntimeError("MiniMax audio is not valid hex/base64") from exc

    def _download(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"Accept": "audio/wav"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError,
                TimeoutError, OSError) as exc:
            raise RuntimeError(f"MiniMax audio download failed: {exc}") from exc

    def synthesize(self, text: str) -> bytes:
        text = (text or "").strip()
        if not text:
            return b""
        payload = {
            "model": self._model,
            "text": text,
            "stream": False,
            "voice_setting": {
                "voice_id": self._voice_id,
                "speed": 1,
                "vol": 1,
                "pitch": 0,
            },
            "audio_setting": {
                "sample_rate": self._sample_rate,
                "format": self._format,
                "channel": 1,
            },
            "output_format": "hex",
        }
        started = time.monotonic()
        with self._lock:
            raw, _ = self._request(payload)
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"MiniMax returned non-JSON: {raw[:200]!r}") from exc
        if not isinstance(response, dict):
            raise RuntimeError(f"MiniMax returned unexpected JSON: {response!r}")
        base = response.get("base_resp") or {}
        code = base.get("status_code")
        if code not in (None, 0, "0"):
            raise RuntimeError(
                f"MiniMax error {code}: {base.get('status_msg', '')}")
        data = response.get("data") or {}
        wav = self._decode_audio(str(data.get("audio") or ""))
        if not wav.startswith(b"RIFF"):
            raise RuntimeError("MiniMax returned non-wav audio")
        self.call_count += 1
        self.last_speak_s = time.monotonic() - started
        extra = response.get("extra_info") or {}
        print(json.dumps({
            "event": "synth",
            "chars": len(text),
            "usage_characters": extra.get("usage_characters"),
            "seconds": round(self.last_speak_s, 3),
            "wav_bytes": len(wav),
        }, ensure_ascii=False), flush=True)
        return wav


def make_handler(tts: MiniMaxTts):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet; synth logs are structured
            pass

        def _text(self) -> str | None:
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self.send_response(400)
                self.end_headers()
                return None
            text = str(body.get("text", "")).strip()
            if not text:
                self.send_response(400)
                self.end_headers()
                return None
            return text

        def do_GET(self):  # noqa: N802
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):  # noqa: N802
            if self.path not in ("/synthesize", "/synthesize/frames"):
                self.send_response(404)
                self.end_headers()
                return
            text = self._text()
            if text is None:
                return
            try:
                wav = tts.synthesize(text)
            except Exception as exc:  # noqa: BLE001 -- HTTP 500 is the contract
                print(f"minimax synth error: {exc}", file=sys.stderr,
                      flush=True)
                self.send_response(500)
                self.end_headers()
                return
            if not wav:
                self.send_response(500)
                self.end_headers()
                return
            if self.path == "/synthesize/frames":
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(struct.pack(">I", len(wav)) + wav)
                self.close_connection = True
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav)))
            self.end_headers()
            self.wfile.write(wav)

    return Handler


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9880)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--voice-id", default=DEFAULT_VOICE_ID)
    ap.add_argument("--voice-id-env", default="MINIMAX_VOICE_ID")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--endpoint", default="")
    ap.add_argument("--api-key-env", default=API_KEY_ENV)
    ap.add_argument("--group-id-env", default=GROUP_ID_ENV)
    ap.add_argument("--group-id", default="")
    ap.add_argument("--sample-rate", type=int, default=32000)
    ap.add_argument("--format", default="wav")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args(argv)

    load_env_file()
    api_key = os.environ.get(args.api_key_env, "").strip()
    group_id = (args.group_id
                or os.environ.get(args.group_id_env, "")).strip()
    voice_id = (args.voice_id
                or os.environ.get(args.voice_id_env, "")).strip()
    if not api_key:
        ap.error(f"missing {args.api_key_env} (environment or .env)")

    try:
        tts = MiniMaxTts(
            api_key=api_key,
            voice_id=voice_id,
            model=args.model,
            base_url=args.base_url,
            endpoint=args.endpoint,
            group_id=group_id,
            sample_rate=args.sample_rate,
            audio_format=args.format,
            timeout=args.timeout,
        )
    except RuntimeError as exc:
        ap.error(str(exc))

    print(json.dumps({
        "engine": "minimax-t2a-v2",
        "model": args.model,
        "voice_id": voice_id,
        "endpoint": tts.endpoint,
        "sample_rate": args.sample_rate,
        "format": args.format,
    }, ensure_ascii=False), flush=True)
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(tts))
    print(f"minimax-tts-server listening on http://{args.host}:{args.port}",
          flush=True)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
