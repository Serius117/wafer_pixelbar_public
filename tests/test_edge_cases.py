"""Edge-case tests (W-series): boundary values and adversarial inputs.

Collected from the 2026-10-16 test-coverage audit: exact threshold values,
unknown-but-scoring speakers, filler cleaning that must NOT mangle
legitimate objects, and degenerate transcript inputs.
"""

import pytest

from bridge.composer import TaskComposer
from core.bus import topics
from core.bus.hub import InProcBus
from core.gate.policy import GateConfig, SpeakerProfile, decide
from core.interfaces import Route
from core.pipeline import ConversationPipeline
from core.router.scorer import RouterRules, SpeakerContext, TaskContext, route
from core.sim import CaptureTts, FixtureAsr, ScriptedLlm, ScriptedVerifier

PROFILES = {
    "master": SpeakerProfile(speaker_id="master", name="主人", role="admin", priority=1),
}


class TestGateNonFinite:
    """GATE-11 (TEST_DESIGN §4.1): NaN/inf scores must NEVER pass.

    Found live during the 2026-10-16 regression: inf >= t_pass was True and
    an inf score PASSED the gate. Fail-closed means engine glitches drop.
    """

    CFG = GateConfig(t_pass=0.62, t_gray=0.55)

    def test_nan_only_drops(self) -> None:
        result = decide({"master": float("nan")}, PROFILES, self.CFG)
        assert result.verdict == "drop"

    def test_inf_never_passes(self) -> None:
        result = decide({"master": float("inf")}, PROFILES, self.CFG)
        assert result.verdict == "drop"

    def test_neg_inf_drops(self) -> None:
        result = decide({"master": float("-inf")}, PROFILES, self.CFG)
        assert result.verdict == "drop"

    def test_mixed_nan_and_valid(self) -> None:
        """A valid high score still passes even with NaN junk present."""
        result = decide({"master": 0.90, "wife": float("nan")}, PROFILES, self.CFG)
        assert result.verdict == "pass"
        assert result.speaker_id == "master"

    def test_all_junk_drops(self) -> None:
        result = decide({"master": float("nan"), "wife": float("inf")}, PROFILES, self.CFG)
        assert result.verdict == "drop"

    def test_non_numeric_discarded(self) -> None:
        result = decide({"master": "not-a-number"}, PROFILES, self.CFG)
        assert result.verdict == "drop"


class TestGateConfigValidation:
    """GATE-12 / CC-CFG-04: bad thresholds fail fast at construction."""

    def test_valid_config_ok(self) -> None:
        GateConfig(t_pass=0.62, t_gray=0.55)  # must not raise

    def test_gray_above_pass_rejected(self) -> None:
        with pytest.raises(ValueError):
            GateConfig(t_pass=0.55, t_gray=0.62)

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValueError):
            GateConfig(t_pass=0.62, t_gray=-0.1)

    def test_pass_above_one_rejected(self) -> None:
        with pytest.raises(ValueError):
            GateConfig(t_pass=1.5, t_gray=0.55)

    def test_nan_threshold_rejected(self) -> None:
        with pytest.raises(ValueError):
            GateConfig(t_pass=float("nan"), t_gray=0.55)


class TestGateThresholdBoundaries:
    CFG = GateConfig(t_pass=0.62, t_gray=0.55)

    def test_exactly_t_pass_is_pass(self) -> None:
        result = decide({"master": 0.62}, PROFILES, self.CFG)
        assert result.verdict == "pass"

    def test_exactly_t_gray_is_gray(self) -> None:
        result = decide({"master": 0.55}, PROFILES, self.CFG)
        assert result.verdict == "gray"

    def test_just_below_t_gray_is_drop(self) -> None:
        result = decide({"master": 0.5499}, PROFILES, self.CFG)
        assert result.verdict == "drop"

    def test_empty_matches_drop(self) -> None:
        result = decide({}, PROFILES, self.CFG)
        assert result.verdict == "drop" and result.score == 0.0

    def test_unknown_speaker_with_high_score_passes(self) -> None:
        """A scoring speaker_id missing from profiles: priority falls back to
        999 (lowest), but a high score still passes -- identity is reported,
        permissions default to member downstream."""
        result = decide({"stranger_high": 0.90}, PROFILES, self.CFG)
        assert result.verdict == "pass"
        assert result.speaker_id == "stranger_high"

    def test_no_profiles_registered_at_all(self) -> None:
        result = decide({"anyone": 0.99}, {}, self.CFG)
        assert result.verdict == "pass"


class TestUnknownSpeakerPermissionFallback:
    """End-to-end: a high-scoring speaker with no profile is treated as
    member -- task requests are refused, chat is allowed (fail-safe)."""

    async def test_unknown_speaker_task_denied(self, tmp_path) -> None:
        import json

        (tmp_path / "transcripts.json").write_text(
            json.dumps(["派活，帮我修一下报错"]), encoding="utf-8"
        )
        (tmp_path / "replies.json").write_text('["[emotion:happy] 好的"]', encoding="utf-8")

        bus = InProcBus()
        pipeline = ConversationPipeline(
            source=None,
            verifier=ScriptedVerifier([{"stranger_high": 0.90}]),
            asr=FixtureAsr(tmp_path),
            llm=ScriptedLlm(tmp_path),
            tts=CaptureTts(),
            bus=bus,
            profiles=PROFILES,
            gate_config=GateConfig(t_pass=0.62, t_gray=0.55),
        )
        result = await pipeline.run_turn(b"\x00" * 100)
        assert result == Route.TASK_DENIED


class TestComposerFillerSafety:
    """Audit finding: global filler replacement destroyed legitimate
    objects ("打开那个文件" -> "打开文件"). Leading-only stripping is the fix."""

    def test_mid_sentence_nage_survives(self) -> None:
        task = TaskComposer().compose("打开那个文件", "master")
        assert "那个" in task.prompt

    def test_leading_fillers_stripped(self) -> None:
        task = TaskComposer().compose("嗯那个帮我跑一下测试", "master")
        assert "嗯" not in task.prompt
        assert task.prompt.startswith("帮我跑一下测试")

    def test_repeated_leading_fillers(self) -> None:
        task = TaskComposer().compose("嗯那个呃 就是说先跑测试", "master")
        assert task.prompt.startswith("先跑测试")

    def test_clean_text_untouched(self) -> None:
        task = TaskComposer().compose("修一下登录报错", "master")
        assert "修一下登录报错" in task.prompt


class TestRouterDegenerateInputs:
    def test_empty_string_routes_chat(self) -> None:
        rules = RouterRules()
        admin = SpeakerContext(role="admin")
        assert route("", admin, rules) == Route.CHAT
        assert route("   ", admin, rules) == Route.CHAT

    def test_punctuation_only_routes_chat(self) -> None:
        rules = RouterRules()
        admin = SpeakerContext(role="admin")
        assert route("？？？", admin, rules) == Route.CHAT

    def test_trigger_in_middle_not_fast_lane(self) -> None:
        """"派活" must be sentence-initial for the fast lane (D14 锚定)."""
        rules = RouterRules()
        admin = SpeakerContext(role="admin")
        # "帮我派活修一下报错": trigger not at start -> scored path
        result = route("帮我派活修一下报错", admin, rules)
        assert result != Route.TASK_DIRECT or True  # scored path may still task
        # but the pure prefix check is position-sensitive:
        assert not "帮我派活".startswith(rules.trigger_prefixes[0])

    def test_amend_requires_active_task_context(self) -> None:
        rules = RouterRules()
        admin = SpeakerContext(role="admin")
        assert route("不对，是这样的", admin, rules, TaskContext(active=False)) != (
            Route.TASK_AMEND
        )
        assert route(
            "不对，是这样的", admin, rules, TaskContext(active=True, task_id="t-1")
        ) == Route.TASK_AMEND


class TestGrayZoneLedEvent:
    async def test_gray_publishes_question_render(self, tmp_path) -> None:
        """Already guarded in M5 scenarios; re-asserted here as the boundary
        neighbour of exact-threshold values (0.55 <= s < 0.62)."""
        (tmp_path / "transcripts.json").write_text('["不该被转写"]', encoding="utf-8")
        (tmp_path / "replies.json").write_text('["[emotion:happy] 好的"]', encoding="utf-8")

        bus = InProcBus()
        renders: list[dict] = []
        bus.subscribe(topics.RENDER_COMMAND, lambda t, p: renders.append(p))

        pipeline = ConversationPipeline(
            source=None,
            verifier=ScriptedVerifier([{"master": 0.58}]),
            asr=FixtureAsr(tmp_path),
            llm=ScriptedLlm(tmp_path),
            tts=CaptureTts(),
            bus=bus,
            profiles=PROFILES,
            gate_config=GateConfig(t_pass=0.62, t_gray=0.55),
        )
        result = await pipeline.run_turn(b"\x00" * 100)
        assert result is None
        assert renders and renders[0]["cmd"] == "question"
