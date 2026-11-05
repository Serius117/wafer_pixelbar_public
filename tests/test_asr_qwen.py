"""Unit tests for the Qwen3-ASR engine client (network-free).

Covers the three observed output formats, PCM wrapping, and hotword
payload construction. Live HTTP behaviour is exercised by the E2E/probe
scripts (dev machine with llama-server running).
"""

import base64
import io
import wave

from engines.asr_qwen import (
    QwenLlamaServerAsr,
    build_payload,
    parse_qwen_asr_output,
    pcm_to_wav_b64,
)


class TestParseQwenOutput:
    def test_prefix_lang_format(self) -> None:
        """llama-server observed format: 'language Chinese<asr_text>正文'."""
        assert parse_qwen_asr_output(
            "language Chinese<asr_text>派活，帮我修一下登录报错"
        ) == "派活，帮我修一下登录报错"

    def test_lang_tag_format(self) -> None:
        """vLLM/qwen-asr package format: '<|zh|>正文'."""
        assert parse_qwen_asr_output("<|zh|>不对，是改按钮的颜色") == "不对，是改按钮的颜色"

    def test_bare_text_passthrough(self) -> None:
        assert parse_qwen_asr_output("今天好累呀") == "今天好累呀"

    def test_residual_special_tokens_stripped(self) -> None:
        """skip_special_tokens=False leaks im_end etc. -- must be cleaned."""
        assert parse_qwen_asr_output("language Chinese<asr_text>晚安<|im_end|>") == "晚安"

    def test_quotes_stripped(self) -> None:
        assert parse_qwen_asr_output('"好的"') == "好的"

    def test_empty(self) -> None:
        assert parse_qwen_asr_output("") == ""
        assert parse_qwen_asr_output(None) == ""  # type: ignore[arg-type]


class TestPcmWrap:
    def test_pcm_to_wav_b64_roundtrip(self) -> None:
        pcm = bytes([0x00, 0x40, 0xFF, 0x7F, 0x00, 0x00])
        b64 = pcm_to_wav_b64(pcm)
        raw = base64.b64decode(b64)
        with wave.open(io.BytesIO(raw), "rb") as w:
            assert w.getframerate() == 16000
            assert w.getnchannels() == 1
            assert w.getsampwidth() == 2
            assert w.readframes(w.getnframes()) == pcm


class TestPayload:
    def test_audio_part_shape(self) -> None:
        payload = build_payload("qwen3-asr", "QUJD", hotwords=[])
        content = payload["messages"][0]["content"]
        assert content[0]["type"] == "input_audio"
        assert content[0]["input_audio"]["format"] == "wav"
        assert payload["temperature"] == 0.0
        assert payload["stream"] is False

    def test_hotwords_appended_as_text(self) -> None:
        payload = build_payload("m", "QUJD", hotwords=["派活", "小饼"])
        content = payload["messages"][0]["content"]
        assert len(content) == 2
        assert content[1]["type"] == "text"
        assert "派活" in content[1]["text"]

    def test_no_hotwords_single_part(self) -> None:
        payload = build_payload("m", "QUJD", hotwords=[])
        assert len(payload["messages"][0]["content"]) == 1


class TestEngineDefaults:
    def test_construction(self) -> None:
        asr = QwenLlamaServerAsr(url="http://127.0.0.1:8001/", hotwords=["派活"])
        assert asr._url == "http://127.0.0.1:8001"
        assert asr._hotwords == ["派活"]
        assert asr.last_transcribe_s is None
        assert asr.call_count == 0
