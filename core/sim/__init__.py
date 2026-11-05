"""Simulator implementations of HAL interfaces (design doc §3.1 matrix).

These are used by CI, the simulator, and scenario replay tests. They are
deterministic: given the same fixture data, they always produce the same
output. Zero network, zero GPU, zero hardware.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from core.interfaces import (
    AudioSegment,
    HotkeyEvent,
    Message,
    TaskEvent,
    TaskSpec,
)


class WavFileSource:
    """Simulated AudioSource: replays WAV files as audio segments.

    In production the OLV audio engine yields segments from live VAD;
    here we replay pre-recorded fixtures for deterministic testing.
    """

    def __init__(self, fixture_dir: Path) -> None:
        self._fixture_dir = fixture_dir

    async def segments(self) -> AsyncIterator[AudioSegment]:
        """Yield segments from WAV files in the fixture directory.

        Files are sorted by name; each becomes one AudioSegment.
        The 'pcm' field is a placeholder (tests use the transcript fixture
        for actual text, not real audio).
        """
        wav_files = sorted(self._fixture_dir.glob("*.wav"))
        for i, wav_path in enumerate(wav_files):
            # Read just enough to prove the file exists and has content
            data = wav_path.read_bytes()
            yield AudioSegment(
                pcm=data[:160],  # first 10ms worth of bytes as placeholder
                start_ts=float(i * 2.0),
                end_ts=float(i * 2.0 + 1.5),
            )


class FixtureVerifier:
    """Simulated SpeakerVerifier: returns pre-computed similarity scores.

    Expects a JSON file with scores: {"speaker_id": score, ...}.
    """

    def __init__(self, fixture_dir: Path) -> None:
        scores_file = fixture_dir / "voiceprints.json"
        if scores_file.exists():
            self._scores: dict[str, float] = json.loads(scores_file.read_text(encoding="utf-8"))
        else:
            self._scores = {"master": 0.75}  # sensible default

    def embed(self, pcm: bytes) -> Sequence[float]:
        """Return a fixed-length dummy embedding."""
        return [0.0] * 192  # CAM++ embedding dimension

    def scores(self, pcm: bytes) -> dict[str, float]:
        return dict(self._scores)


class ScriptedVerifier:
    """Simulated SpeakerVerifier with per-call score sets (scenario replay).

    Each scores() call consumes the next entry from the list, e.g.
    [{"master": 0.75}, {"master": 0.40}] replays a member turn then a
    stranger turn. Exhausting the list repeats the last entry.
    """

    def __init__(self, score_sets: list[dict[str, float]]) -> None:
        if not score_sets:
            raise ValueError("need at least one score set")
        self._sets = [dict(s) for s in score_sets]
        self._call_count = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    def embed(self, pcm: bytes) -> Sequence[float]:
        return [0.0] * 192

    def scores(self, pcm: bytes) -> dict[str, float]:
        idx = min(self._call_count, len(self._sets) - 1)
        self._call_count += 1
        return dict(self._sets[idx])


class FixtureAsr:
    """Simulated AsrEngine: returns pre-written transcripts.

    Expects a JSON file with a list of transcripts, one per segment.
    """

    def __init__(self, fixture_dir: Path) -> None:
        transcripts_file = fixture_dir / "transcripts.json"
        if transcripts_file.exists():
            self._transcripts: list[str] = json.loads(
                transcripts_file.read_text(encoding="utf-8")
            )
        else:
            self._transcripts = ["你好"]
        self._call_count = 0

    async def transcribe(self, pcm: bytes) -> str:
        idx = self._call_count % len(self._transcripts)
        self._call_count += 1
        return self._transcripts[idx]


class ScriptedLlm:
    """Simulated LlmEngine: returns pre-written responses.

    Expects a JSON file with a list of response texts. Each call to
    stream() yields the next response character by character.
    """

    def __init__(self, fixture_dir: Path) -> None:
        replies_file = fixture_dir / "replies.json"
        if replies_file.exists():
            self._replies: list[str] = json.loads(replies_file.read_text(encoding="utf-8"))
        else:
            self._replies = ["[emotion:neutral] 你好呀"]
        self._call_count = 0

    async def stream(self, messages: Sequence[Message]) -> AsyncIterator[str]:
        idx = self._call_count % len(self._replies)
        self._call_count += 1
        reply = self._replies[idx]
        for ch in reply:
            yield ch


class CaptureTts:
    """Simulated TtsEngine: captures text instead of producing audio.

    Tests assert on the captured text to verify what was sent to TTS.
    """

    def __init__(self) -> None:
        self.captured: list[str] = []

    async def speak(self, text: str) -> AsyncIterator[bytes]:
        self.captured.append(text)
        yield b"\x00" * 160  # 10ms of silence as placeholder audio


class FakeTaskRunner:
    """Simulated TaskRunner: returns pre-scripted task events.

    Expects a JSON file with a list of event dicts:
    [{"kind": "progress", "detail": "..."}, ...]
    """

    def __init__(self, fixture_dir: Path) -> None:
        events_file = fixture_dir / "task_events.json"
        if events_file.exists():
            self._events: list[dict] = json.loads(events_file.read_text(encoding="utf-8"))
        else:
            self._events = [{"kind": "result", "detail": "done"}]

    async def submit(self, spec: TaskSpec) -> AsyncIterator[TaskEvent]:
        for evt in self._events:
            yield TaskEvent(
                task_id=spec.task_id,
                kind=evt.get("kind", "progress"),
                detail=evt.get("detail", ""),
            )

    def approve(self, task_id: str, allow: bool) -> None:
        pass

    def cancel(self, task_id: str) -> None:
        pass

    def reset(self, project: str) -> None:
        pass


class HotkeyFixtureSource:
    """Simulated HotkeySource: yields pre-configured hotkey events."""

    def __init__(self, events: list[HotkeyEvent] | None = None) -> None:
        self._events = events or []

    def set_events(self, events: list[HotkeyEvent]) -> None:
        self._events = events

    async def events(self) -> AsyncIterator[HotkeyEvent]:
        for evt in self._events:
            yield evt


class LogNotifier:
    """Simulated Notifier: logs instead of showing Windows toast."""

    def __init__(self) -> None:
        self.notifications: list[tuple[str, str]] = []

    async def notify(self, level: str, text: str) -> None:
        self.notifications.append((level, text))
