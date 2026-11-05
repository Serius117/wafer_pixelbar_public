"""M5 tests (T5.5): gate false-trigger regression scenarios.

Scenarios per design doc §4.2 and requirement 误触=0:
  - stranger (all scores below t_gray) -> silent drop, ASR never called
  - gray zone (t_gray <= s < t_pass) -> drop + LED "?" render command
  - multi-person arbitration -> registration priority wins over raw score
  - member task intent -> TASK_DENIED + refusal TTS
"""

from core.bus import topics
from core.bus.hub import InProcBus
from core.gate.policy import GateConfig, SpeakerProfile
from core.interfaces import Route
from core.pipeline import ConversationPipeline
from core.sim import CaptureTts, FixtureAsr, ScriptedLlm, ScriptedVerifier

PROFILES = {
    "master": SpeakerProfile(speaker_id="master", name="主人", role="admin", priority=1),
    "wife": SpeakerProfile(speaker_id="wife", name="老婆", role="member", priority=2),
}

GATE_CFG = GateConfig(t_pass=0.62, t_gray=0.55)


class AsrSpy:
    """ASR wrapper that records whether it was ever called."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.called = 0

    async def transcribe(self, pcm: bytes) -> str:
        self.called += 1
        return await self._inner.transcribe(pcm)


def make_pipeline(
    score_sets: list[dict[str, float]],
    transcripts: list[str],
    replies: list[str] | None = None,
) -> tuple[ConversationPipeline, AsrSpy, CaptureTts, InProcBus]:
    import json
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    (tmp / "transcripts.json").write_text(json.dumps(transcripts), encoding="utf-8")
    (tmp / "replies.json").write_text(
        json.dumps(replies or ["[emotion:happy] 好的"]), encoding="utf-8"
    )

    bus = InProcBus()
    asr_spy = AsrSpy(FixtureAsr(tmp))
    tts = CaptureTts()
    pipeline = ConversationPipeline(
        source=None,
        verifier=ScriptedVerifier(score_sets),
        asr=asr_spy,
        llm=ScriptedLlm(tmp),
        tts=tts,
        bus=bus,
        profiles=PROFILES,
        gate_config=GATE_CFG,
    )
    return pipeline, asr_spy, tts, bus


class TestStrangerScenario:
    async def test_stranger_silent_drop(self) -> None:
        """All scores below t_gray: dropped, nothing downstream runs."""
        pipeline, asr, tts, bus = make_pipeline(
            score_sets=[{"master": 0.30, "wife": 0.20}],
            transcripts=["绝不该被转写的话"],
        )
        gate_events: list[dict] = []
        bus.subscribe(topics.GATE_DECISION, lambda t, p: gate_events.append(p))

        result = await pipeline.run_turn(b"\x00" * 100)

        assert result is None
        assert asr.called == 0
        assert tts.captured == []
        assert gate_events[0]["verdict"] == "drop"


class TestGrayZoneScenario:
    async def test_gray_drop_with_led_question(self) -> None:
        """Gray zone: drop + render.command(question) published (T5.2)."""
        pipeline, asr, tts, bus = make_pipeline(
            score_sets=[{"master": 0.58}],
            transcripts=["不该被转写"],
        )
        render_events: list[dict] = []
        bus.subscribe(topics.RENDER_COMMAND, lambda t, p: render_events.append(p))
        gate_events: list[dict] = []
        bus.subscribe(topics.GATE_DECISION, lambda t, p: gate_events.append(p))

        result = await pipeline.run_turn(b"\x00" * 100)

        assert result is None
        assert asr.called == 0
        assert gate_events[0]["verdict"] == "gray"
        assert len(render_events) == 1
        assert render_events[0]["cmd"] == "question"
        assert render_events[0]["duration_ms"] == 3000

    async def test_just_below_gray_is_silent(self) -> None:
        """Score just under t_gray: still dropped but no LED event."""
        pipeline, asr, _, bus = make_pipeline(
            score_sets=[{"master": 0.54}],
            transcripts=["x"],
        )
        render_events: list[dict] = []
        bus.subscribe(topics.RENDER_COMMAND, lambda t, p: render_events.append(p))

        result = await pipeline.run_turn(b"\x00" * 100)

        assert result is None
        assert render_events == []


class TestMultiPersonArbitration:
    async def test_priority_beats_raw_score(self) -> None:
        """Both pass threshold: master (priority 1) wins even with lower score."""
        pipeline, asr, _, bus = make_pipeline(
            score_sets=[{"master": 0.70, "wife": 0.85}],
            transcripts=["今天天气怎么样"],
            replies=["[emotion:happy] 挺好的呀"],
        )
        gate_events: list[dict] = []
        bus.subscribe(topics.GATE_DECISION, lambda t, p: gate_events.append(p))

        result = await pipeline.run_turn(b"\x00" * 100)

        assert result == Route.CHAT
        assert gate_events[0]["verdict"] == "pass"
        assert gate_events[0]["speaker_id"] == "master"  # priority, not max score

    async def test_wife_alone_passes_as_member(self) -> None:
        pipeline, _, _, bus = make_pipeline(
            score_sets=[{"master": 0.30, "wife": 0.80}],
            transcripts=["聊聊天吧"],
            replies=["[emotion:happy] 好呀"],
        )
        gate_events: list[dict] = []
        bus.subscribe(topics.GATE_DECISION, lambda t, p: gate_events.append(p))

        result = await pipeline.run_turn(b"\x00" * 100)

        assert result == Route.CHAT
        assert gate_events[0]["speaker_id"] == "wife"

    async def test_member_task_denied(self) -> None:
        """D12: member voice + task intent -> TASK_DENIED + refusal TTS."""
        pipeline, _, tts, bus = make_pipeline(
            score_sets=[{"wife": 0.80}],
            transcripts=["派活，帮我修一下报错"],
        )
        gate_events: list[dict] = []
        bus.subscribe(topics.GATE_DECISION, lambda t, p: gate_events.append(p))

        result = await pipeline.run_turn(b"\x00" * 100)

        assert result == Route.TASK_DENIED
        assert gate_events[0]["speaker_id"] == "wife"
        assert len(tts.captured) == 1  # the deterministic refusal
        assert tts.captured[0] == "这个要主人来才行哦"

    async def test_admin_task_passes_gate(self) -> None:
        pipeline, _, _, _ = make_pipeline(
            score_sets=[{"master": 0.75}],
            transcripts=["派活，帮我修一下报错"],
        )
        result = await pipeline.run_turn(b"\x00" * 100)
        assert result == Route.TASK_DIRECT


class TestScriptedVerifier:
    def test_exhausts_and_repeats_last(self) -> None:
        from core.sim import ScriptedVerifier

        v = ScriptedVerifier([{"a": 0.9}, {"a": 0.1}])
        assert v.scores(b"")["a"] == 0.9
        assert v.scores(b"")["a"] == 0.1
        assert v.scores(b"")["a"] == 0.1  # repeats last

    def test_empty_raises(self) -> None:
        import pytest

        from core.sim import ScriptedVerifier

        with pytest.raises(ValueError):
            ScriptedVerifier([])
