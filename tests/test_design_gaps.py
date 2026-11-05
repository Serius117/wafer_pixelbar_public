"""TEST_DESIGN.md gap-closure tests (2026-10-16 full regression + round 2).

Round 1: SESS-13/16/19/20, PIPE-20/21, HUB-12.
Round 2: SESS-23 (state-machine exhaustive matrix), HUB-09 (amplitude
throttle), HUB-11 (slow-subscriber semantics locked), ASR-04 (empty pcm),
ASR-06 / CC-PROC-03 (dead engine endpoints degrade via rule 6),
CC-AUD-01 (zero-byte segment), CC-LLM-02 (illegal emotion tag),
CC-TXT-04 (long instruction confirmation truncation), REND-04/05 (blink
and breathing state), BRIDGE-26 (single-task policy).
"""

from __future__ import annotations

import asyncio
import time

from core.bus import topics
from core.bus.hub import HubServer, InProcBus
from core.bus.hub_client import HubClient
from core.interfaces import AudioSegment, Route, SessionState
from core.pipeline import ConversationPipeline
from core.session.state_machine import Event, SessionStateMachine
from core.sim import CaptureTts, FixtureAsr, ScriptedLlm, ScriptedVerifier


class TestSess13StandbyTaskCompleteAutoWake:
    """SESS-13: task completes while asleep -> brief SPEAKING (summary)."""

    def test_standby_task_complete(self) -> None:
        sm = SessionStateMachine()
        assert sm.state == SessionState.STANDBY
        sm.handle(Event.TASK_COMPLETE, summary="任务完成：修好了")
        assert sm.state == SessionState.SPEAKING


class TestSess16NoTimeoutInStableStates:
    """SESS-16: STANDBY / TASK_RUNNING never time out (is_timed_out False)."""

    def test_standby_never_times_out(self) -> None:
        sm = SessionStateMachine(timeout_sec=0.01)
        time.sleep(0.05)
        assert sm.is_timed_out() is False

    def test_task_running_never_times_out(self) -> None:
        sm = SessionStateMachine(timeout_sec=0.01)
        sm.handle(Event.WAKE)
        sm.handle(Event.GATE_PASS_TASK)
        assert sm.state == SessionState.TASK_RUNNING
        time.sleep(0.05)
        assert sm.is_timed_out() is False

    def test_listening_does_time_out(self) -> None:
        sm = SessionStateMachine(timeout_sec=0.01)
        sm.handle(Event.WAKE)
        time.sleep(0.05)
        assert sm.is_timed_out() is True


class TestSess19CallbackExceptionIsolation:
    """SESS-19: a crashing observer must not break the state machine."""

    def test_bad_callback_does_not_block_transition(self) -> None:
        sm = SessionStateMachine()

        def bad_cb(old, new, ctx):
            raise RuntimeError("observer bug")

        sm.add_transition_callback(bad_cb)
        final = sm.handle(Event.WAKE)
        assert final == SessionState.LISTENING  # transition still happened


class TestSess20MultipleCallbacksInOrder:
    """SESS-20: owner callback + added callbacks all fire, in order."""

    def test_order(self) -> None:
        calls: list[str] = []

        def owner(old, new, ctx):
            calls.append("owner")

        def second(old, new, ctx):
            calls.append("second")

        sm = SessionStateMachine(on_transition=owner)
        sm.add_transition_callback(second)
        sm.handle(Event.WAKE)
        assert calls == ["owner", "second"]


class TestPipe20EmptyAsrTranscript:
    """PIPE-20: ASR yields "" -> routed as CHAT, no crash, LLM still called."""

    async def test_empty_transcript_chat(self, tmp_path) -> None:
        (tmp_path / "transcripts.json").write_text('[""]', encoding="utf-8")
        (tmp_path / "replies.json").write_text(
            '["[emotion:happy] 我在呢"]', encoding="utf-8"
        )
        bus = InProcBus()
        tts = CaptureTts()
        pipeline = ConversationPipeline(
            source=None,
            verifier=ScriptedVerifier([{"master": 0.75}]),
            asr=FixtureAsr(tmp_path),
            llm=ScriptedLlm(tmp_path),
            tts=tts,
            bus=bus,
        )
        result = await pipeline.run_turn(b"\x00" * 100)
        assert result == Route.CHAT
        assert tts.captured == ["我在呢"]


class TestPipe21RunLoop:
    """PIPE-21: run() consumes every source segment then exits cleanly."""

    async def test_run_consumes_all(self, tmp_path) -> None:
        import json

        class ListSource:
            def __init__(self, n: int) -> None:
                self._segments = [
                    AudioSegment(pcm=b"\x00" * 10, start_ts=0.0 + i, end_ts=0.5 + i)
                    for i in range(n)
                ]

            async def segments(self):
                for seg in self._segments:
                    yield seg

        (tmp_path / "transcripts.json").write_text(
            json.dumps(["今天好累啊"] * 3), encoding="utf-8"
        )
        (tmp_path / "replies.json").write_text(
            json.dumps(["[emotion:happy] 好的"] * 3), encoding="utf-8"
        )
        bus = InProcBus()
        tts = CaptureTts()
        pipeline = ConversationPipeline(
            source=ListSource(3),
            verifier=ScriptedVerifier([{"master": 0.75}] * 3),
            asr=FixtureAsr(tmp_path),
            llm=ScriptedLlm(tmp_path),
            tts=tts,
            bus=bus,
        )
        await asyncio.wait_for(pipeline.run(), timeout=5.0)
        assert len(tts.captured) == 3  # every segment became a turn


class TestHub12NonSerializablePayload:
    """HUB-12: bad payload on HubClient degrades to a log, never raises."""

    async def test_publish_bad_payload_does_not_raise(self) -> None:
        server = HubServer("127.0.0.1", 0)
        await server.start()
        try:
            client = HubClient("127.0.0.1", server.port)
            await client.connect()

            class Unserializable:
                pass

            # must not raise despite the set() payload
            await client.publish(topics.BENCH_TICK, {"weird": {Unserializable()}})

            # client is still usable afterwards
            received: list[dict] = []
            client.subscribe(topics.SESSION_STATE, lambda t, p: received.append(p))
            await client.publish(topics.SESSION_STATE, {"state": "listening"})
            deadline = time.monotonic() + 2.0
            while not received and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert received == [{"state": "listening"}]
            await client.close()
        finally:
            await server.close()


# --- round 2 (P0/P1 gap closure, 2026-10-16 evening) -------------------------


class TestSess23ExhaustiveMatrix:
    """SESS-23: every (state, event) not in TRANSITIONS is a silent no-op."""

    ALL_STATES = list(SessionState)
    ALL_EVENTS = list(Event)

    def test_invalid_combinations_are_noops(self) -> None:
        valid = set(SessionStateMachine.TRANSITIONS.keys())
        checked = 0
        for state in self.ALL_STATES:
            for event in self.ALL_EVENTS:
                if (state, event) in valid:
                    continue
                sm = SessionStateMachine()
                # force into the target state via a legal path
                sm._state = state  # noqa: SLF001 (matrix rig)
                result = sm.handle(event)
                assert result == state, f"{state} + {event} must be a no-op"
                checked += 1
        assert checked > 40  # sanity: the matrix really was exhaustive


class TestHub09AmplitudeThrottle:
    async def test_50hz_input_throttled_to_30hz(self) -> None:
        """HUB-09: rms events above 30Hz are dropped at the render consumer."""
        from render.claw_face import ClawFace
        from render.event_bridge import RenderEventBridge

        bus = InProcBus()
        face = ClawFace()
        bridge = RenderEventBridge(bus, face)
        await bridge.start()

        # simulate 50Hz for 0.3s = 15 events; ~30Hz cap -> <= 10 updates
        for i in range(15):
            await bus.publish(topics.TTS_AMPLITUDE, {"rms": (i % 10) / 10})
            await asyncio.sleep(0.02)  # 50Hz cadence

        assert bridge._amp_updates <= 10  # noqa: SLF001 (observability)
        assert bridge._amp_updates >= 4  # not over-throttled to death


class TestHub11SlowSubscriberSemantics:
    async def test_slow_subscriber_orders_over_latency(self) -> None:
        """HUB-11 (locked design trade-off): a slow async subscriber delays
        the publisher (sequential await = guaranteed ordering). Slow I/O
        belongs to engine timeouts, not the bus; locked as current behaviour."""
        bus = InProcBus()
        received: list[str] = []

        async def slow(topic, payload):
            # 60ms x2 keeps the >=0.1s blocking assertion robust against
            # Windows timer jitter (observed 94ms with 50ms sleeps, 2026-10-28).
            await asyncio.sleep(0.06)
            received.append(payload["i"])

        bus.subscribe(topics.BENCH_TICK, slow)
        t0 = time.monotonic()
        await bus.publish(topics.BENCH_TICK, {"i": "a"})
        await bus.publish(topics.BENCH_TICK, {"i": "b"})
        elapsed = time.monotonic() - t0

        assert received == ["a", "b"]  # strict ordering
        assert elapsed >= 0.1  # and yes, it blocked (documented trade-off)


class TestAsr04EmptySegment:
    def test_empty_pcm_short_circuits_before_model_load(self) -> None:
        """ASR-04: zero-byte PCM returns "" without loading whisper."""
        from engines.asr import FasterWhisperAsr

        asr = FasterWhisperAsr()
        assert asr._transcribe_sync(b"") == ""  # noqa: SLF001
        assert asr._transcribe_sync(b"\x00") == ""  # noqa: SLF001 (odd byte)
        assert asr._model is None  # model never loaded


class TestDeadEngineEndpoints:
    """ASR-06 / CC-PROC-03: unreachable engines degrade via rule 6."""

    async def test_dead_asr_server_degrades(self, tmp_path) -> None:
        from engines.asr_qwen import QwenLlamaServerAsr

        bus = InProcBus()
        renders: list[dict] = []
        bus.subscribe(topics.RENDER_COMMAND, lambda t, p: renders.append(p))
        pipeline = ConversationPipeline(
            source=None,
            verifier=ScriptedVerifier([{"master": 0.75}]),
            asr=QwenLlamaServerAsr(url="http://127.0.0.1:9", timeout=0.5),
            llm=ScriptedLlm(tmp_path),
            tts=CaptureTts(),
            bus=bus,
            asr_timeout=5.0,
        )
        (tmp_path / "replies.json").write_text('["ok"]', encoding="utf-8")
        result = await pipeline.run_turn(b"\x00" * 100)
        assert result is None
        assert any(r["cmd"] == "error" for r in renders)

    async def test_dead_llm_api_degrades(self, tmp_path) -> None:
        from engines.llm_api import OpenAiCompatLlm

        (tmp_path / "transcripts.json").write_text('["今天好累啊"]', encoding="utf-8")
        bus = InProcBus()
        renders: list[dict] = []
        bus.subscribe(topics.RENDER_COMMAND, lambda t, p: renders.append(p))
        pipeline = ConversationPipeline(
            source=None,
            verifier=ScriptedVerifier([{"master": 0.75}]),
            asr=FixtureAsr(tmp_path),
            llm=OpenAiCompatLlm(base_url="http://127.0.0.1:9", api_key="k",
                                 timeout=0.5),
            tts=CaptureTts(),
            bus=bus,
        )
        result = await pipeline.run_turn(b"\x00" * 100)
        assert result is None
        assert any(r["cmd"] == "error" for r in renders)


class TestCcAud01ZeroByteSegment:
    async def test_zero_byte_pcm_full_turn(self, tmp_path) -> None:
        """CC-AUD-01: a 0-byte segment passes gate, yields empty transcript,
        routes to CHAT -- nothing crashes."""
        (tmp_path / "transcripts.json").write_text('[""]', encoding="utf-8")
        (tmp_path / "replies.json").write_text('["[emotion:happy] 嗯"]', encoding="utf-8")
        bus = InProcBus()
        tts = CaptureTts()
        pipeline = ConversationPipeline(
            source=None,
            verifier=ScriptedVerifier([{"master": 0.75}]),
            asr=FixtureAsr(tmp_path),
            llm=ScriptedLlm(tmp_path),
            tts=tts,
            bus=bus,
        )
        result = await pipeline.run_turn(b"")
        assert result == Route.CHAT

    def test_seeded_verifier_empty_pcm(self, tmp_path) -> None:
        from engines.voiceprint import EnrollmentStore, SeededVerifier

        store = EnrollmentStore(tmp_path)
        store.save("master", [1.0] + [0.0] * 191)
        verifier = SeededVerifier(store, seed=1)
        scores = verifier.scores(b"")  # must not raise
        assert "master" in scores


class TestCcLlm02IllegalEmotionTag:
    async def test_unknown_tag_falls_back_to_normal(self) -> None:
        """CC-LLM-02: [emotion:愤怒] (non-vocabulary) renders neutral."""
        from render.claw_face import ClawFace
        from render.event_bridge import RenderEventBridge

        bus = InProcBus()
        face = ClawFace()
        bridge = RenderEventBridge(bus, face)
        await bridge.start()

        await bus.publish(topics.LLM_EMOTION, {"tag": "愤怒"})
        assert face.state.expression == "normal"  # neutral fallback


class TestCcTxt04LongInstruction:
    async def test_confirmation_truncated_to_30_chars(self, tmp_path) -> None:
        """CC-TXT-04: 500-char instruction -> spoken confirm is ~30 chars."""
        long_text = "派活，" + "请把这些文件全部重构并且补齐单元测试" * 25
        (tmp_path / "transcripts.json").write_text(
            __import__("json").dumps([long_text]), encoding="utf-8"
        )
        (tmp_path / "replies.json").write_text('["unused"]', encoding="utf-8")
        bus = InProcBus()
        from bridge.server import Bridge
        from core.gate.policy import SpeakerProfile

        profiles = {
            "master": SpeakerProfile(
                speaker_id="master", name="主人", role="admin", priority=1
            ),
        }
        bridge = Bridge(bus=bus, session_book_path=tmp_path / "sb.json",
                        grace_seconds=999.0)
        notifies: list[dict] = []
        bus.subscribe(topics.SYSTEM_NOTIFY, lambda t, p: notifies.append(p))
        pipeline = ConversationPipeline(
            source=None,
            verifier=ScriptedVerifier([{"master": 0.75}]),
            asr=FixtureAsr(tmp_path),
            llm=None,
            tts=None,  # silent mode: confirmations land in notify (assertable)
            bus=bus,
            bridge=bridge,
            profiles=profiles,
        )
        result = await pipeline.run_turn(b"\x00" * 100)
        assert result == Route.TASK_DIRECT
        confirm = next(n["text"] for n in notifies if "收到" in n["text"])
        assert len(confirm) <= 45  # 30-char task slice + fixed phrasing
        await bridge.close()


class TestRend04BlinkState:
    def test_blink_lifecycle(self) -> None:
        """REND-04: scheduled blink closes and reopens the eye."""
        from render.claw_face import ClawFace

        face = ClawFace()
        face.state._next_blink = time.monotonic()  # due now  # noqa: SLF001
        face.tick()
        assert face.state._blinking is True  # noqa: SLF001
        progressed = False
        for _ in range(30):
            time.sleep(0.02)  # blink uses wall clock; give it real time
            face.tick()
            if face.state.blink_progress > 0:  # noqa: SLF001
                progressed = True
            if face.state._blinking is False:  # noqa: SLF001
                break
        assert progressed
        assert face.state._blinking is False  # noqa: SLF001
        assert face.state.blink_progress == 0.0  # noqa: SLF001


class TestRend05BreathingState:
    def test_breathing_oscillates(self) -> None:
        """REND-05: breathing phase advances and oscillates in [-0.5, 0.5]."""
        from render.claw_face import ClawFace

        face = ClawFace()
        values = []
        for _ in range(50):  # phase needs > pi to cross the negative half
            face.tick(0.1)
            values.append(face.state.breathing)
        assert any(v > 0.1 for v in values)
        assert any(v < -0.1 for v in values)
        assert all(-0.6 <= v <= 0.6 for v in values)


class TestBridge26SingleTaskPolicy:
    async def test_second_task_refused_while_first_held(self, tmp_path) -> None:
        """BRIDGE-26: a task while one is held/running is refused with a
        question -- never silently supersedes."""
        (tmp_path / "transcripts.json").write_text(
            __import__("json").dumps(["派活，修一下报错", "派活，再写个脚本"]),
            encoding="utf-8"
        )
        (tmp_path / "replies.json").write_text('["unused"]', encoding="utf-8")
        bus = InProcBus()
        from bridge.server import Bridge
        from core.gate.policy import SpeakerProfile

        profiles = {
            "master": SpeakerProfile(
                speaker_id="master", name="主人", role="admin", priority=1
            ),
        }
        bridge = Bridge(bus=bus, session_book_path=tmp_path / "sb.json",
                        grace_seconds=999.0)
        notifies: list[dict] = []
        bus.subscribe(topics.SYSTEM_NOTIFY, lambda t, p: notifies.append(p))
        events: list[tuple[str, dict]] = []
        bus.subscribe("*", lambda t, p: events.append((t, dict(p))))
        pipeline = ConversationPipeline(
            source=None,
            verifier=ScriptedVerifier([{"master": 0.75}] * 2),
            asr=FixtureAsr(tmp_path),
            llm=None,
            tts=None,
            bus=bus,
            bridge=bridge,
            profiles=profiles,
        )

        r1 = await pipeline.run_turn(b"\x00" * 100)
        assert r1 == Route.TASK_DIRECT
        first_task_id = bridge.active_task.task_id

        r2 = await pipeline.run_turn(b"\x00" * 100)
        assert r2 == Route.TASK_DIRECT  # routed fine...
        # ...but the bridge kept the FIRST task and asked instead
        assert bridge.active_task.task_id == first_task_id
        assert any("还没完成" in n["text"] for n in notifies)
        # only one task ever dispatched
        assert not any(t == topics.TASK_REQUEST for t, _ in events)
        await bridge.close()
