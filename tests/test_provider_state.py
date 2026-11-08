"""Runtime local/online provider state (TEST.PROVIDER.08)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engines import provider_state  # noqa: E402


def test_default_is_online(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WAFER_PROVIDER_STATE", str(tmp_path / "missing.json"))
    assert provider_state.load_state() == {
        "llm": "online", "tts": "online", "asr": "cpu"}


def test_save_load_and_apply(tmp_path, monkeypatch) -> None:
    path = tmp_path / "state.json"
    monkeypatch.setenv("WAFER_PROVIDER_STATE", str(path))
    saved = provider_state.save_state(
        {"llm": "local", "tts": "online", "asr": "gpu"})
    assert saved == {"llm": "local", "tts": "online", "asr": "gpu"}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded == saved
    conf = provider_state.apply_state({
        "llm": {"provider": "openai_compat"},
        "tts": {"provider": "qwen_tts"},
        "asr": {},
    })
    assert conf["llm"]["provider"] == "local"
    assert conf["tts"]["provider"] == "qwen_tts"
    assert conf["asr"]["device"] == "gpu"


def test_invalid_asr_device_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WAFER_PROVIDER_STATE", str(tmp_path / "state.json"))
    try:
        provider_state.save_state({"asr": "tpu"})
    except ValueError as exc:
        assert "invalid asr" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_invalid_mode_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WAFER_PROVIDER_STATE", str(tmp_path / "state.json"))
    try:
        provider_state.save_state({"llm": "bogus"})
    except ValueError as exc:
        assert "invalid llm" in str(exc)
    else:
        raise AssertionError("expected ValueError")
