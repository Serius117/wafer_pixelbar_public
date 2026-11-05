"""Aliyun DashScope CosyVoice TTS server with the wafer HTTP contract.

Same endpoints as scripts/cosyvoice_server.py and gpt_sovits_server.py:

    GET  /health            -> 200 when the client is configured
    POST /synthesize        -> audio/wav
    POST /synthesize/frames -> u32 BE length + wav, one complete frame

The remote API is non-streaming: one POST returns a 24h audio URL, which is
downloaded before the response is sent.  This keeps OLV/wafer consumers
unchanged and makes failure semantics identical to the local servers.

Configuration is environment-only for the key:
  DASHSCOPE_API_KEY, DASHSCOPE_WORKSPACE_ID, DASHSCOPE_VOICE_ID
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import dashscope_common as dc  # noqa: E402


class DashScopeTts:
    """Thin wrapper around SpeechSynthesizer; raises RuntimeError on failure."""

    def __init__(self, *, api_key: str, workspace_id: str,
                 model: str = "cosyvoice-v3.5-flash", voice_id: str = "",
                 region: str = dc.DEFAULT_REGION, endpoint: str = "",
                 sample_rate: int = 24000, audio_format: str = "wav",
                 language: str = "zh", timeout: float = 60.0) -> None:
        if not api_key:
            raise RuntimeError("missing DASHSCOPE_API_KEY")
        if not voice_id:
            raise RuntimeError("missing cloned voice id (DASHSCOPE_VOICE_ID)")
        self._api_key = api_key
        self._model = model
        self._voice_id = voice_id
        self._endpoint = endpoint or dc.tts_endpoint(workspace_id, region)
        self._sample_rate = int(sample_rate)
        self._format = audio_format
        self._language = language
        self._timeout = float(timeout)
        self._lock = threading.Lock()
        self.last_speak_s: float | None = None
        self.call_count = 0

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def synthesize(self, text: str) -> bytes:
        text = (text or "").strip()
        if not text:
            return b""
        payload = {
            "model": self._model,
            "input": {
                "text": text,
                "voice": self._voice_id,
                "format": self._format,
                "sample_rate": self._sample_rate,
                "language_hints": [self._language],
            },
        }
        started = time.monotonic()
        with self._lock:
            response = dc.post_json(self._endpoint, payload, self._api_key,
                                    timeout=self._timeout)
            wav = dc.audio_bytes(response, timeout=self._timeout)
        if not wav.startswith(b"RIFF"):
            raise RuntimeError("DashScope returned non-wav audio")
        self.call_count += 1
        self.last_speak_s = time.monotonic() - started
        usage = (response.get("usage") or {}).get("characters")
        print(json.dumps({
            "event": "synth",
            "chars": len(text),
            "usage_characters": usage,
            "seconds": round(self.last_speak_s, 3),
            "wav_bytes": len(wav),
        }, ensure_ascii=False), flush=True)
        return wav


def make_handler(tts: DashScopeTts):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet, synth logs are structured
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
                print(f"dashscope synth error: {exc}", file=sys.stderr,
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
    ap.add_argument("--model", default="cosyvoice-v3.5-flash")
    ap.add_argument("--voice-id", default="")
    ap.add_argument("--voice-id-env", default=dc.VOICE_ID_ENV)
    ap.add_argument("--workspace-id", default="")
    ap.add_argument("--workspace-id-env", default=dc.WORKSPACE_ID_ENV)
    ap.add_argument("--api-key-env", default=dc.API_KEY_ENV)
    ap.add_argument("--region", default=dc.DEFAULT_REGION)
    ap.add_argument("--endpoint", default="",
                    help="full SpeechSynthesizer URL override (tests)")
    ap.add_argument("--sample-rate", type=int, default=24000)
    ap.add_argument("--format", default="wav")
    ap.add_argument("--language", default="zh")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args(argv)

    dc.load_env_file()
    api_key = os.environ.get(args.api_key_env, "").strip()
    workspace = (args.workspace_id
                 or os.environ.get(args.workspace_id_env, "")).strip()
    voice_id = (args.voice_id
                or os.environ.get(args.voice_id_env, "")).strip()
    if not api_key:
        ap.error(f"missing {args.api_key_env} (environment or .env)")
    if not workspace and not args.endpoint:
        ap.error(f"missing {args.workspace_id_env} (environment or .env)")
    if not voice_id:
        ap.error("missing --voice-id / DASHSCOPE_VOICE_ID (run the clone first)")

    try:
        tts = DashScopeTts(
            api_key=api_key,
            workspace_id=workspace,
            model=args.model,
            voice_id=voice_id,
            region=args.region,
            endpoint=args.endpoint,
            sample_rate=args.sample_rate,
            audio_format=args.format,
            language=args.language,
            timeout=args.timeout,
        )
    except RuntimeError as exc:
        ap.error(str(exc))

    print(json.dumps({
        "engine": "dashscope-cosyvoice",
        "model": args.model,
        "voice_id": voice_id,
        "endpoint": tts.endpoint,
        "sample_rate": args.sample_rate,
        "format": args.format,
    }, ensure_ascii=False), flush=True)
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(tts))
    print(f"dashscope-tts-server listening on http://{args.host}:{args.port}",
          flush=True)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
