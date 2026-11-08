"""Task composer: restricted rewrite + context extraction (T4.4, D14).

Takes raw ASR transcript + recent conversation context, produces a structured
task prompt for opencode. Constraints (D14):
  - Only reorganize, never add new content
  - Preserve all constraint words (只/不要/先)
  - On failure/timeout, fall back to raw transcript
  - TTS confirmation before dispatch
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ComposedTask:
    """A structured task prompt ready for opencode."""

    prompt: str
    original_text: str
    speaker_id: str
    task_id: str
    needs_confirmation: bool = True
    composed: bool = True  # False = fell back to raw text


class TaskComposer:
    """Composes clean task prompts from voice transcripts.

    v1: rule-based (deterministic). Future: LLM-assisted with constraints.
    Multi-project (D33): `extract_project()` pulls "在<别名>项目里" out of
    the transcript and returns the alias; the pipeline uses it to switch
    projects before dispatching.
    """

    def __init__(self, project_aliases: list[str] | None = None) -> None:
        self._task_counter = 0
        self._aliases = list(project_aliases) if project_aliases else []

    def _next_task_id(self) -> str:
        import time
        self._task_counter += 1
        ts = time.strftime("%Y%m%d")
        return f"t-{ts}-{self._task_counter:03d}"

    def compose(
        self,
        transcript: str,
        speaker_id: str,
        recent_context: list[str] | None = None,
    ) -> ComposedTask:
        """Compose a task prompt from a voice transcript.

        Args:
            transcript: Raw ASR text (e.g. "帮我修一下登录报错")
            speaker_id: Who said it (for permission checking)
            recent_context: Recent conversation lines for reference resolution
        """
        task_id = self._next_task_id()
        text = transcript.strip()

        # Rule-based cleanup: remove filler words, normalize
        cleaned = self._clean_transcript(text)

        # Try to resolve references from context
        if recent_context:
            resolved = self._resolve_references(cleaned, recent_context)
        else:
            resolved = cleaned

        # Build structured prompt
        prompt = self._build_prompt(resolved, recent_context)

        return ComposedTask(
            prompt=prompt,
            original_text=text,
            speaker_id=speaker_id,
            task_id=task_id,
        )

    def amend(
        self,
        original: ComposedTask,
        correction_text: str,
    ) -> ComposedTask:
        """Re-compose a task with user correction (D15 amendment loop)."""
        # Merge: original prompt + correction
        merged = f"{original.prompt}\n\n[User correction] {correction_text.strip()}"
        return ComposedTask(
            prompt=self._build_prompt(merged, None),
            original_text=f"{original.original_text} → {correction_text}",
            speaker_id=original.speaker_id,
            task_id=original.task_id,  # same task, revised
        )

    # Fillers are only stripped at sentence start: mid-sentence "那个" is a
    # legitimate object ("打开那个文件") and must survive (D14: never mangle).
    _LEADING_FILLERS = ("嗯", "啊", "呃", "那个", "就是说", "然后呢", "你知道吗")

    def set_project_aliases(self, aliases: list[str]) -> None:
        """Update the known project aliases (called on config reload)."""
        self._aliases = list(aliases)

    def extract_project(self, text: str) -> tuple[str | None, str, str]:
        """Extract a project alias from the transcript (D33 deterministic grammar).

        Handles aliases that already contain "项目" as a suffix (e.g. "字幕项目"
        matches "在字幕项目里" directly, not "在字幕项目项目里").

        Returns (alias_or_None, spoken_or_empty, remaining_text_stripped).
        """
        import re

        for alias in self._aliases:
            if alias.endswith("项目"):
                # alias already has 项目: match "在字幕项目里" / "在字幕项目"
                patterns = (f"在{alias}里", f"在{alias}中", f"在{alias}",
                            f"去{alias}里", f"去{alias}",
                            f"回{alias}里", f"回{alias}")
            else:
                # alias is a bare name: match "在字幕项目里" via "在字幕+项目"
                patterns = (f"在{alias}项目里", f"在{alias}项目中", f"在{alias}项目",
                            f"去{alias}项目里", f"去{alias}项目",
                            f"回{alias}项目里", f"回{alias}项目")
            for pattern in patterns:
                if pattern in text:
                    return alias, "", text.replace(pattern, "", 1).strip()

        # no known alias matched; check for any project-ish pattern:
        # "在/去/回 X 里/中" where X is 1-6 chars (catches misheard names
        # like 项木 for 项目, and bare-name without 项目 suffix)
        match = re.search(r"[在去回](.{1,6}?)(?:项目|工程)[里中]?", text)
        if not match:
            # broader: "在/去/回 X 里" (X not matching known aliases)
            match = re.search(r"[在去回](.{1,6}?)里", text)
        if match:
            spoken = match.group(1)
            stripped = text[:match.start()] + text[match.end():]
            return "__NOT_FOUND__", spoken, stripped.strip()
        return None, "", text

    def _clean_transcript(self, text: str) -> str:
        """Strip leading filler words (conservative: sentence-initial only)."""
        result = text.strip()
        changed = True
        while changed and result:
            changed = False
            for filler in self._LEADING_FILLERS:
                if result.startswith(filler):
                    result = result[len(filler):].lstrip()
                    changed = True
        return " ".join(result.split()).strip()

    def _resolve_references(self, text: str, context: list[str]) -> str:
        """Try to resolve vague references using recent context.

        v1: simple heuristics only.
        """
        # "刚才那个" → try to find the most recent technical topic
        if "刚才" in text or "那个问题" in text or "之前" in text:
            # Look for technical keywords in recent context
            tech_keywords = ["报错", "bug", "测试", "文件", "函数", "接口", "登录"]
            for line in reversed(context[-5:]):
                for kw in tech_keywords:
                    if kw in line:
                        # Found a likely referent, note it
                        return f"{text} (refers to: {line.strip()})"
        return text

    def _build_prompt(self, text: str, context: list[str] | None) -> str:
        """Build the final structured prompt for opencode.

        TASK.CORE.04 D14 red line: the raw conversation context must
        NEVER appear in the prompt sent to opencode -- it is used only
        locally by _resolve_references to produce a self-contained
        prompt. The old "Recent context" appendix (v1) leaked chat
        content to the cloud and has been removed.
        """
        return text.strip()
