"""Tests for the session state machine (design doc §4.1, D22)."""

import time

from core.interfaces import SessionState
from core.session.state_machine import EndReason, Event, SessionStateMachine, WakeSource


class TestInitialState:
    def test_starts_in_standby(self) -> None:
        sm = SessionStateMachine()
        assert sm.state == SessionState.STANDBY

    def test_default_timeout_is_8s(self) -> None:
        sm = SessionStateMachine()
        assert sm.timeout_sec == 8.0


class TestWakeAndEnd:
    def test_hotkey_wake(self) -> None:
        sm = SessionStateMachine()
        sm.handle(Event.WAKE, source=WakeSource.HOTKEY)
        assert sm.state == SessionState.LISTENING

    def test_timeout_ends_session(self) -> None:
        sm = SessionStateMachine(timeout_sec=0.01)
        sm.handle(Event.WAKE)
        assert sm.state == SessionState.LISTENING
        time.sleep(0.02)
        assert sm.check_timeout() == SessionState.STANDBY

    def test_verbal_end(self) -> None:
        sm = SessionStateMachine()
        sm.handle(Event.WAKE)
        sm.handle(Event.VERBAL_END, reason=EndReason.VERBAL)
        assert sm.state == SessionState.STANDBY

    def test_semantic_end(self) -> None:
        sm = SessionStateMachine()
        sm.handle(Event.WAKE)
        sm.handle(Event.SEMANTIC_END, reason=EndReason.SEMANTIC)
        assert sm.state == SessionState.STANDBY

    def test_forced_end_from_listening(self) -> None:
        sm = SessionStateMachine()
        sm.handle(Event.WAKE)
        sm.force_standby()
        assert sm.state == SessionState.STANDBY

    def test_wake_in_listening_is_noop(self) -> None:
        sm = SessionStateMachine()
        sm.handle(Event.WAKE)
        sm.handle(Event.WAKE)  # already awake, should stay
        assert sm.state == SessionState.LISTENING


class TestChatFlow:
    def test_full_chat_cycle(self) -> None:
        """WAKE -> LISTENING -> THINKING -> SPEAKING -> LISTENING -> STANDBY."""
        sm = SessionStateMachine()
        sm.handle(Event.WAKE)
        sm.handle(Event.GATE_PASS_CHAT, speaker="master", route="chat")
        assert sm.state == SessionState.THINKING
        sm.handle(Event.LLM_FIRST_TOKEN)
        assert sm.state == SessionState.SPEAKING
        sm.handle(Event.PLAYBACK_DONE)
        assert sm.state == SessionState.LISTENING
        sm.handle(Event.VERBAL_END)
        assert sm.state == SessionState.STANDBY

    def test_barge_in_during_speaking(self) -> None:
        sm = SessionStateMachine()
        sm.handle(Event.WAKE)
        sm.handle(Event.GATE_PASS_CHAT)
        sm.handle(Event.LLM_FIRST_TOKEN)
        sm.handle(Event.INTERRUPTED)  # user spoke during playback
        assert sm.state == SessionState.LISTENING

    def test_gate_fail_stays_listening(self) -> None:
        """Dropped segment: state unchanged, silent."""
        sm = SessionStateMachine()
        sm.handle(Event.WAKE)
        sm.handle(Event.GATE_FAIL, verdict="drop")
        assert sm.state == SessionState.LISTENING  # no change

    def test_member_denied_goes_to_speaking(self) -> None:
        """Member coding request: spoken refusal via SPEAKING (not a state)."""
        sm = SessionStateMachine()
        sm.handle(Event.WAKE)
        sm.handle(Event.MEMBER_DENIED, speaker="wife", route="task_denied")
        assert sm.state == SessionState.SPEAKING


class TestTaskFlow:
    def test_task_direct(self) -> None:
        sm = SessionStateMachine()
        sm.handle(Event.WAKE)
        sm.handle(Event.GATE_PASS_TASK, speaker="master", route="task_direct")
        assert sm.state == SessionState.TASK_RUNNING

    def test_task_complete_plays_summary(self) -> None:
        sm = SessionStateMachine()
        sm.handle(Event.WAKE)
        sm.handle(Event.GATE_PASS_TASK)
        sm.handle(Event.TASK_COMPLETE, task_id="t-1", summary="测试 3 过 1 挂")
        assert sm.state == SessionState.SPEAKING
        sm.handle(Event.PLAYBACK_DONE)
        assert sm.state == SessionState.LISTENING

    def test_task_to_background(self) -> None:
        sm = SessionStateMachine()
        sm.handle(Event.WAKE)
        sm.handle(Event.GATE_PASS_TASK)
        sm.handle(Event.TASK_BACKGROUND, task_id="t-1")
        assert sm.state == SessionState.LISTENING
        # Session can still end while task runs in background
        sm.handle(Event.VERBAL_END)
        assert sm.state == SessionState.STANDBY

    def test_task_complete_auto_wake_from_standby(self) -> None:
        """D16/D22: task completes while sleeping -> brief wake to play result."""
        sm = SessionStateMachine()
        sm.handle(Event.WAKE)
        sm.handle(Event.GATE_PASS_TASK)
        sm.handle(Event.TASK_BACKGROUND)  # task in background, back to listening
        sm.handle(Event.VERBAL_END)  # session ends, task still running
        assert sm.state == SessionState.STANDBY
        # Task finishes while we're asleep
        sm.handle(Event.TASK_COMPLETE, task_id="t-1", summary="done")
        assert sm.state == SessionState.SPEAKING  # auto-wake to play
        sm.handle(Event.PLAYBACK_DONE)
        assert sm.state == SessionState.LISTENING  # then back to listening
        # And eventually times out back to standby
        sm.handle(Event.TIMEOUT)
        assert sm.state == SessionState.STANDBY


class TestInvalidTransitions:
    def test_llm_first_token_in_standby_is_noop(self) -> None:
        sm = SessionStateMachine()
        sm.handle(Event.LLM_FIRST_TOKEN)
        assert sm.state == SessionState.STANDBY

    def test_gate_pass_in_thinking_is_noop(self) -> None:
        sm = SessionStateMachine()
        sm.handle(Event.WAKE)
        sm.handle(Event.GATE_PASS_CHAT)
        sm.handle(Event.GATE_PASS_CHAT)  # already thinking, ignore
        assert sm.state == SessionState.THINKING

    def test_playback_done_in_standby_is_noop(self) -> None:
        sm = SessionStateMachine()
        sm.handle(Event.PLAYBACK_DONE)
        assert sm.state == SessionState.STANDBY

    def test_timeout_in_standby_is_noop(self) -> None:
        sm = SessionStateMachine(timeout_sec=0.001)
        time.sleep(0.002)
        sm.check_timeout()
        assert sm.state == SessionState.STANDBY  # standby doesn't time out


class TestTransitionCallback:
    def test_callback_receives_old_and_new_state(self) -> None:
        transitions: list[tuple[SessionState, SessionState]] = []

        def on_transition(old: SessionState, new: SessionState, ctx: dict) -> None:
            transitions.append((old, new))

        sm = SessionStateMachine(on_transition=on_transition)
        sm.handle(Event.WAKE)
        sm.handle(Event.GATE_PASS_CHAT)

        assert transitions == [
            (SessionState.STANDBY, SessionState.LISTENING),
            (SessionState.LISTENING, SessionState.THINKING),
        ]

    def test_callback_error_does_not_crash(self) -> None:
        def bad_callback(old: SessionState, new: SessionState, ctx: dict) -> None:
            raise RuntimeError("callback bug")

        sm = SessionStateMachine(on_transition=bad_callback)
        sm.handle(Event.WAKE)  # should not raise
        assert sm.state == SessionState.LISTENING

    def test_noop_events_do_not_fire_callback(self) -> None:
        fired: list[tuple[SessionState, SessionState]] = []

        def on_transition(old: SessionState, new: SessionState, ctx: dict) -> None:
            fired.append((old, new))

        sm = SessionStateMachine(on_transition=on_transition)
        sm.handle(Event.GATE_PASS_CHAT)  # invalid from STANDBY
        assert fired == []  # no transition, no callback


class TestFullScenario:
    def test_morning_routine(self) -> None:
        """Complete morning: wake, chat twice, dispatch task, task auto-wakes."""
        sm = SessionStateMachine()
        assert sm.state == SessionState.STANDBY

        # Wake up
        sm.handle(Event.WAKE, source=WakeSource.HOTKEY)
        assert sm.state == SessionState.LISTENING

        # First chat turn
        sm.handle(Event.GATE_PASS_CHAT, text="今天好累啊")
        sm.handle(Event.LLM_FIRST_TOKEN)
        sm.handle(Event.PLAYBACK_DONE)
        assert sm.state == SessionState.LISTENING

        # Second chat turn (barge-in test)
        sm.handle(Event.GATE_PASS_CHAT, text="帮我修一下登录")
        sm.handle(Event.LLM_FIRST_TOKEN)
        sm.handle(Event.INTERRUPTED)  # user interrupts
        assert sm.state == SessionState.LISTENING

        # Dispatch a task
        sm.handle(Event.GATE_PASS_TASK, text="派活，跑测试")
        assert sm.state == SessionState.TASK_RUNNING
        sm.handle(Event.TASK_BACKGROUND)  # go back to listening
        assert sm.state == SessionState.LISTENING

        # Session ends, task still running
        sm.handle(Event.VERBAL_END)
        assert sm.state == SessionState.STANDBY

        # Task completes, auto-wake to play result
        sm.handle(Event.TASK_COMPLETE, task_id="t-1", summary="测试 3 过 1 挂")
        assert sm.state == SessionState.SPEAKING
        sm.handle(Event.PLAYBACK_DONE)
        sm.handle(Event.TIMEOUT)
        assert sm.state == SessionState.STANDBY  # back to sleep
