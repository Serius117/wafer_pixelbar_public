"""WaferAgent: the only module that talks to the LLM (design doc §3.6, §4.6).

Responsibilities:
  1. Assemble prompt (persona + memory injection)
  2. Stream LLM response
  3. Parse and strip emotion tags
  4. Forward emotion to bus, text to TTS
  5. Proxy tool calls (stamp speaker identity — D12, never exposed to LLM)
"""

from __future__ import annotations

import logging

from core.bus.hub import InProcBus
from core.interfaces import LlmEngine, Message, Route, TtsEngine
from core.memory import MemoryManager
from core.naming import DEFAULT_ROBOT_NAME

logger = logging.getLogger(__name__)

EMOTION_PREFIX = "[emotion:"
EMOTION_SUFFIX = "]"


def parse_emotion_tag(text: str) -> tuple[str | None, str]:
    """Extract leading [emotion:xxx] tag. Returns (tag, remaining_text)."""
    if text.startswith(EMOTION_PREFIX):
        end = text.find(EMOTION_SUFFIX)
        if end > len(EMOTION_PREFIX):
            tag = text[len(EMOTION_PREFIX):end].strip()
            return tag, text[end + len(EMOTION_SUFFIX):]
    return None, text


class WaferAgent:
    """The sole LLM interface: prompt assembly, streaming, emotion, TTS relay."""

    def __init__(
        self,
        llm: LlmEngine,
        tts: TtsEngine,
        bus: InProcBus,
        memory: MemoryManager,
        persona_template: str = "",
        robot_name: str = DEFAULT_ROBOT_NAME,
    ) -> None:
        self._llm = llm
        self._tts = tts
        self._bus = bus
        self._memory = memory
        self._persona = persona_template
        self._robot_name = robot_name

    async def chat_turn(self, text: str, speaker_id: str) -> Route:
        """Handle one chat turn: build prompt -> stream LLM -> extract emotion -> TTS."""
        # Build messages: system (persona + memory) + user text
        system_msgs = self._memory.build_context(speaker_id, self._persona, self._robot_name)
        messages = list(system_msgs) + [Message(role="user", content=text)]

        # Stream LLM response
        full_text = ""
        async for chunk in self._llm.stream(messages):
            full_text += chunk

        # Parse emotion tag from full text
        emotion_tag, speak_text = parse_emotion_tag(full_text)
        if emotion_tag:
            await self._bus.publish("llm.emotion", {"tag": emotion_tag})
            logger.info("Emotion: %s", emotion_tag)

        # Speak the response (strip tag)
        if speak_text.strip():
            async for _ in self._tts.speak(speak_text.strip()):
                pass

        # Record turn for rolling summary trigger
        should_summarize = self._memory.record_turn(speaker_id, text, full_text)
        if should_summarize:
            await self._summarize(speaker_id)

        return Route.CHAT

    async def _summarize(self, speaker_id: str) -> None:
        """Generate a rolling summary of recent conversation."""
        # Simplified v1: just log, real summarization uses the same LLM
        logger.info("Rolling summary triggered for %s (turn %d)",
                     speaker_id, self._memory.get_memory(speaker_id).turn_count)
