"""HAL contracts and domain types for wafer-pixel.

This module is the single authoritative definition of every hardware /
engine boundary in the system (design doc section 3.1/3.2). Business logic
(core) depends ONLY on the protocols defined here; concrete implementations
live with their touchpoint owners (engines/, olv/adapters/, render/sinks/,
ctl/, bridge/) and are wired in olv/adapters/wiring.py (or core/sim for the
simulator). Placement rule: an implementation lives with whoever owns the
touchpoint; zero-OLV-dependency engines go to engines/.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

# Python 3.11+ has StrEnum; the OLV fork venv is 3.10 (TASK.OLVCORE.01)
try:
    from enum import StrEnum
except ImportError:  # pragma: no cover (Python 3.10 only)
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore[no-redef,misc]  # noqa: UP042
        """3.10 fallback: str+Enum mixin with auto-str values."""

        def __str__(self) -> str:
            return self.value

# ---------------------------------------------------------------------------
# Domain types (shared vocabulary of the business layer)
# ---------------------------------------------------------------------------


class Route(StrEnum):
    """Intent routing result (router/scorer output, design doc 4.3)."""

    CHAT = "chat"                # pure small talk
    LLM = "llm"                  # ambiguous band -> local 9B decides
    TASK_DIRECT = "task_direct"  # confident coding intent -> bridge
    TASK_DENIED = "task_denied"  # member coding request -> deterministic refusal
    SESSION_END = "session_end"  # exit words close the session
    TASK_AMEND = "task_amend"    # correction cues while a task is active (D15)
    TASK_CANCEL = "task_cancel"  # cancel cues while a task is active (D15)
    TASK_RESET = "task_reset"    # drop the working session (D16/D18)


class SessionState(StrEnum):
    """Conversation state machine states (design doc 4.1)."""

    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    TASK_RUNNING = "task_running"
    STANDBY = "standby"          # v1.1 sleep mode: wake word only, zero transcription


@dataclass(frozen=True, slots=True)
class AudioSegment:
    """One VAD-cut utterance: PCM 16kHz/16bit/mono plus timestamps."""

    pcm: bytes
    start_ts: float
    end_ts: float


GateVerdict = Literal["pass", "drop", "gray"]


@dataclass(frozen=True, slots=True)
class GateResult:
    """Voiceprint gate decision (core/gate/policy output, design doc 4.2)."""

    verdict: GateVerdict
    speaker_id: str | None  # set only on pass
    score: float


@dataclass(frozen=True, slots=True)
class Message:
    """A single chat message for the LLM (role: system / user / assistant)."""

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class Rect:
    """A rectangle region inside the LED canvas (dirty-rect updates)."""

    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class Frame:
    """A 256x32 RGB888 bitmap (pixels = width * height * 3 bytes)."""

    pixels: bytes
    width: int = 256
    height: int = 32


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """A composed task prompt handed to the bridge / opencode (D14/D18)."""

    task_id: str
    prompt: str
    project: str  # working directory (default project when unspecified)


@dataclass(frozen=True, slots=True)
class TaskEvent:
    """Progress / permission / result notification from a running task."""

    task_id: str
    kind: Literal["progress", "permission", "result"]
    detail: str  # human-readable one-liner (LED status area / TTS payload)


@dataclass(frozen=True, slots=True)
class TaskResult:
    """Final outcome of a task (one-line summary, requirement 12#9)."""

    task_id: str
    summary: str
    ok: bool


@dataclass(frozen=True, slots=True)
class VadEvent:
    """Speech activity boundary within a PCM chunk."""

    kind: Literal["start", "end"]
    ts: float


@dataclass(frozen=True, slots=True)
class WakeResult:
    """Wake word detector output (v1.1)."""

    hit: bool
    score: float
    keyword: str = ""  # D48: which keyword hit ("" on miss)


@dataclass(frozen=True, slots=True)
class HotkeyEvent:
    """A semantic hotkey action (e.g. gate_toggle, approve) from ctl."""

    action: str


# ---------------------------------------------------------------------------
# HAL protocols (design doc 3.1 interface matrix -- 11 contracts)
# ---------------------------------------------------------------------------


@runtime_checkable
class AudioSource(Protocol):
    """Ears: yields VAD-cut audio segments (real: OLV audio engine; sim: WAV fixtures)."""

    def segments(self) -> AsyncIterator[AudioSegment]: ...


@runtime_checkable
class VadEngine(Protocol):
    """Speech activity detection over PCM chunks (real: silero; sim: fixture timeline)."""

    def process(self, pcm: bytes, ts: float) -> list[VadEvent]: ...


@runtime_checkable
class WakeWordEngine(Protocol):
    """Wake word detector (v1.1; real: openWakeWord; sim: hotkey / fixture trigger)."""

    def detect(self, pcm: bytes) -> WakeResult: ...


@runtime_checkable
class SpeakerVerifier(Protocol):
    """Voiceprint engine (real: CAM++; sim: fixture embeddings).

    Embeddings are plain float sequences so core stays numpy-free; concrete
    implementations may use numpy internally (layering rule, design doc 2.2).
    """

    def embed(self, pcm: bytes) -> Sequence[float]: ...

    def scores(self, pcm: bytes) -> dict[str, float]:
        """Cosine similarity per enrolled speaker id (input to gate policy)."""
        ...


@runtime_checkable
class AsrEngine(Protocol):
    """Transcription (real: faster-whisper GPU or CPU int8; sim: fixture transcripts)."""

    async def transcribe(self, pcm: bytes) -> str: ...


@runtime_checkable
class LlmEngine(Protocol):
    """Chat brain (real: Ollama or cloud API; sim: scripted replies)."""

    def stream(self, messages: Sequence[Message]) -> AsyncIterator[str]: ...


@runtime_checkable
class TtsEngine(Protocol):
    """Voice output: yields PCM chunks (16k/16bit/mono) for the given text."""

    def speak(self, text: str) -> AsyncIterator[bytes]: ...


@runtime_checkable
class LedSink(Protocol):
    """Display output (real: pixelbar HID, single writer; sims: web / terminal / file)."""

    def push(self, frame: Frame, dirty_rect: Rect) -> None: ...


@runtime_checkable
class TaskRunner(Protocol):
    """Coding task channel (real: bridge -> opencode working session; sim: scripted)."""

    def submit(self, spec: TaskSpec) -> AsyncIterator[TaskEvent]: ...

    def approve(self, task_id: str, allow: bool) -> None: ...

    def cancel(self, task_id: str) -> None: ...

    def reset(self, project: str) -> None:
        """Drop the working session of a project (D16/D18 session book)."""
        ...


@runtime_checkable
class HotkeySource(Protocol):
    """Global hotkeys (real: pynput in ctl; sim: fixture events)."""

    def events(self) -> AsyncIterator[HotkeyEvent]: ...


@runtime_checkable
class Notifier(Protocol):
    """Desktop notifications (real: winotify in ctl; sim: log line)."""

    async def notify(self, level: Literal["info", "warn", "error"], text: str) -> None: ...
