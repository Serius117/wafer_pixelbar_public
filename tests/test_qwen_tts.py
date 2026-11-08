"""Qwen3-TTS native adapter tests (TEST.VOICE.07)."""
from __future__ import annotations

import io
import json
import struct
import sys
import threading
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.qwen_tts_server import QwenTts, make_handler  # noqa: E402


def silent_wav(seconds: float = 0.1, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


class Upstream:
    def __init__(self) -> None:
        self.payloads: list[dict] = []
        self.auth: list[str] = []
        self.audio = silent_wav()
        self.fail = False
        self.server = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/tts"

    @property
    def audio_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/audio.wav"


def _upstream():
    state = Upstream()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):  # noqa: N802
            if self.path != "/audio.wav":
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(state.audio)))
            self.end_headers()
            self.wfile.write(state.audio)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            state.payloads.append(json.loads(
                self.rfile.read(length).decode("utf-8")))
            state.auth.append(self.headers.get("Authorization", ""))
            if state.fail:
                body = json.dumps({"code": "InvalidApiKey",
                                   "message": "bad key"}).encode()
                self.send_response(400)
            else:
                body = json.dumps({
                    "output": {"audio": {"url": state.audio_url}},
                    "usage": {"characters": 4},
                    "request_id": "req-1",
                }).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.server = server
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return state, server


def test_pcm_to_wav_frame_header() -> None:
    from scripts.qwen_tts_server import pcm_to_wav

    pcm = b"\x01\x02" * 12
    wav = pcm_to_wav(pcm, 24000)
    assert wav.startswith(b"RIFF")
    assert wav[8:12] == b"WAVE"
    assert len(wav) == 44 + len(pcm)
    assert wav[44:] == pcm


def test_qwen_synthesize_payload_and_download() -> None:
    upstream, server = _upstream()
    try:
        tts = QwenTts(
            api_key="test-key", endpoint=upstream.url,
            model="qwen3-tts-flash", voice="Serena", timeout=5.0)
        wav = tts.synthesize("你好呀")
        assert wav.startswith(b"RIFF")
        assert upstream.auth == ["Bearer test-key"]
        assert upstream.payloads[0] == {
            "model": "qwen3-tts-flash",
            "input": {
                "text": "你好呀",
                "voice": "Serena",
                "language_type": "Chinese",
            },
        }
        assert tts.call_count == 1
    finally:
        server.shutdown()


def test_qwen_empty_text_no_request() -> None:
    upstream, server = _upstream()
    try:
        tts = QwenTts(api_key="test-key", endpoint=upstream.url)
        assert tts.synthesize(" ") == b""
        assert upstream.payloads == []
    finally:
        server.shutdown()


def test_qwen_api_error_does_not_leak_key() -> None:
    upstream, server = _upstream()
    try:
        upstream.fail = True
        tts = QwenTts(api_key="secret-key", endpoint=upstream.url)
        try:
            tts.synthesize("测试")
        except RuntimeError as exc:
            assert "HTTP 400" in str(exc)
            assert "secret-key" not in str(exc)
        else:
            raise AssertionError("expected RuntimeError")
    finally:
        server.shutdown()


def test_qwen_http_contract() -> None:
    upstream, server = _upstream()
    try:
        tts = QwenTts(api_key="test-key", endpoint=upstream.url)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tts))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/health", timeout=3) as r:
                assert r.status == 200 and r.read() == b"ok"
            req = urllib.request.Request(
                base + "/synthesize/frames", data=b'{"text":"hi"}',
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                body = r.read()
            size = struct.unpack(">I", body[:4])[0]
            assert size == len(body) - 4
            assert body[4:].startswith(b"RIFF")
        finally:
            httpd.shutdown()
    finally:
        server.shutdown()
