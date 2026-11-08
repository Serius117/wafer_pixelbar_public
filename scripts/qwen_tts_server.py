"""Qwen3-TTS native HTTP server with the wafer TTS contract.

The MaaS compatible-mode endpoint only serves chat/models; Qwen TTS must use
the native multimodal-generation endpoint:

POST /api/v1/services/aigc/multimodal-generation/generation
{"model": "qwen3-tts-flash",
 "input": {"text": "...", "voice": "Serena", "language_type": "Chinese"}}

The response contains a signed output.audio.url.  This adapter downloads it
before returning so the wafer /synthesize contract stays a synchronous WAV
response and never hands an expiring URL to OLV.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import queue
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

DEFAULT_ENDPOINT = (
    "https://maas.qianwenaiapi.com/api/v1/"
    "services/aigc/multimodal-generation/generation"
)
DEFAULT_MODEL = "qwen3-tts-flash"
DEFAULT_REALTIME_MODEL = "qwen3-tts-flash-realtime"
DEFAULT_VOICE = "Serena"
DEFAULT_WS_URL = "wss://cn-beijing.maas.qianwenaiapi.com/api-ws/v1/realtime"
API_KEY_ENV = "QWEN_MAAS_API_KEY"


def pcm_to_wav(pcm: bytes, sample_rate: int = 24000) -> bytes:
    """Wrap one PCM chunk in a standalone mono 16-bit WAV container."""
    data_len = len(pcm)
    header = (
        b"RIFF" + struct.pack("<I", 36 + data_len) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                                sample_rate * 2, 2, 16)
        + b"data" + struct.pack("<I", data_len)
    )
    return header + pcm


def _error_detail(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return text[:400] or "(empty body)"
    return str(obj.get("message") or obj.get("code") or text)[:400]


class QwenTts:
    """Qwen3-TTS native client; raises RuntimeError on failure."""

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str = DEFAULT_ENDPOINT,
        model: str = DEFAULT_MODEL,
        voice: str = DEFAULT_VOICE,
        sample_rate: int = 24000,
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise RuntimeError(f"missing {API_KEY_ENV}")
        if not voice:
            raise RuntimeError("missing Qwen TTS voice")
        self._api_key = api_key
        self._endpoint = endpoint
        self._model = model
        self._voice = voice
        self._sample_rate = int(sample_rate)
        self._timeout = float(timeout)
        self._lock = threading.Lock()
        self.last_speak_s: float | None = None
        self.call_count = 0

    def _post(self, text: str) -> dict:
        body = json.dumps({
            "model": self._model,
            "input": {
                "text": text,
                "voice": self._voice,
                "language_type": "Chinese",
            },
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"Qwen TTS HTTP {exc.code}: {_error_detail(exc.read())}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"Qwen TTS unreachable: {exc}") from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Qwen TTS returned non-JSON: {raw[:200]!r}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"Qwen TTS unexpected JSON: {data!r}")
        if data.get("code"):
            raise RuntimeError(
                f"Qwen TTS error {data.get('code')}: "
                f"{data.get('message', '')}")
        return data

    def _download(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"Accept": "audio/wav"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError,
                TimeoutError, OSError) as exc:
            raise RuntimeError(f"Qwen TTS audio download failed: {exc}") from exc

    def synthesize(self, text: str) -> bytes:
        text = (text or "").strip()
        if not text:
            return b""
        started = time.monotonic()
        with self._lock:
            data = self._post(text)
            output = data.get("output") or {}
            audio = output.get("audio") or {}
            if isinstance(audio, str):
                audio = {"url": audio}
            url = str(audio.get("url") or "").strip()
            if not url:
                raise RuntimeError("Qwen TTS response has no audio URL")
            wav = self._download(url)
        if not wav.startswith(b"RIFF"):
            raise RuntimeError("Qwen TTS returned non-wav audio")
        self.call_count += 1
        self.last_speak_s = time.monotonic() - started
        usage = data.get("usage") or {}
        print(json.dumps({
            "event": "synth",
            "chars": len(text),
            "usage": usage,
            "seconds": round(self.last_speak_s, 3),
            "wav_bytes": len(wav),
            "model": self._model,
            "voice": self._voice,
        }, ensure_ascii=False), flush=True)
        return wav


class QwenTtsRealtime:
    """Streaming Qwen3-TTS realtime client (PCM chunks -> WAV frames)."""

    supports_streaming = True

    def __init__(
        self,
        *,
        api_key: str,
        ws_url: str = DEFAULT_WS_URL,
        model: str = DEFAULT_REALTIME_MODEL,
        voice: str = DEFAULT_VOICE,
        sample_rate: int = 24000,
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise RuntimeError(f"missing {API_KEY_ENV}")
        self._api_key = api_key
        self._ws_url = ws_url
        self._model = model
        self._voice = voice
        self._sample_rate = int(sample_rate)
        self._timeout = float(timeout)
        self._lock = threading.Lock()
        self.last_first_audio_s: float | None = None
        try:
            import dashscope
            from dashscope.audio.qwen_tts_realtime import (
                AudioFormat,
                QwenTtsRealtimeCallback,
            )
            from dashscope.audio.qwen_tts_realtime import (
                QwenTtsRealtime as Client,
            )
            self._sdk = (dashscope, AudioFormat, Client,
                         QwenTtsRealtimeCallback)
        except ImportError:
            self._sdk = None

    def stream_pcm(self, text: str):
        text = (text or "").strip()
        if not text:
            return
        if self._sdk is None:
            raise RuntimeError(
                "dashscope package is required for Qwen realtime TTS")
        dashscope, AudioFormat, Client, QwenTtsRealtimeCallback = self._sdk

        chunks: queue.Queue = queue.Queue()
        done = object()

        class Callback(QwenTtsRealtimeCallback):
            def on_open(self) -> None:
                pass

            def on_close(self, close_status_code, close_msg) -> None:
                chunks.put(done)

            def on_event(self, response: dict) -> None:
                kind = response.get("type")
                if kind == "response.audio.delta":
                    delta = response.get("delta") or ""
                    if delta:
                        try:
                            chunks.put(base64.b64decode(delta))
                        except (ValueError, TypeError) as exc:
                            chunks.put(exc)
                elif kind == "session.finished":
                    chunks.put(done)

        dashscope.api_key = self._api_key
        with self._lock:
            client = Client(
                model=self._model,
                url=self._ws_url,
                callback=Callback(),
            )
            started = time.monotonic()
            try:
                client.connect()
                client.update_session(
                    voice=self._voice,
                    response_format=AudioFormat.PCM_24000HZ_MONO_16BIT,
                    mode="server_commit",
                    language_type="Chinese",
                )
                client.append_text(text)
                client.finish()
                first = True
                while True:
                    item = chunks.get(timeout=self._timeout)
                    if item is done:
                        break
                    if isinstance(item, Exception):
                        raise item
                    if first:
                        self.last_first_audio_s = time.monotonic() - started
                        print(json.dumps({
                            "event": "realtime_first_audio",
                            "seconds": round(self.last_first_audio_s, 3),
                            "model": self._model,
                            "voice": self._voice,
                        }, ensure_ascii=False), flush=True)
                        first = False
                    yield item
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"Qwen realtime TTS failed: {exc}") from exc
            finally:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass

    def stream_wav(self, text: str):
        for pcm in self.stream_pcm(text):
            yield pcm_to_wav(pcm, self._sample_rate)

    def synthesize(self, text: str) -> bytes:
        pcm = b"".join(self.stream_pcm(text))
        if not pcm:
            return b""
        return pcm_to_wav(pcm, self._sample_rate)


def make_handler(tts: QwenTts | QwenTtsRealtime):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
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
            if self.path == "/synthesize/frames" and hasattr(tts, "stream_wav"):
                # Real streaming path: each PCM delta becomes a standalone
                # WAV frame as soon as the upstream sends it.
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    for wav in tts.stream_wav(text):
                        if wav:
                            self.wfile.write(
                                struct.pack(">I", len(wav)) + wav)
                            self.wfile.flush()
                except Exception as exc:  # noqa: BLE001
                    print(f"qwen-tts stream error: {exc}", file=sys.stderr,
                          flush=True)
                self.close_connection = True
                return
            try:
                wav = tts.synthesize(text)
            except Exception as exc:  # noqa: BLE001 -- HTTP 500 is the contract
                print(f"qwen-tts synth error: {exc}", file=sys.stderr,
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
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--ws-url", default=DEFAULT_WS_URL)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--voice", default=DEFAULT_VOICE)
    ap.add_argument("--voice-env", default="QWEN_TTS_VOICE")
    ap.add_argument("--api-key-env", default=API_KEY_ENV)
    ap.add_argument("--sample-rate", type=int, default=24000)
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args(argv)

    load_env_file()
    api_key = os.environ.get(args.api_key_env, "").strip()
    voice = (args.voice
             or os.environ.get(args.voice_env, "")).strip()
    try:
        if "realtime" in args.model:
            tts = QwenTtsRealtime(
                api_key=api_key,
                ws_url=args.ws_url,
                model=args.model,
                voice=voice,
                sample_rate=args.sample_rate,
                timeout=args.timeout,
            )
        else:
            tts = QwenTts(
                api_key=api_key,
                endpoint=args.endpoint,
                model=args.model,
                voice=voice,
                sample_rate=args.sample_rate,
                timeout=args.timeout,
            )
    except RuntimeError as exc:
        ap.error(str(exc))

    print(json.dumps({
        "engine": "qwen3-tts-realtime"
        if isinstance(tts, QwenTtsRealtime) else "qwen3-tts",
        "model": args.model,
        "voice": voice,
        "endpoint": args.ws_url if isinstance(tts, QwenTtsRealtime)
        else args.endpoint,
        "sample_rate": args.sample_rate,
    }, ensure_ascii=False), flush=True)
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(tts))
    print(f"qwen-tts-server listening on http://{args.host}:{args.port}",
          flush=True)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
