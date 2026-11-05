"""DashScope CosyVoice adapter tests (TEST.VOICE.02/03/04).

All external calls are served by a local stdlib mock: CI needs no API key and
no network.  The real clone/synthesis is TASK.VOICE.03b (HOME).
"""

from __future__ import annotations

import base64
import io
import json
import struct
import sys
import threading
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.dashscope_common import audio_bytes  # noqa: E402
from scripts.dashscope_tts_server import DashScopeTts, make_handler  # noqa: E402


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
        self.fail = False
        self.audio = silent_wav()
        self.audio_url = ""

    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/tts"


@pytest.fixture
def upstream():
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
            state.payloads.append(
                json.loads(self.rfile.read(length).decode("utf-8")))
            state.auth.append(self.headers.get("Authorization", ""))
            if state.fail:
                body = json.dumps({"code": "InvalidApiKey",
                                   "message": "bad key"}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            state.audio_url = (
                f"http://127.0.0.1:{self.server.server_address[1]}/audio.wav")
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
    yield state
    server.shutdown()


def make_client(upstream: Upstream) -> DashScopeTts:
    return DashScopeTts(
        api_key="test-key", workspace_id="", endpoint=upstream.url(),
        model="cosyvoice-v3.5-flash", voice_id="cloned-voice",
        sample_rate=24000, timeout=5.0)


class TestDashScopeTts:
    def test_synthesize_requests_and_downloads_wav(self, upstream) -> None:
        client = make_client(upstream)
        wav = client.synthesize("你好呀")
        assert wav.startswith(b"RIFF")
        assert upstream.payloads[0] == {
            "model": "cosyvoice-v3.5-flash",
            "input": {
                "text": "你好呀",
                "voice": "cloned-voice",
                "format": "wav",
                "sample_rate": 24000,
                "language_hints": ["zh"],
            },
        }
        assert upstream.auth == ["Bearer test-key"]
        assert client.call_count == 1
        assert client.last_speak_s is not None

    def test_empty_text_makes_no_request(self, upstream) -> None:
        client = make_client(upstream)
        assert client.synthesize("  ") == b""
        assert upstream.payloads == []

    def test_api_error_raises_without_leaking_key(self, upstream) -> None:
        upstream.fail = True
        client = make_client(upstream)
        with pytest.raises(RuntimeError) as exc:
            client.synthesize("测试")
        assert "HTTP 400" in str(exc.value)
        assert "bad key" in str(exc.value)
        assert "test-key" not in str(exc.value)

    def test_non_wav_audio_raises(self, upstream) -> None:
        upstream.audio = b"not-a-wav"
        client = make_client(upstream)
        with pytest.raises(RuntimeError, match="non-wav"):
            client.synthesize("测试")

    def test_data_uri_fallback(self) -> None:
        wav = silent_wav()
        response = {"output": {"audio": {
            "data": "data:audio/wav;base64,"
                    + base64.b64encode(wav).decode()}}}
        assert audio_bytes(response) == wav

    def test_http_contract_frames_and_health(self, upstream) -> None:
        client = make_client(upstream)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(client))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with urllib.request.urlopen(base + "/health", timeout=3) as r:
                assert r.status == 200 and r.read() == b"ok"
            req = urllib.request.Request(
                base + "/synthesize/frames",
                data=b'{"text":"hi"}',
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                body = r.read()
            size = struct.unpack(">I", body[:4])[0]
            assert size == len(body) - 4
            assert body[4:].startswith(b"RIFF")
        finally:
            httpd.shutdown()
