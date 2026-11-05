"""Wake controller: the two entries into a session (T6.4, D22).

v1 had exactly one wake entry -- the hotkey (ctl.hotkey{wake}). T6.4 adds
the second one: the wake WORD (sherpa-onnx KWS). Both funnel into the
same SessionStateMachine WAKE event, so the session shell (state machine,
LED, timeout policy) is entry-agnostic.

Bus wiring:
    ctl.hotkey{action:"wake"}  -->  WAKE  (from wafer-ctl)
    WakeController.feed_pcm()  -->  KWS detect --> WAKE_WORD event --> WAKE
                                     (from the olv P5 patch, which forwards
                                      mic frames while in standby)

The hotkey ALWAYS stays as the fallback (D22): a KWS outage degrades to
hotkey-only wake, never to "cannot wake at all".
"""

from __future__ import annotations

import logging

from core.bus import topics
from core.bus.hub import InProcBus
from core.interfaces import WakeWordEngine
from core.session.state_machine import Event, SessionStateMachine

logger = logging.getLogger(__name__)


class WakeController:
    """Owns the wake entries and drives the session state machine."""

    def __init__(
        self,
        bus: InProcBus,
        state_machine: SessionStateMachine,
        engine: WakeWordEngine | None = None,
    ) -> None:
        self._bus = bus
        self._sm = state_machine
        self._engine = engine
        bus.subscribe(topics.CTL_HOTKEY, self._on_hotkey)

    @property
    def state_machine(self) -> SessionStateMachine:
        return self._sm

    async def _on_hotkey(self, topic: str, payload: dict) -> None:
        """Hotkey wake (the always-on fallback)."""
        if payload.get("action") == "wake":
            self.wake(source="hotkey")

    def wake(self, source: str, keyword: str | None = None) -> bool:
        """Fire the WAKE event; returns True if the session actually woke."""
        self._sm.handle(Event.WAKE, source=source, keyword=keyword)
        if self._sm.state.value == "listening":
            logger.info("session woke via %s", source)
            return True
        return False

    async def feed_pcm(self, pcm: bytes) -> bool:
        """Feed mic audio to the KWS engine (called by the olv P5 patch).

        Only meaningful while asleep: in LISTENING+ states the pipeline
        itself owns the audio and the wake word is NOT monitored (D8:
        wake word is the door to a session, not a per-turn requirement).
        """
        if self._engine is None:
            return False
        if self._sm.state.value != "standby":
            return False  # already awake (or busy): KWS stays silent

        result = self._engine.detect(pcm)
        if result.hit:
            await self._bus.publish(topics.WAKE_WORD, {"keyword": "auto"})
            self.wake(source="wake_word")
            return True
        return False
