"""Session state machine: the conversation lifecycle controller.

Implements design doc §4.1 (v2.9, D22: sleep-default + hotkey wake).

States:
  STANDBY     -> closed eyes, zero transcription, waiting for hotkey (v1) or wake word (v1.1)
  LISTENING   -> radar eyes, VAD active, gate -> ASR -> router pipeline live
  THINKING    -> spinning pupils, waiting for LLM first token
  SPEAKING    -> mouth animated by TTS amplitude
  TASK_RUNNING-> work layout, task progress on LED status area

Transitions (see design doc §4.1 mermaid):
  STANDBY -> LISTENING     on wake (hotkey / wake word)
  LISTENING -> THINKING    on chat intent (gate passed)
  LISTENING -> TASK_RUNNING on task intent (gate passed, admin)
  THINKING -> SPEAKING     on first LLM token + TTS start
  SPEAKING -> LISTENING    on playback done / interrupted
  TASK_RUNNING -> SPEAKING on task complete (summary playback)
  TASK_RUNNING -> LISTENING on task to background
  LISTENING -> STANDBY     on timeout / verbal end / semantic end
  SPEAKING -> STANDBY      on timeout (after playback)

Task completion auto-wake: STANDBY -> SPEAKING (brief) -> STANDBY after 8s idle.

This module is a pure state machine with zero I/O. Event emission is
delegated to a callback so it can be wired to the bus or captured in tests.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import Enum, auto
from typing import Any

from core.interfaces import SessionState


class WakeSource(Enum):
    """How the session was woken up."""

    HOTKEY = auto()
    WAKE_WORD = auto()  # v1.1
    TASK_COMPLETE = auto()  # auto-wake for result playback


class EndReason(Enum):
    """Why the session ended."""

    TIMEOUT = auto()  # 8s no speech
    VERBAL = auto()  # "拜拜" / "好了"
    SEMANTIC = auto()  # LLM [end] flag
    FORCED = auto()  # mode switch (game), manual, etc.


class Event(Enum):
    """Inputs that drive state transitions."""

    WAKE = auto()  # hotkey or wake word
    GATE_PASS_CHAT = auto()  # gate passed, router says chat
    GATE_PASS_TASK = auto()  # gate passed, router says task (admin)
    GATE_FAIL = auto()  # gate dropped segment (stay in LISTENING, silent)
    MEMBER_DENIED = auto()  # member coding request -> spoken refusal
    LLM_FIRST_TOKEN = auto()  # first LLM token arrived, TTS starting
    PLAYBACK_DONE = auto()  # TTS finished playing
    INTERRUPTED = auto()  # user spoke during SPEAKING (barge-in)
    TASK_COMPLETE = auto()  # opencode finished, summary ready to play
    TASK_BACKGROUND = auto()  # task running, back to listening
    TIMEOUT = auto()  # no speech for timeout_sec
    VERBAL_END = auto()  # "拜拜" / "好了"
    SEMANTIC_END = auto()  # LLM says conversation over
    FORCED_END = auto()  # external force-close (game mode, manual, etc.)


# Type for the event-emission callback (wired to bus.publish in production)
EventCallback = Callable[[SessionState, SessionState, dict[str, Any]], None]


class SessionStateMachine:
    """Pure state machine for the conversation lifecycle.

    The caller feeds events via `handle(event, **context)` and reads the
    current state via `state`. State changes are reported through the
    optional `on_transition` callback (old_state, new_state, context).
    """

    # Valid transitions: (current_state, event) -> new_state
    # States not listed for an event means the event is ignored in that state.
    TRANSITIONS: dict[tuple[SessionState, Event], SessionState] = {
        # Wake
        (SessionState.STANDBY, Event.WAKE): SessionState.LISTENING,
        # Chat flow
        (SessionState.LISTENING, Event.GATE_PASS_CHAT): SessionState.THINKING,
        (SessionState.LISTENING, Event.GATE_PASS_TASK): SessionState.TASK_RUNNING,
        (SessionState.LISTENING, Event.MEMBER_DENIED): SessionState.SPEAKING,
        # LLM -> speech
        (SessionState.THINKING, Event.LLM_FIRST_TOKEN): SessionState.SPEAKING,
        # Speech cycle
        (SessionState.SPEAKING, Event.PLAYBACK_DONE): SessionState.LISTENING,
        (SessionState.SPEAKING, Event.INTERRUPTED): SessionState.LISTENING,
        # Task lifecycle
        (SessionState.TASK_RUNNING, Event.TASK_COMPLETE): SessionState.SPEAKING,
        (SessionState.TASK_RUNNING, Event.TASK_BACKGROUND): SessionState.LISTENING,
        # Session end
        (SessionState.LISTENING, Event.TIMEOUT): SessionState.STANDBY,
        (SessionState.LISTENING, Event.VERBAL_END): SessionState.STANDBY,
        (SessionState.LISTENING, Event.SEMANTIC_END): SessionState.STANDBY,
        (SessionState.SPEAKING, Event.TIMEOUT): SessionState.STANDBY,
        (SessionState.SPEAKING, Event.VERBAL_END): SessionState.STANDBY,
        (SessionState.SPEAKING, Event.SEMANTIC_END): SessionState.STANDBY,
        # Forced end from any active state
        (SessionState.LISTENING, Event.FORCED_END): SessionState.STANDBY,
        (SessionState.THINKING, Event.FORCED_END): SessionState.STANDBY,
        (SessionState.SPEAKING, Event.FORCED_END): SessionState.STANDBY,
        (SessionState.TASK_RUNNING, Event.FORCED_END): SessionState.STANDBY,
        # Task auto-wake from standby (brief playback then back to sleep)
        (SessionState.STANDBY, Event.TASK_COMPLETE): SessionState.SPEAKING,
    }

    def __init__(
        self,
        timeout_sec: float = 8.0,
        on_transition: EventCallback | None = None,
    ) -> None:
        self._state = SessionState.STANDBY
        self._timeout_sec = timeout_sec
        self._on_transition = on_transition
        self._extra_callbacks: list[EventCallback] = []
        self._last_activity: float = time.monotonic()

    def add_transition_callback(self, callback: EventCallback) -> None:
        """Register an additional transition callback (W.6: bus publishing).

        Multiple callbacks are supported so the owner (constructor arg) and
        the pipeline (bus wiring) can both observe transitions.
        """
        self._extra_callbacks.append(callback)

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def timeout_sec(self) -> float:
        return self._timeout_sec

    def touch(self) -> None:
        """Record activity (called on every valid speech segment or interaction)."""
        self._last_activity = time.monotonic()

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_activity

    def is_timed_out(self) -> bool:
        """Check if the current state has exceeded the idle timeout."""
        if self._state in (SessionState.STANDBY, SessionState.TASK_RUNNING):
            return False  # standby and background tasks don't time out
        return self.idle_seconds >= self._timeout_sec

    def handle(self, event: Event, **context: Any) -> SessionState:
        """Process an event and return the (possibly unchanged) state.

        Invalid transitions are silently ignored (the machine stays put).
        Valid transitions fire the on_transition callback.
        """
        key = (self._state, event)
        new_state = self.TRANSITIONS.get(key)

        if new_state is None:
            return self._state  # no-op: event not valid in current state

        old_state = self._state
        self._state = new_state
        self.touch()

        if self._on_transition:
            try:
                self._on_transition(old_state, new_state, context)
            except Exception:
                pass  # callback errors must not crash the state machine

        for cb in self._extra_callbacks:
            try:
                cb(old_state, new_state, context)
            except Exception:
                pass  # same rule: observers must not break the machine

        return self._state

    def force_standby(self) -> SessionState:
        """Force-close from any state (game mode entry, manual override)."""
        return self.handle(Event.FORCED_END, reason="forced")

    def check_timeout(self) -> SessionState:
        """Convenience: fire TIMEOUT event if idle too long."""
        if self.is_timed_out():
            return self.handle(Event.TIMEOUT)
        return self._state
