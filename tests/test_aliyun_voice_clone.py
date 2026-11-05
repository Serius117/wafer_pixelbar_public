"""Voice-enrollment request tests (TEST.VOICE.05), mock HTTP only."""

from __future__ import annotations

import json
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.aliyun_voice_clone import (  # noqa: E402
    audio_data_uri,
    create_voice,
    query_voice,
)


def write_wav(path: Path, seconds: float = 5.0) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * int(seconds * 16000))


@pytest.fixture
def clone_server():
    state = {"payloads": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            state["payloads"].append(payload)
            action = payload["input"]["action"]
            if action == "create_voice":
                body = {"output": {"voice_id": "cosyvoice-v3.5-flash-xiaobing-1"},
                        "request_id": "req-1"}
            else:
                body = {"output": {"status": "OK"}, "request_id": "req-2"}
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/customization", state
    server.shutdown()


def test_create_voice_payload_and_voice_id(clone_server) -> None:
    endpoint, state = clone_server
    response = create_voice(
        api_key="k", workspace_id="", endpoint=endpoint,
        audio="data:audio/wav;base64,AAAA", prefix="xiaobing",
        target_model="cosyvoice-v3.5-flash", max_prompt_audio_length=10.0)
    assert response["output"]["voice_id"].endswith("-xiaobing-1")
    payload = state["payloads"][0]
    assert payload["model"] == "voice-enrollment"
    assert payload["input"] == {
        "action": "create_voice",
        "target_model": "cosyvoice-v3.5-flash",
        "prefix": "xiaobing",
        "audio": "data:audio/wav;base64,AAAA",
        "language_hints": ["zh"],
        "enable_preprocess": False,
        "enable_volume_normalization": "false",
        "max_prompt_audio_length": 10.0,
    }


def test_query_voice(clone_server) -> None:
    endpoint, state = clone_server
    response = query_voice(api_key="k", workspace_id="", endpoint=endpoint,
                           voice_id="voice-1")
    assert response["output"]["status"] == "OK"
    assert state["payloads"][0]["input"] == {
        "action": "query_voice", "voice_id": "voice-1"}


def test_local_wav_to_data_uri(tmp_path) -> None:
    path = tmp_path / "ref.wav"
    write_wav(path)
    uri = audio_data_uri(path)
    assert uri.startswith("data:audio/wav;base64,")
    assert len(uri) > 100


def test_prefix_validation() -> None:
    with pytest.raises(RuntimeError, match="ASCII alphanumeric"):
        create_voice(api_key="k", workspace_id="", endpoint="http://x",
                     audio="data:audio/wav;base64,AAAA", prefix="小饼")
    with pytest.raises(RuntimeError, match="ASCII alphanumeric"):
        create_voice(api_key="k", workspace_id="", endpoint="http://x",
                     audio="data:audio/wav;base64,AAAA", prefix="abcdefghijk")


def test_prompt_length_validation() -> None:
    with pytest.raises(RuntimeError, match="max_prompt_audio_length"):
        create_voice(api_key="k", workspace_id="", endpoint="http://x",
                     audio="data:audio/wav;base64,AAAA", prefix="xiaobing",
                     max_prompt_audio_length=2.0)
