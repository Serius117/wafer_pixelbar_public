"""Contract tests for core.interfaces: API surface, enum values, structural typing."""

import core.interfaces as ifaces

EXPECTED_PROTOCOLS = [
    "AudioSource",
    "VadEngine",
    "WakeWordEngine",
    "SpeakerVerifier",
    "AsrEngine",
    "LlmEngine",
    "TtsEngine",
    "LedSink",
    "TaskRunner",
    "HotkeySource",
    "Notifier",
]


def test_protocol_surface() -> None:
    for name in EXPECTED_PROTOCOLS:
        assert hasattr(ifaces, name), f"missing protocol: {name}"
    assert len(EXPECTED_PROTOCOLS) == 11


def test_route_enum() -> None:
    assert {r.value for r in ifaces.Route} == {
        "chat",
        "llm",
        "task_direct",
        "task_denied",
        "session_end",
        "task_amend",
        "task_cancel",
        "task_reset",
    }


def test_session_states() -> None:
    assert {s.value for s in ifaces.SessionState} == {
        "listening",
        "thinking",
        "speaking",
        "task_running",
        "standby",
    }


def test_dataclass_basics() -> None:
    seg = ifaces.AudioSegment(pcm=b"\x00\x01", start_ts=0.0, end_ts=1.5)
    assert seg.end_ts == 1.5

    gate = ifaces.GateResult(verdict="pass", speaker_id="master", score=0.71)
    assert gate.verdict == "pass"

    frame = ifaces.Frame(pixels=b"\x00" * (256 * 32 * 3))
    assert (frame.width, frame.height) == (256, 32)

    spec = ifaces.TaskSpec(task_id="t-1", prompt="fix login bug", project="D:/code/x")
    assert spec.project == "D:/code/x"


def test_structural_typing() -> None:
    class FakeSink:
        def push(self, frame: ifaces.Frame, dirty_rect: ifaces.Rect) -> None:
            pass

    assert isinstance(FakeSink(), ifaces.LedSink)
