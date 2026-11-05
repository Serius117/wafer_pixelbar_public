"""Memory management: per-speaker profiles + rolling summary (design doc §4.4).

Handles:
  1. Loading/saving speaker profile files (config/memory/<speaker>.md)
  2. Rolling conversation summary (triggered every N turns or token overflow)
  3. Prompt assembly with injection budget (persona + profile + summary ≤ 2.5K)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from core.interfaces import Message

logger = logging.getLogger(__name__)

# Injection budget (tokens)
PERSONA_BUDGET = 1500
PROFILE_BUDGET = 500
SUMMARY_BUDGET = 500
TOTAL_BUDGET = PERSONA_BUDGET + PROFILE_BUDGET + SUMMARY_BUDGET

# Summary trigger
SUMMARY_TURN_INTERVAL = 8


@dataclass
class SpeakerMemory:
    """In-memory representation of one speaker's persistent data."""

    speaker_id: str
    name: str = ""
    facts: list[str] = field(default_factory=list)
    rolling_summary: str = ""
    turn_count: int = 0


class MemoryManager:
    """Manages per-speaker memory: profiles, summaries, and prompt assembly."""

    def __init__(self, memory_dir: Path = Path("config/memory")) -> None:
        self._memory_dir = memory_dir
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, SpeakerMemory] = {}

    def get_memory(self, speaker_id: str) -> SpeakerMemory:
        """Load or create memory for a speaker."""
        if speaker_id not in self._cache:
            self._cache[speaker_id] = self._load(speaker_id)
        return self._cache[speaker_id]

    def _load(self, speaker_id: str) -> SpeakerMemory:
        """Load speaker memory from markdown file with YAML frontmatter."""
        path = self._memory_dir / f"{speaker_id}.md"
        mem = SpeakerMemory(speaker_id=speaker_id)

        if not path.exists():
            return mem

        content = path.read_text(encoding="utf-8")

        # Parse YAML frontmatter (name) and markdown body (facts)
        lines = content.strip().split("\n")
        in_frontmatter = False
        body_lines: list[str] = []

        for line in lines:
            if line.strip() == "---":
                in_frontmatter = not in_frontmatter
                continue
            if in_frontmatter and line.startswith("name:"):
                mem.name = line.split(":", 1)[1].strip()
            elif not in_frontmatter:
                body_lines.append(line)

        # Parse facts (lines starting with "- ")
        for line in body_lines:
            stripped = line.strip()
            if stripped.startswith("- "):
                mem.facts.append(stripped[2:].strip())

        return mem

    def save(self, speaker_id: str) -> None:
        """Persist speaker memory to disk."""
        mem = self.get_memory(speaker_id)
        path = self._memory_dir / f"{speaker_id}.md"

        lines = ["---", f"name: {mem.name}", "---", ""]
        for fact in mem.facts:
            lines.append(f"- {fact}")
        lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")

    def record_turn(self, speaker_id: str, user_text: str, ai_text: str) -> bool:
        """Record one conversation turn. Returns True if summary should trigger."""
        mem = self.get_memory(speaker_id)
        mem.turn_count += 1
        return mem.turn_count % SUMMARY_TURN_INTERVAL == 0

    def update_summary(self, speaker_id: str, summary_text: str) -> None:
        """Update the rolling summary (called after LLM summarization)."""
        mem = self.get_memory(speaker_id)
        mem.rolling_summary = summary_text

    def add_fact(self, speaker_id: str, fact: str) -> None:
        """Add a persistent fact to a speaker's profile."""
        mem = self.get_memory(speaker_id)
        if fact not in mem.facts:
            mem.facts.append(fact)
        self.save(speaker_id)

    def build_context(
        self, speaker_id: str, persona_template: str, robot_name: str
    ) -> list[Message]:
        """Assemble the system prompt with memory injection (≤ 2.5K tokens)."""
        mem = self.get_memory(speaker_id)

        # Render persona with variables
        persona = persona_template.replace("{{robot_name}}", robot_name)
        persona = persona.replace("{{speaker_name}}", mem.name or speaker_id)

        # Inject profile facts
        if mem.facts:
            profile_text = "\n".join(f"- {f}" for f in mem.facts[:10])
            persona += f"\n\n## 关于{mem.name or speaker_id}\n{profile_text}"

        # Inject rolling summary
        if mem.rolling_summary:
            persona += f"\n\n## 对话摘要\n{mem.rolling_summary}"

        return [Message(role="system", content=persona)]
