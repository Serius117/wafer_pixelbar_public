"""W.11 unit tests: LLM API client parsing + ASR script normalization.

Network-free: only pure functions are tested (SSE parsing, delta
extraction, config resolution fallbacks, traditional->simplified).
"""

import json

import pytest

from engines.asr import to_simplified
from engines.llm_api import (
    DEFAULT_MODEL,
    OpenAiCompatLlm,
    extract_delta_content,
    parse_sse_chunk,
)


class TestSseParsing:
    def test_data_line_parsed(self) -> None:
        chunk = 'data: {"choices":[{"delta":{"content":"你"}}]}'
        events = parse_sse_chunk(chunk)
        assert len(events) == 1
        assert extract_delta_content(events[0]) == "你"

    def test_done_marker_skipped(self) -> None:
        assert parse_sse_chunk("data: [DONE]") == []

    def test_non_data_lines_skipped(self) -> None:
        assert parse_sse_chunk("event: message") == []
        assert parse_sse_chunk("") == []

    def test_malformed_json_skipped(self) -> None:
        assert parse_sse_chunk("data: {broken") == []

    def test_empty_delta(self) -> None:
        events = parse_sse_chunk('data: {"choices":[{"delta":{}}]}')
        assert extract_delta_content(events[0]) == ""

    def test_no_choices(self) -> None:
        event = json.loads('{"choices":[]}')
        assert extract_delta_content(event) == ""

    def test_multiline_chunk(self) -> None:
        line_a = 'data: {"choices":[{"delta":{"content":"a"}}]}'
        line_b = 'data: {"choices":[{"delta":{"content":"b"}}]}'
        events = parse_sse_chunk(f"{line_a}\n{line_b}")
        assert [extract_delta_content(e) for e in events] == ["a", "b"]


class TestConfigResolution:
    def test_default_model(self) -> None:
        assert DEFAULT_MODEL == "deepseek-v4.1-flash"

    def test_explicit_args_win(self, monkeypatch) -> None:
        monkeypatch.setenv("WAFER_LLM_BASE_URL", "http://env-url")
        monkeypatch.setenv("WAFER_LLM_API_KEY", "env-key")
        llm = OpenAiCompatLlm(base_url="http://arg-url", api_key="arg-key",
                              model="test-model")
        assert llm.model == "test-model"
        assert llm._base_url == "http://arg-url"
        assert llm._api_key == "arg-key"

    def test_env_fallback(self, monkeypatch) -> None:
        monkeypatch.setenv("WAFER_LLM_BASE_URL", "http://env-url")
        monkeypatch.setenv("WAFER_LLM_API_KEY", "env-key")
        monkeypatch.setenv("WAFER_LLM_MODEL", "env-model")
        llm = OpenAiCompatLlm()
        assert llm.model == "env-model"
        assert llm._base_url == "http://env-url"

    def test_no_config_raises_helpfully(self, monkeypatch, tmp_path) -> None:
        from engines import llm_api

        monkeypatch.delenv("WAFER_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("WAFER_LLM_API_KEY", raising=False)
        monkeypatch.setattr(llm_api, "OPENCODE_CONFIGS",
                            (tmp_path / "nope.json", tmp_path / "nope.jsonc"))
        with pytest.raises(FileNotFoundError):
            OpenAiCompatLlm()


class TestJsoncConfig:
    """Home machine uses opencode.jsonc (comments + trailing commas)."""

    def _write(self, tmp_path, content: str):
        from engines import llm_api

        path = tmp_path / "opencode.jsonc"
        path.write_text(content, encoding="utf-8")
        monkey_targets = (tmp_path / "opencode.json", path)
        return path, monkey_targets, llm_api

    def test_jsonc_with_comments_and_trailing_commas(self, monkeypatch, tmp_path):
        _, configs, llm_api = self._write(tmp_path, """
        {
          // wafer home config
          "provider": {
            "huoshan": {
              "options": {
                "baseURL": "http://example:1/v1",
                "apiKey": "jsonc-key", /* inline */
              },
            },
          },
        }
        """)
        monkeypatch.setattr(llm_api, "OPENCODE_CONFIGS", configs)
        base, key = llm_api.load_opencode_provider("huoshan")
        assert base == "http://example:1/v1"
        assert key == "jsonc-key"

    def test_json_strict_first(self, monkeypatch, tmp_path):
        """Plain JSON files must never go through the stripping path."""
        import json as jsonlib

        _, configs, llm_api = self._write(tmp_path, jsonlib.dumps({
            "provider": {"huoshan": {"options": {
                "baseURL": "http://strict/v1", "apiKey": "k"}}}
        }))
        monkeypatch.setattr(llm_api, "OPENCODE_CONFIGS", configs)
        base, _ = llm_api.load_opencode_provider("huoshan")
        assert base == "http://strict/v1"

    def test_json_preferred_over_jsonc(self, monkeypatch, tmp_path):
        import json as jsonlib

        (tmp_path / "opencode.json").write_text(jsonlib.dumps({
            "provider": {"huoshan": {"options": {
                "baseURL": "http://json-first/v1", "apiKey": "k"}}}
        }), encoding="utf-8")
        path = tmp_path / "opencode.jsonc"
        path.write_text("{ broken", encoding="utf-8")
        _, configs, llm_api = self._write(tmp_path, "{ broken")
        (tmp_path / "opencode.json").write_text(jsonlib.dumps({
            "provider": {"huoshan": {"options": {
                "baseURL": "http://json-first/v1", "apiKey": "k"}}}
        }), encoding="utf-8")
        monkeypatch.setattr(llm_api, "OPENCODE_CONFIGS", configs)
        base, _ = llm_api.load_opencode_provider("huoshan")
        assert base == "http://json-first/v1"


class TestScriptNormalization:
    def test_traditional_to_simplified(self) -> None:
        """E2E finding: whisper zh output mixes scripts; router cues like
        "不对" must still match "不對...按鈕...顏色"."""
        pytest.importorskip("opencc")
        result = to_simplified("不對,是改按鈕的顏色")
        assert "不对" in result
        assert "按钮" in result
        assert "颜色" in result

    def test_already_simplified_unchanged(self) -> None:
        pytest.importorskip("opencc")
        assert to_simplified("派活，帮我修一下登录报错") == "派活，帮我修一下登录报错"

    def test_ascii_passthrough(self) -> None:
        pytest.importorskip("opencc")
        assert to_simplified("hello 123") == "hello 123"
