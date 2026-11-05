"""Integration tests (W-series): module wiring, the seams CI missed before.

These tests exist because a 2026-10-16 audit found three wiring gaps that
unit tests could never catch (each module tested alone, never together):
  1. pipeline never called bridge.dispatch (TASK_DIRECT just returned)
  2. task_ctx was hardcoded active=False (AMEND/CANCEL unreachable in real flow)
  3. Bridge.amend lost the original task (cancel cleared it before use)

Scenarios here drive REAL pipeline + REAL bridge together (only the HAL
engines stay scripted): fixture audio in -> gate -> ASR -> route -> bridge
submit/amend/cancel/flush -> bus events out.
"""

import asyncio
import json
from pathlib import Path

from bridge.server import Bridge
from core.bus import topics
from core.bus.hub import InProcBus
from core.gate.policy import GateConfig, SpeakerProfile
from core.interfaces import Route
from core.pipeline import ConversationPipeline
from core.session.state_machine import Event
from core.sim import CaptureTts, FixtureAsr, ScriptedLlm, ScriptedVerifier

PROFILES = {
    "master": SpeakerProfile(speaker_id="master", name="主人", role="admin", priority=1),
    "wife": SpeakerProfile(speaker_id="wife", name="老婆", role="member", priority=2),
}
GATE_CFG = GateConfig(t_pass=0.62, t_gray=0.55)


class BoomThenRecoverAsr:
    """ASR stub that explodes once, then recovers (degradation test)."""

    def __init__(self, recover_text: str = "今天天气怎么样") -> None:
        self._calls = 0
        self._recover = recover_text

    async def transcribe(self, pcm: bytes) -> str:
        self._calls += 1
        if self._calls == 1:
            raise RuntimeError("asr exploded")
        return self._recover


def make_stack(
    tmp_path: Path,
    score_sets: list[dict[str, float]],
    transcripts: list[str],
    replies: list[str] | None = None,
    grace: float = 999.0,
    asr=None,
    execution_delay: float = 0.01,
) -> tuple[ConversationPipeline, Bridge, CaptureTts, InProcBus, list]:
    """Wire a REAL pipeline + REAL bridge; only engines are scripted."""
    (tmp_path / "transcripts.json").write_text(json.dumps(transcripts), encoding="utf-8")
    (tmp_path / "replies.json").write_text(
        json.dumps(replies or ["[emotion:happy] 好的呀"]), encoding="utf-8"
    )

    bus = InProcBus()
    events: list[tuple[str, dict]] = []
    bus.subscribe("*", lambda t, p: events.append((t, dict(p))))

    tts = CaptureTts()
    bridge = Bridge(
        bus=bus,
        session_book_path=tmp_path / "sb.json",
        grace_seconds=grace,
        execution_delay=execution_delay,
    )
    pipeline = ConversationPipeline(
        source=None,
        verifier=ScriptedVerifier(score_sets),
        asr=asr or FixtureAsr(tmp_path),
        llm=ScriptedLlm(tmp_path),
        tts=tts,
        bus=bus,
        bridge=bridge,
        profiles=PROFILES,
        gate_config=GATE_CFG,
    )
    pipeline.state_machine.handle(Event.WAKE)
    return pipeline, bridge, tts, bus, events


def topic_events(events: list, topic: str) -> list[dict]:
    return [p for t, p in events if t == topic]


class TestFullTaskDispatch:
    """Scenario A: voice -> gate -> fast lane -> bridge submit -> confirm
    -> grace expiry -> dispatch -> result events. The whole D14 chain."""

    async def test_end_to_end_dispatch(self, tmp_path) -> None:
        pipeline, bridge, tts, bus, events = make_stack(
            tmp_path,
            score_sets=[{"master": 0.75}],
            transcripts=["派活，帮我修一下登录报错"],
            grace=0.0,
        )
        try:
            result = await pipeline.run_turn(b"\x00" * 100)
            assert result == Route.TASK_DIRECT

            # TTS confirmation was spoken (D14: 复述确认)
            assert any("收到" in t for t in tts.captured)

            # grace=0: background flush fires on next loop ticks
            await asyncio.sleep(0.05)

            # bridge actually dispatched (THE gap this test guards: pipeline
            # used to just `return Route.TASK_DIRECT` and nobody called bridge)
            progress = topic_events(events, topics.TASK_PROGRESS)
            results = topic_events(events, topics.TASK_RESULT)
            assert any(p["phase"] == "dispatched" for p in progress)
            assert any(p["phase"] == "running" for p in progress)
            assert len(results) == 1
            assert "任务完成" in results[0]["summary"]
            assert bridge.has_work() is False
        finally:
            await bridge.close()

    async def test_grace_timer_auto_dispatches(self, tmp_path) -> None:
        """Scenario G: no manual flush -- the arm() countdown dispatches."""
        pipeline, bridge, tts, bus, events = make_stack(
            tmp_path,
            score_sets=[{"master": 0.75}],
            transcripts=["派活，帮我修一下登录报错"],
            grace=0.05,
        )
        try:
            await pipeline.run_turn(b"\x00" * 100)
            assert bridge.has_work() is True  # held, not yet dispatched
            await asyncio.sleep(0.2)  # grace expires
            assert len(topic_events(events, topics.TASK_RESULT)) == 1
            assert bridge.has_work() is False
        finally:
            await bridge.close()


class TestGraceWindowAmend:
    """Scenario B: correction lands INSIDE the grace window (zero cost,
    D15's whole point): task never dispatched, amended in place, then flushed."""

    async def test_amend_before_dispatch(self, tmp_path) -> None:
        pipeline, bridge, tts, bus, events = make_stack(
            tmp_path,
            score_sets=[{"master": 0.75}, {"master": 0.75}],
            transcripts=["派活，帮我修一下登录报错", "不对，是改按钮颜色"],
            grace=999.0,  # window never expires on its own
        )
        try:
            # Turn 1: submit + confirm + arm (held)
            assert await pipeline.run_turn(b"\x00" * 100) == Route.TASK_DIRECT
            assert bridge.has_work() is True
            assert bridge.is_dispatched() is False
            assert topic_events(events, topics.TASK_RESULT) == []

            # Turn 2: correction routed as AMEND *because* the bridge
            # reports work (task_ctx flow-back -- previously hardcoded False)
            assert await pipeline.run_turn(b"\x00" * 100) == Route.TASK_AMEND

            # Original task was cancelled (audit trail) but NEVER dispatched
            cancels = topic_events(events, topics.TASK_CANCEL)
            assert len(cancels) == 1 and cancels[0]["reason"] == "amendment"
            assert topic_events(events, topics.TASK_RESULT) == []

            # Held task is the amended one
            assert bridge.active_task is not None
            assert "改按钮颜色" in bridge.active_task.prompt
            assert any("好的，按新的来" in t for t in tts.captured)

            # Manual flush dispatches the AMENDED prompt
            await bridge.flush()
            results = topic_events(events, topics.TASK_RESULT)
            assert len(results) == 1
            assert "改按钮颜色" in results[0]["summary"]
        finally:
            await bridge.close()


class TestPostDispatchAmend:
    """Scenario C: correction lands AFTER dispatch, while opencode is still
    running (the realistic window: real opencode takes minutes) -> cancel
    the running task, re-dispatch amended in the background (D15)."""

    async def test_amend_while_running(self, tmp_path) -> None:
        pipeline, bridge, tts, bus, events = make_stack(
            tmp_path,
            score_sets=[{"master": 0.75}, {"master": 0.75}],
            transcripts=["派活，帮我修一下登录报错", "不对，是改按钮颜色"],
            grace=0.0,
            execution_delay=999.0,  # freeze execution mid-run (opencode-like)
        )
        try:
            await pipeline.run_turn(b"\x00" * 100)
            await asyncio.sleep(0.05)  # grace=0 -> dispatched, still running
            assert bridge.is_dispatched() is True
            assert bridge.has_work() is True  # not completed yet
            assert len(topic_events(events, topics.TASK_PROGRESS)) == 2

            # Amend a RUNNING task
            assert await pipeline.run_turn(b"\x00" * 100) == Route.TASK_AMEND
            await asyncio.sleep(0.05)  # let the background re-dispatch run

            cancels = topic_events(events, topics.TASK_CANCEL)
            assert len(cancels) == 1 and cancels[0]["reason"] == "amendment"

            # amended task re-dispatched in the background
            progress = topic_events(events, topics.TASK_PROGRESS)
            assert len(progress) == 4  # original dispatched+running, amended dispatched+running
            assert bridge.active_task is not None
            assert "改按钮颜色" in bridge.active_task.prompt
            # neither execution has completed (frozen)
            assert topic_events(events, topics.TASK_RESULT) == []
        finally:
            await bridge.close()


class TestTaskCancel:
    """Scenario D: verbal cancel during the grace window."""

    async def test_cancel_held_task(self, tmp_path) -> None:
        pipeline, bridge, tts, bus, events = make_stack(
            tmp_path,
            score_sets=[{"master": 0.75}, {"master": 0.75}],
            transcripts=["派活，帮我修一下登录报错", "算了别弄了"],
            grace=999.0,
        )
        try:
            assert await pipeline.run_turn(b"\x00" * 100) == Route.TASK_DIRECT
            assert await pipeline.run_turn(b"\x00" * 100) == Route.TASK_CANCEL

            cancels = topic_events(events, topics.TASK_CANCEL)
            assert len(cancels) == 1 and cancels[0]["reason"] == "user_request"
            assert bridge.has_work() is False
            assert topic_events(events, topics.TASK_RESULT) == []  # never ran
            assert any("取消" in t for t in tts.captured)
        finally:
            await bridge.close()


class TestDegradation:
    """Scenario E: engine exception -> rule 6 (LED error 3s + notify, no crash),
    and the pipeline keeps working on the next turn."""

    async def test_asr_exception_degrades_and_recovers(self, tmp_path) -> None:
        pipeline, bridge, tts, bus, events = make_stack(
            tmp_path,
            score_sets=[{"master": 0.75}, {"master": 0.75}],
            transcripts=["", "今天天气怎么样"],
            replies=["[emotion:happy] 挺好的呀"],
            asr=BoomThenRecoverAsr(),
        )
        # Turn 1: ASR explodes -> degraded, not raised
        result = await pipeline.run_turn(b"\x00" * 100)
        assert result is None

        renders = topic_events(events, topics.RENDER_COMMAND)
        notifies = topic_events(events, topics.SYSTEM_NOTIFY)
        assert any(r["cmd"] == "error" and r["duration_ms"] == 3000 for r in renders)
        assert any(n["level"] == "error" for n in notifies)

        # Turn 2: pipeline survived, normal chat works
        result = await pipeline.run_turn(b"\x00" * 100)
        assert result == Route.CHAT
        assert tts.captured[-1] == "挺好的呀"


class TestPermissionWiring:
    """Scenario F: member task request is refused by the pipeline AND the
    bridge receives nothing (permission enforced at both points, D12)."""

    async def test_member_denied_bridge_untouched(self, tmp_path) -> None:
        pipeline, bridge, tts, bus, events = make_stack(
            tmp_path,
            score_sets=[{"wife": 0.80}],
            transcripts=["派活，帮我修一下报错"],
        )
        result = await pipeline.run_turn(b"\x00" * 100)

        assert result == Route.TASK_DENIED
        assert bridge.has_work() is False
        assert topic_events(events, topics.TASK_PROGRESS) == []
        assert tts.captured == ["这个要主人来才行哦"]
