"""TTS provider command assembly (TEST.VOICE.01)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import serve_inference as si  # noqa: E402


def _local_conf(provider: str = "gpt_sovits") -> dict:
    return {
        "llama_server": "tools/llama/llama-server.exe",
        "tts": {
            "provider": provider,
            "host": "127.0.0.1",
            "port": 9880,
            "python": "third_party/GPT-SoVITS/.venv/Scripts/python.exe",
            "server": "scripts/gpt_sovits_server.py",
            "model_dir": "models/tts/gpt",
            "prompt_wav": "data/prompts/ref.wav",
            "prompt_text": "参考文本",
            "device": "cuda",
            "fp16": True,
            "extra_args": [],
        },
    }


def test_gpt_sovits_command_unchanged() -> None:
    cmd = si._cmd(_local_conf(), "tts")
    assert cmd[1].endswith("gpt_sovits_server.py")
    assert "--model-dir" in cmd
    assert "--prompt-wav" in cmd
    assert cmd[-1] == "--fp16"


def test_aliyun_provider_command() -> None:
    conf = _local_conf()
    conf["tts"]["aliyun_cosyvoice"] = {
        "python": ".venv/Scripts/python.exe",
        "server": "scripts/dashscope_tts_server.py",
        "model": "cosyvoice-v3.5-flash",
        "voice_id": "cloned-voice",
        "region": "cn-beijing",
        "api_key_env": "DASHSCOPE_API_KEY",
        "workspace_id_env": "DASHSCOPE_WORKSPACE_ID",
        "voice_id_env": "DASHSCOPE_VOICE_ID",
        "sample_rate": 24000,
        "format": "wav",
        "language": "zh",
        "timeout": 60,
        "extra_args": [],
    }
    conf["tts"]["provider"] = "aliyun_cosyvoice"
    cmd = si._cmd(conf, "tts")
    assert cmd[1].endswith("dashscope_tts_server.py")
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == (
        "cosyvoice-v3.5-flash")
    assert cmd[cmd.index("--voice-id") + 1] == "cloned-voice"
    assert cmd[cmd.index("--region") + 1] == "cn-beijing"
    assert cmd[cmd.index("--api-key-env") + 1] == "DASHSCOPE_API_KEY"
    assert "--prompt-wav" not in cmd
    assert "--device" not in cmd
    assert "--fp16" not in cmd


def test_unknown_provider_rejected() -> None:
    with pytest.raises(SystemExit, match="unknown tts provider"):
        si._cmd(_local_conf("bogus"), "tts")


def test_repo_configs_build_tts_commands() -> None:
    for name in ("inference.yaml", "inference.smoke.yaml"):
        conf = si.load_config(ROOT / "config" / name)
        cmd = si._cmd(conf, "tts")
        assert "--host" in cmd and "--port" in cmd
        assert "--model-dir" in cmd
