"""OLV adapter layer: connects wafer-core to the OLV audio pipeline.

This module implements the HAL interfaces by wrapping OLV's internal engines.
It is the bridge between "our business logic" and "OLV's audio machinery".

Per D18 placement rule: this code DOES depend on OLV internals (that's its job).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from core.bus import topics
from core.bus.hub import InProcBus
from core.interfaces import AudioSegment

logger = logging.getLogger(__name__)


class OlvAudioSource:
    """Wraps OLV's audio engine to yield VAD-cut audio segments.

    In OLV, the WebSocket handler receives mic-audio-data, accumulates it in
    received_data_buffers, and triggers conversation on mic-audio-end.
    Our P1 patch intercepts before ASR. This adapter receives the buffer
    directly from the patch point.
    """

    def __init__(self) -> None:
        self._segments: list[AudioSegment] = []
        self._callback = None

    def feed_segment(self, pcm: bytes, start_ts: float, end_ts: float) -> None:
        """Called by the P1 patch when OLV yields an audio segment."""
        self._segments.append(AudioSegment(pcm=pcm, start_ts=start_ts, end_ts=end_ts))

    async def segments(self) -> AsyncIterator[AudioSegment]:
        """Yield pending segments (consumed by the pipeline)."""
        while self._segments:
            yield self._segments.pop(0)


class OlvAdapter:
    """Main adapter: wires wafer-core modules into OLV's conversation flow.

    This is the integration point that the P2 patch (custom Agent) uses.
    OLV calls our AgentInterface.chat() -> we route through wafer-core.
    """

    def __init__(self, bus: InProcBus) -> None:
        self._bus = bus
        self.audio_source = OlvAudioSource()

    async def on_audio_segment(self, pcm: bytes, start_ts: float, end_ts: float) -> None:
        """Called by P1 patch: an audio segment is ready before ASR."""
        self.audio_source.feed_segment(pcm, start_ts, end_ts)

    async def publish_state(self, state: str) -> None:
        """Publish session state changes to hub (P4 patch)."""
        await self._bus.publish(topics.SESSION_STATE, {"state": state})

    async def publish_vad(self, vad: str) -> None:
        """Publish VAD events to hub (P4 patch)."""
        await self._bus.publish(topics.AUDIO_STATE, {"vad": vad})
