"""Tests for simulator implementations (core/sim)."""

from core.interfaces import HotkeyEvent, Message
from core.sim import (
    CaptureTts,
    FixtureAsr,
    FixtureVerifier,
    HotkeyFixtureSource,
    LogNotifier,
    ScriptedLlm,
)


class TestFixtureVerifier:
    def test_default_scores(self, tmp_path) -> None:
        v = FixtureVerifier(tmp_path)
        scores = v.scores(b"\x00" * 100)
        assert "master" in scores
        assert scores["master"] > 0.5

    def test_custom_scores(self, tmp_path) -> None:
        (tmp_path / "voiceprints.json").write_text('{"wife": 0.72}', encoding="utf-8")
        v = FixtureVerifier(tmp_path)
        assert v.scores(b"") == {"wife": 0.72}

    def test_embed_returns_sequence(self, tmp_path) -> None:
        v = FixtureVerifier(tmp_path)
        emb = v.embed(b"")
        assert len(emb) == 192
        assert all(x == 0.0 for x in emb)


class TestFixtureAsr:
    async def test_default_transcript(self, tmp_path) -> None:
        asr = FixtureAsr(tmp_path)
        text = await asr.transcribe(b"")
        assert text == "你好"

    async def test_sequential_transcripts(self, tmp_path) -> None:
        (tmp_path / "transcripts.json").write_text(
            '["第一句", "第二句", "第三句"]', encoding="utf-8"
        )
        asr = FixtureAsr(tmp_path)
        assert await asr.transcribe(b"") == "第一句"
        assert await asr.transcribe(b"") == "第二句"
        assert await asr.transcribe(b"") == "第三句"
        assert await asr.transcribe(b"") == "第一句"  # wraps around


class TestScriptedLlm:
    async def test_streams_reply(self, tmp_path) -> None:
        (tmp_path / "replies.json").write_text('["[emotion:happy] 哈哈"]', encoding="utf-8")
        llm = ScriptedLlm(tmp_path)
        chunks = [ch async for ch in llm.stream([Message("user", "hi")])]
        assert "".join(chunks) == "[emotion:happy] 哈哈"

    async def test_default_reply(self, tmp_path) -> None:
        llm = ScriptedLlm(tmp_path)
        chunks = [ch async for ch in llm.stream([])]
        assert "[emotion:" in "".join(chunks)


class TestCaptureTts:
    async def test_captures_text(self) -> None:
        tts = CaptureTts()
        async for _ in tts.speak("你好呀"):
            pass
        assert tts.captured == ["你好呀"]


class TestHotkeyFixtureSource:
    async def test_yields_events(self) -> None:
        source = HotkeyFixtureSource([HotkeyEvent(action="wake")])
        events = [e async for e in source.events()]
        assert len(events) == 1
        assert events[0].action == "wake"


class TestLogNotifier:
    async def test_logs_notification(self) -> None:
        n = LogNotifier()
        await n.notify("warn", "小饼打了个盹")
        assert n.notifications == [("warn", "小饼打了个盹")]
