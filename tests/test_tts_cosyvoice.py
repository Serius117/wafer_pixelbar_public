"""CosyVoiceTts engine tests (TTS-03): against a mock HTTP server.

CI never needs the real CosyVoice stack -- the engine is a thin HTTP
client, so a stdlib mock returning a silent wav exercises the whole
contract. Real-voice verification is TTS-03/HOME (see TEST_DESIGN).
"""

import asyncio
import io
import json
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from engines.tts_cosyvoice import CosyVoiceTts


def silent_wav(seconds: float = 0.1, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


@pytest.fixture
def mock_server():
    """A stand-in cosyvoice_server returning a silent wav."""
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            pass

        def do_GET(self):  # noqa: N802
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            seen.append(body)
            if not str(body.get("text", "")).strip():
                self.send_response(400)
                self.end_headers()
                return
            wav = silent_wav()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav)))
            self.end_headers()
            self.wfile.write(wav)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", seen
    server.shutdown()


class TestCosyVoiceTts:
    async def test_speak_yields_wav(self, mock_server) -> None:
        url, seen = mock_server
        tts = CosyVoiceTts(url=url)
        chunks = [c async for c in tts.speak("你好呀，主人")]
        assert len(chunks) == 1
        assert chunks[0].startswith(b"RIFF")
        assert seen == [{"text": "你好呀，主人"}]
        assert tts.call_count == 1
        assert tts.last_speak_s is not None

    async def test_empty_text_says_nothing(self, mock_server) -> None:
        url, seen = mock_server
        tts = CosyVoiceTts(url=url)
        chunks = [c async for c in tts.speak("   ")]
        assert chunks == []
        assert seen == []  # no request made

    async def test_unreachable_server_raises(self) -> None:
        tts = CosyVoiceTts(url="http://127.0.0.1:9", timeout=0.5)
        with pytest.raises(RuntimeError, match="unreachable"):
            async for _ in tts.speak("测试"):
                pass

    async def test_non_wav_payload_raises(self, mock_server) -> None:
        url, _ = mock_server
        # corrupt the mock: return text on /synthesize via a second server
        class BadHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *a):
                pass

            def do_POST(self):  # noqa: N802
                body = b"not-a-wav"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        bad = ThreadingHTTPServer(("127.0.0.1", 0), BadHandler)
        threading.Thread(target=bad.serve_forever, daemon=True).start()
        # Wait for the HTTP handler, not just the listening socket: under
        # full-suite CPU contention a bare TCP connect can succeed before
        # serve_forever handles requests, flipping the assertion into
        # "unreachable" instead of "non-wav" (observed 2026-10-31).
        import time as _time

        import httpx

        bad_url = f"http://127.0.0.1:{bad.server_address[1]}"
        for _ in range(200):
            try:
                httpx.get(f"{bad_url}/health", timeout=0.2)
                break
            except Exception:  # noqa: BLE001 -- server still starting
                _time.sleep(0.05)
        else:
            raise AssertionError("bad mock server did not become ready")
        try:
            tts = CosyVoiceTts(url=bad_url)
            with pytest.raises(RuntimeError, match="non-wav"):
                async for _ in tts.speak("测试"):
                    pass
        finally:
            bad.shutdown()

    async def test_engine_in_pipeline_silent_mode_contract(self, mock_server) -> None:
        """The engine satisfies the pipeline's TtsEngine shape: async
        iterator of bytes; degradation path is rule-6 on RuntimeError."""
        url, _ = mock_server
        tts = CosyVoiceTts(url=url)
        got = []
        async for chunk in tts.speak("收到，这就开工"):
            got.append(chunk)
        assert len(got) == 1
        await asyncio.sleep(0)  # loop hygiene
