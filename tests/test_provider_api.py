"""Online provider configuration and MiniMax TTS (TEST.PROVIDER.*).

All network calls are local stdlib mocks: CI needs no cloud key and no
internet. Real-key acceptance remains TASK.PROVIDER.03.
"""

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

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engines.provider_config import load_inference, resolve_llm, tts_profile  # noqa: E402
from scripts import serve_inference as si  # noqa: E402
from scripts import setup_olv  # noqa: E402
from scripts.minimax_tts_server import MiniMaxTts, make_handler  # noqa: E402


def silent_wav(seconds: float = 0.1, rate: int = 32000) -> bytes:
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
        self.groups: list[str] = []
        self.audio = silent_wav()
        self.mode = "ok"
        self.server = None

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1/t2a_v2"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def upstream():
    state = Upstream()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            state.payloads.append(json.loads(
                self.rfile.read(length).decode("utf-8")))
            state.auth.append(self.headers.get("Authorization", ""))
            state.groups.append(self.headers.get("GroupId", ""))
            if state.mode == "http_error":
                body = json.dumps({"base_resp": {
                    "status_code": 1004, "status_msg": "bad key"}}).encode()
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if state.mode == "api_error":
                body = json.dumps({"base_resp": {
                    "status_code": 1004, "status_msg": "bad voice"}}).encode()
            elif state.mode == "bad_audio":
                body = json.dumps({
                    "data": {"audio": "not-a-wav"},
                    "base_resp": {"status_code": 0},
                }).encode()
            elif state.mode == "not_json":
                body = b"<html>bad gateway</html>"
            else:
                body = json.dumps({
                    "data": {"audio": state.audio.hex()},
                    "extra_info": {"usage_characters": 4},
                    "base_resp": {"status_code": 0,
                                  "status_msg": "success"},
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


def make_client(upstream: Upstream) -> MiniMaxTts:
    return MiniMaxTts(
        api_key="test-key",
        voice_id="Chinese_crisp_podcaster_nv1",
        model="speech-2.8-hd",
        endpoint=upstream.endpoint,
        group_id="group-1",
        sample_rate=32000,
        timeout=5.0,
    )


class TestProviderConfig:
    def test_local_overlay_deep_merge(self, tmp_path) -> None:
        base = tmp_path / "inference.yaml"
        base.write_text(
            "llm:\n  provider: local\n  port: 8080\n"
            "tts:\n  provider: gpt_sovits\n  port: 9880\n",
            encoding="utf-8")
        overlay = tmp_path / "providers.local.yaml"
        overlay.write_text(
            "llm:\n  provider: openai_compat\n"
            "  openai_compat:\n    base_url: http://cloud/v1\n"
            "    model: deepseek-v4.1-flash\n"
            "tts:\n  provider: minimax\n",
            encoding="utf-8")
        conf = load_inference(base, overlay)
        assert conf["llm"]["provider"] == "openai_compat"
        assert conf["llm"]["port"] == 8080
        assert conf["llm"]["openai_compat"]["base_url"] == "http://cloud/v1"
        assert conf["tts"]["provider"] == "minimax"

    def test_explicit_smoke_config_skips_overlay(self, tmp_path,
                                                 monkeypatch) -> None:
        base = tmp_path / "inference.yaml"
        base.write_text("llm:\n  provider: local\n", encoding="utf-8")
        monkeypatch.setenv("WAFER_PROVIDER_CONFIG", "")
        conf = load_inference(base)
        assert conf["llm"]["provider"] == "local"

    def test_resolve_llm_env_wins(self, tmp_path, monkeypatch) -> None:
        conf = {
            "llm": {
                "provider": "openai_compat",
                "openai_compat": {
                    "base_url": "http://file/v1",
                    "model": "file-model",
                    "base_url_env": "TEST_LLM_BASE",
                    "model_env": "TEST_LLM_MODEL",
                },
            },
        }
        monkeypatch.setenv("TEST_LLM_BASE", "http://env/v1")
        monkeypatch.setenv("TEST_LLM_MODEL", "env-model")
        assert resolve_llm(conf) == (
            "openai_compat", "http://env/v1", "env-model")

    def test_tts_profile_uses_nested_provider(self) -> None:
        conf = {"tts": {"provider": "minimax", "port": 9880,
                        "minimax": {"voice_id": "v1", "sample_rate": 32000}}}
        provider, profile = tts_profile(conf)
        assert provider == "minimax"
        assert profile["voice_id"] == "v1"
        assert profile["port"] == 9880


class TestMiniMax:
    def test_synthesize_request_and_wav(self, upstream) -> None:
        client = make_client(upstream)
        wav = client.synthesize("你好呀")
        assert wav.startswith(b"RIFF")
        assert upstream.auth == ["Bearer test-key"]
        assert upstream.groups == ["group-1"]
        payload = upstream.payloads[0]
        assert payload["model"] == "speech-2.8-hd"
        assert payload["text"] == "你好呀"
        assert payload["voice_setting"]["voice_id"] == (
            "Chinese_crisp_podcaster_nv1")
        assert payload["audio_setting"]["format"] == "wav"
        assert payload["audio_setting"]["sample_rate"] == 32000
        assert client.call_count == 1

    def test_empty_text_makes_no_request(self, upstream) -> None:
        assert make_client(upstream).synthesize("  ") == b""
        assert upstream.payloads == []

    @pytest.mark.parametrize("mode,needle", [
        ("http_error", "HTTP 401"),
        ("api_error", "MiniMax error 1004"),
        ("bad_audio", "not valid hex/base64"),
        ("not_json", "non-JSON"),
    ])
    def test_failures_raise_without_leaking_key(self, upstream, mode,
                                                needle) -> None:
        upstream.mode = mode
        with pytest.raises(RuntimeError) as exc:
            make_client(upstream).synthesize("测试")
        assert needle in str(exc.value)
        assert "test-key" not in str(exc.value)

    def test_http_contract_health_and_frames(self, upstream) -> None:
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


class TestProviderWiring:
    def test_minimax_command_and_remote_llm_skip(self) -> None:
        conf = {
            "llama_server": "tools/llama/llama-server.exe",
            "llm": {"provider": "openai_compat"},
            "tts": {
                "provider": "minimax",
                "host": "127.0.0.1",
                "port": 9880,
                "python": ".venv/Scripts/python.exe",
                "server": "scripts/minimax_tts_server.py",
                "minimax": {
                    "model": "speech-2.8-hd",
                    "voice_id": "Chinese_crisp_podcaster_nv1",
                    "endpoint": "https://api.minimax.cn/v1/t2a_v2",
                    "api_key_env": "MINIMAX_API_KEY",
                    "group_id_env": "MINIMAX_GROUP_ID",
                    "sample_rate": 32000,
                    "format": "wav",
                    "timeout": 60,
                },
            },
        }
        cmd = si._cmd(conf, "tts")
        assert cmd[1].endswith("minimax_tts_server.py")
        assert cmd[cmd.index("--voice-id") + 1] == (
            "Chinese_crisp_podcaster_nv1")
        assert cmd[cmd.index("--api-key-env") + 1] == "MINIMAX_API_KEY"
        assert "--prompt-wav" not in cmd
        assert not si._enabled(conf, "llm")
        assert si._enabled(conf, "asr")

    def test_setup_olv_applies_cloud_config(self, monkeypatch) -> None:
        from engines import provider_config

        inf = {
            "llm": {
                "provider": "openai_compat",
                "openai_compat": {
                    "base_url": "https://cloud/v1",
                    "model": "deepseek-v4.1-flash",
                    "api_key_env": "TEST_PROVIDER_KEY",
                },
            },
        }
        monkeypatch.setattr(provider_config, "load_env_file", lambda: {})
        monkeypatch.setattr(provider_config, "load_inference", lambda: inf)
        monkeypatch.setenv("TEST_PROVIDER_KEY", "secret-key")
        conf = {"character_config": {"agent_config": {"llm_configs": {
            "openai_compatible_llm": {}}}}}
        status = setup_olv._apply_cloud_llm(conf, "auto")
        llm = conf["character_config"]["agent_config"]["llm_configs"][
            "openai_compatible_llm"]
        assert "cloud deepseek-v4.1-flash" in status
        assert llm["base_url"] == "https://cloud/v1"
        assert llm["model"] == "deepseek-v4.1-flash"
        assert llm["llm_api_key"] == "secret-key"
        assert "secret-key" not in status


def test_secret_files_are_gitignored() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in ignored
    assert "config/providers.local.yaml" in ignored
    assert "config/*.local.yaml" in ignored
