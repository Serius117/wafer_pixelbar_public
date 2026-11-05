"""Tests for conversation pipeline + render + scenario replay (M1 skeleton)."""

import json
from pathlib import Path

from core.bus.hub import InProcBus
from core.gate.policy import SpeakerProfile
from core.interfaces import Route, SessionState
from core.pipeline import ConversationPipeline, parse_emotion_tag
from core.session.state_machine import Event
from core.sim import CaptureTts, FixtureAsr, FixtureVerifier, ScriptedLlm
from render.pipeline import Frame, RenderPipeline, SimpleFace

# --- Shared fixtures ---
PROFILES = {
    "master": SpeakerProfile(speaker_id="master", name="主人", role="admin", priority=1),
}


def make_pipeline(
    tmp_path: Path, transcripts: list[str], replies: list[str]
) -> tuple[ConversationPipeline, CaptureTts, InProcBus]:
    (tmp_path / "voiceprints.json").write_text('{"master": 0.75}', encoding="utf-8")
    (tmp_path / "transcripts.json").write_text(json.dumps(transcripts), encoding="utf-8")
    (tmp_path / "replies.json").write_text(json.dumps(replies), encoding="utf-8")

    bus = InProcBus()
    tts = CaptureTts()

    pipeline = ConversationPipeline(
        source=None,  # not used in direct run_turn calls
        verifier=FixtureVerifier(tmp_path),
        asr=FixtureAsr(tmp_path),
        llm=ScriptedLlm(tmp_path),
        tts=tts,
        bus=bus,
        profiles=PROFILES,
    )
    return pipeline, tts, bus


class TestParseEmotionTag:
    def test_extracts_tag(self) -> None:
        tag, rest = parse_emotion_tag("[emotion:happy] 你好")
        assert tag == "happy"
        assert rest == " 你好"

    def test_no_tag(self) -> None:
        tag, rest = parse_emotion_tag("普通文本")
        assert tag is None
        assert rest == "普通文本"

    def test_malformed_tag(self) -> None:
        tag, rest = parse_emotion_tag("[emotion:")
        assert tag is None

    def test_unknown_tag_falls_back_to_normal(self) -> None:
        """TASK.CORE.01: local 9B occasionally invents tags like curious."""
        tag, rest = parse_emotion_tag("[emotion:curious] 你好呀")
        assert tag == "normal"
        assert rest == " 你好呀"


class TestChatSystemPrompt:
    def test_prompt_has_semantics_rules_and_few_shot(self) -> None:
        from core.pipeline import build_chat_system_prompt

        prompt = build_chat_system_prompt("小饼")
        assert "小饼" in prompt
        for tag in ("normal", "happy", "sad", "angry"):
            assert tag in prompt
        assert "标签含义" in prompt
        assert "判断规则" in prompt
        assert "禁止自造" in prompt
        # Format spec + >=12 few-shot lines (TASK.CORE.01).
        assert prompt.count("[emotion:") >= 12


class TestPipelineChatTurn:
    async def test_full_chat_turn(self, tmp_path) -> None:
        """ASR -> route(chat) -> LLM -> emotion -> TTS."""
        pipeline, tts, bus = make_pipeline(
            tmp_path,
            transcripts=["今天好累啊"],
            replies=["[emotion:sad] 辛苦啦，休息一下吧"],
        )

        # Simulate a hotkey wake
        pipeline.state_machine.handle(Event.WAKE)

        # Publish gate decisions to verify bus events
        bus_events: list[dict] = []
        bus.subscribe("gate.decision", lambda t, p: bus_events.append(p))
        emotion_events: list[dict] = []
        bus.subscribe("llm.emotion", lambda t, p: emotion_events.append(p))

        result = await pipeline.run_turn(b"\x00" * 100)

        assert result == Route.CHAT
        assert tts.captured == ["辛苦啦，休息一下吧"]
        assert any(e.get("tag") == "sad" for e in emotion_events)
        assert any(e.get("verdict") == "pass" for e in bus_events)

    async def test_gate_drop_returns_none(self, tmp_path) -> None:
        """Gate fail-closed: run_turn returns None, no ASR/TTS."""
        (tmp_path / "voiceprints.json").write_text('{"master": 0.30}', encoding="utf-8")
        (tmp_path / "transcripts.json").write_text('["不该被转写"]', encoding="utf-8")

        bus = InProcBus()
        tts = CaptureTts()
        pipeline = ConversationPipeline(
            source=None,
            verifier=FixtureVerifier(tmp_path),
            asr=FixtureAsr(tmp_path),
            llm=ScriptedLlm(tmp_path),
            tts=tts,
            bus=bus,
            profiles=PROFILES,
        )

        result = await pipeline.run_turn(b"\x00" * 100)
        assert result is None
        assert tts.captured == []  # nothing spoken

    async def test_session_end(self, tmp_path) -> None:
        pipeline, tts, bus = make_pipeline(
            tmp_path, transcripts=["拜拜"], replies=["[emotion:neutral] 好的"]
        )
        pipeline.state_machine.handle(Event.WAKE)
        result = await pipeline.run_turn(b"\x00" * 100)
        assert result == Route.SESSION_END
        assert pipeline.state_machine.state == SessionState.STANDBY


class TestSimpleFace:
    def test_renders_256x32_frame(self) -> None:
        face = SimpleFace()
        frame = face.render()
        assert frame.width == 256
        assert frame.height == 32
        assert len(frame.pixels) == 256 * 32 * 3

    def test_standby_closes_eyes(self) -> None:
        face = SimpleFace()
        face.update(session_state="standby")
        assert face.params.eye_open < 0.1

    def test_listening_opens_eyes(self) -> None:
        face = SimpleFace()
        face.update(session_state="listening")
        assert face.params.eye_open == 1.0
        assert face.params.is_listening

    def test_amplitude_drives_mouth(self) -> None:
        face = SimpleFace()
        face.update(tts_amplitude=0.8)
        assert face.params.mouth_open == 0.8


class TestRenderPipeline:
    def test_renders_and_pushes_to_sinks(self) -> None:
        rp = RenderPipeline()
        frames: list[Frame] = []
        rp.add_sink(frames.append)

        rp.update(session_state="listening")
        frame = rp.render_frame()

        assert len(frames) == 1
        assert frame is frames[0]
        assert rp.frame_count == 1

    def test_sink_error_does_not_crash(self) -> None:
        rp = RenderPipeline()
        good_frames: list[Frame] = []

        def bad_sink(f: Frame) -> None:
            raise RuntimeError("sink bug")

        rp.add_sink(bad_sink)
        rp.add_sink(good_frames.append)

        rp.render_frame()
        assert len(good_frames) == 1  # good sink still ran


class TestScenarioReplay:
    """End-to-end scenario: chat_tired (design doc §3.6)."""

    async def test_chat_tired_scenario(self, tmp_path) -> None:
        """The canonical scenario from design doc §3.6:
        '今天好累啊' -> gate pass -> ASR -> chat -> LLM -> emotion:sad -> TTS."""
        pipeline, tts, bus = make_pipeline(
            tmp_path,
            transcripts=["今天好累啊"],
            replies=["[emotion:sad] 辛苦啦"],
        )

        # Track all bus events for assertion
        all_events: list[tuple[str, dict]] = []
        bus.subscribe("*", lambda t, p: all_events.append((t, dict(p))))

        # Simulate hotkey wake
        pipeline.state_machine.handle(Event.WAKE)
        assert pipeline.state_machine.state == SessionState.LISTENING

        # Run the turn
        result = await pipeline.run_turn(b"\x00" * 100)

        # Assertions from design doc §3.6 test spec:
        # 1. Route is CHAT
        assert result == Route.CHAT

        # 2. CaptureTts received the response text
        assert any("辛苦" in t for t in tts.captured)

        # 3. Bus events include gate.decision(pass, master) and llm.emotion(sad)
        topics = [t for t, _ in all_events]
        assert "gate.decision" in topics
        assert "llm.emotion" in topics

        gate_events = [p for t, p in all_events if t == "gate.decision"]
        assert any(
            e.get("verdict") == "pass" and e.get("speaker_id") == "master"
            for e in gate_events
        )

        emotion_events = [p for t, p in all_events if t == "llm.emotion"]
        assert any(e.get("tag") == "sad" for e in emotion_events)

        # 4. State machine transitions happened
        assert pipeline.state_machine.state in (SessionState.LISTENING, SessionState.SPEAKING)

    async def test_render_pipeline_integrated(self, tmp_path) -> None:
        """Render pipeline responds to bus signals from the conversation."""
        pipeline, tts, bus = make_pipeline(
            tmp_path,
            transcripts=["你好"],
            replies=["[emotion:happy] 哈哈你好呀"],
        )

        rp = RenderPipeline()
        frames: list[Frame] = []
        rp.add_sink(frames.append)

        # Wire bus -> render pipeline
        def on_state(topic: str, payload: dict) -> None:
            rp.update(session_state=payload.get("state", ""))

        bus.subscribe("session.state", on_state)

        # Simulate wake
        pipeline.state_machine.handle(Event.WAKE)
        rp.update(session_state="listening")

        # Run turn
        await pipeline.run_turn(b"\x00" * 100)

        # Render some frames
        for _ in range(5):
            rp.render_frame()

        assert len(frames) == 5
        assert all(f.width == 256 and f.height == 32 for f in frames)
