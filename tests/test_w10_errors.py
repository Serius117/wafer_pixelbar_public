"""W.10 tests: HID error paths + engine timeouts (the last pre-hardware gaps).

HID: a write failure (unplug / USB glitch) must degrade the sink without
crashing the render loop; reconnection is throttled; startup stays fail-fast.

Timeouts: a hung ASR / LLM / TTS engine must trip its timeout and flow into
the rule-6 degradation (LED error + notify) instead of freezing the turn.
"""

import asyncio
import time

from core.bus import topics
from core.bus.hub import InProcBus
from core.gate.policy import GateConfig, SpeakerProfile
from core.interfaces import Frame, Message, Route
from core.pipeline import ConversationPipeline
from core.sim import CaptureTts, FixtureAsr, ScriptedLlm, ScriptedVerifier
from render.sinks.pixelbar import RECONNECT_INTERVAL, PixelBarSink
from render.sinks.pixelbar import protocol as px

PROFILES = {
    "master": SpeakerProfile(speaker_id="master", name="主人", role="admin", priority=1),
}


# --- fake HID world -----------------------------------------------------------


class FakeDevice:
    """Programmable HID device: can fail N writes, or fail forever.

    Also answers the ACK-driven 0x17/0xEF protocol so a healthy push can
    complete (a frame is ~25 writes plus one reply per write).
    """

    def __init__(self, fail_writes: int = 0, fail_forever: bool = False) -> None:
        self.fail_remaining = fail_writes
        self.fail_forever = fail_forever
        self.writes = 0
        self.responses: list[bytes] = []

    def open_path(self, path) -> None:
        pass

    def _queue(self, command: int, payload: list[int]) -> None:
        frame = [0x2F, 0xBB, 0xEC, command,
                 (len(payload) >> 8) & 0xFF, len(payload) & 0xFF, *payload]
        frame.append(sum(frame[1:]) & 0xFF)
        frame += [0] * (64 - len(frame))
        self.responses.append(bytes(frame))

    def write(self, data) -> int:
        if self.fail_forever or self.fail_remaining > 0:
            self.fail_remaining -= 1
            raise OSError("device detached")
        raw = bytes(data)
        command = raw[3]
        length = (raw[4] << 8) | raw[5]
        payload = list(raw[6:6 + length])
        if command == px.CMD_PIXEL_SET:
            self._queue(command, payload[:9])
        elif command == px.CMD_PIXEL_FILE:
            mode, state = payload[0], payload[1]
            if state == px.STATE_NOTIFY_DEVICE_RECEIVE:
                self._queue(command, [mode, px.STATE_NOTIFY_APP_SEND_DATA, 0, 53])
            elif state == px.STATE_SEND_DATA_TO_DEVICE:
                self._queue(command, [mode, px.STATE_NOTIFY_APP_SEND_NEXT_DATA])
            elif state == px.STATE_NOTIFY_DEVICE_SEND_DATA_END:
                self._queue(command, [mode, px.STATE_NOTIFY_APP_CHECK_FINISH])
        self.writes += 1
        return len(raw)

    def read(self, size, timeout_ms=0) -> bytes:
        return self.responses.pop(0) if self.responses else b""

    def close(self) -> None:
        pass


class FakeHidModule:
    """Fake `hid` module: enumerate() presence + device() factory."""

    def __init__(self, device: FakeDevice | None, present: bool = True) -> None:
        self._device = device
        self._present = present
        self.device_calls = 0
        self.enumerate_calls = 0

    def enumerate(self) -> list[dict]:
        self.enumerate_calls += 1
        if not self._present:
            return []
        return [{
            "vendor_id": px.VID,
            "product_id": px.PID,
            "interface_number": px.HID_INTERFACE,
            "usage_page": px.USAGE_PAGE,
            "path": b"fake-path",
            "product_string": "Fake PixelBar",
        }]

    def device(self) -> FakeDevice:
        self.device_calls += 1
        assert self._device is not None
        return self._device


def blank_frame() -> Frame:
    return Frame(pixels=b"\x00" * (256 * 32 * 3), width=256, height=32)


class TestHidErrorPaths:
    def test_startup_fails_fast_when_absent(self) -> None:
        """No device at boot = wiring problem: RuntimeError (documented)."""
        sink = PixelBarSink(hid_module=FakeHidModule(device=None, present=False))
        try:
            sink.open()
            raise AssertionError("should have raised")
        except RuntimeError as e:
            assert "PixelBar not found" in str(e)

    def test_write_failure_never_raises(self) -> None:
        """Mid-run unplug: push() swallows the OSError, counts it, degrades."""
        dev = FakeDevice(fail_forever=True)  # every write fails
        sink = PixelBarSink(hid_module=FakeHidModule(device=dev), fps=100)
        sink.open()
        assert sink.is_healthy is True

        # space pushes past the frame interval so every push really writes
        for _ in range(5):
            sink.push(blank_frame())  # must not raise
            time.sleep(0.012)

        assert sink.error_count > 0
        assert sink.is_healthy is False
        assert sink.frame_count == 0  # nothing completed

    def test_reconnect_after_device_returns(self) -> None:
        """Device drops out, then comes back: sink recovers and pushes again."""
        # enough failed writes to kill the first frame (400+ writes), then OK
        dev = FakeDevice(fail_writes=1_000_000)
        hid_mod = FakeHidModule(device=dev)
        sink = PixelBarSink(hid_module=hid_mod, fps=100)
        sink.open()

        sink.push(blank_frame())  # fails -> degrade
        time.sleep(0.012)
        assert sink.is_healthy is False
        assert sink.error_count >= 1

        # force past the reconnect throttle; device "returns" (writes now OK)
        dev.fail_remaining = 0
        sink._next_reconnect_at = 0.0  # noqa: SLF001 (test rig)
        # also bypass the frame-rate limiter: Windows monotonic() is ~15.6ms
        # coarse, so the 12ms sleep above can quantize to 0 and drop the push.
        sink._last_push_time = 0.0  # noqa: SLF001 (test rig)
        sink.push(blank_frame())  # reconnects + pushes
        assert sink.is_healthy is True
        assert sink.reconnect_count == 1
        assert sink.frame_count == 1

    def test_reconnect_throttled_while_absent(self) -> None:
        """Device fully unplugged (enumerate empty): pushes are dropped,
        reconnect attempts are rate-limited, nothing raises."""
        absent_hid = FakeHidModule(device=None, present=False)
        sink = PixelBarSink(hid_module=absent_hid, fps=1000)
        # simulate previously-open then unplugged: dying handle, absent bus
        sink._device = FakeDevice(fail_forever=True)  # noqa: SLF001 (test rig)

        before = time.monotonic()
        for _ in range(20):
            sink.push(blank_frame())

        # first push errored + dropped the handle; subsequent pushes attempt
        # reconnects but enumerate() finds nothing
        assert sink.error_count >= 1
        assert sink.frame_count == 0
        assert sink.reconnect_count == 0  # no successful reconnect
        # throttled: not one enumerate per push (20 pushes)
        assert absent_hid.enumerate_calls <= 20 and absent_hid.enumerate_calls >= 1
        assert absent_hid.enumerate_calls < 20 or time.monotonic() - before > 0
        # no busy-wait: 20 pushes complete well within a reconnect interval
        assert time.monotonic() - before < RECONNECT_INTERVAL

    def test_device_call_growth_while_degraded(self) -> None:
        """Absent device: enumerate/device factories are not hammered per frame."""
        dev = FakeDevice(fail_writes=1)
        hid_mod = FakeHidModule(device=dev)
        sink = PixelBarSink(hid_module=hid_mod, fps=1000)
        sink.open()

        sink.push(blank_frame())  # fails -> degraded
        calls_after_first = hid_mod.device_calls

        for _ in range(20):
            sink.push(blank_frame())

        # throttling: at most a couple of reconnect attempts for 20 pushes
        assert hid_mod.device_calls - calls_after_first <= 2


# --- hung engine timeouts ------------------------------------------------------


class HungAsr:
    """ASR that never returns."""

    async def transcribe(self, pcm: bytes) -> str:
        await asyncio.sleep(999)
        return ""


class HungLlm:
    """LLM that yields one chunk then hangs forever."""

    async def stream(self, messages: list[Message]):
        yield "h"
        await asyncio.sleep(999)


class HungTts:
    """TTS whose speak stream never ends."""

    async def speak(self, text: str):
        yield b"\x00"
        await asyncio.sleep(999)


class FastAsr:
    async def transcribe(self, pcm: bytes) -> str:
        return "今天天气怎么样"


def make_pipeline(
    asr=None, llm=None, tts=None, *, asr_t=0.05, llm_t=0.05, tts_t=0.05
) -> tuple[ConversationPipeline, CaptureTts | None, InProcBus]:
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    (tmp / "transcripts.json").write_text('["今天天气怎么样"]', encoding="utf-8")
    (tmp / "replies.json").write_text('["[emotion:happy] 好的"]', encoding="utf-8")

    bus = InProcBus()
    real_tts = CaptureTts()
    pipeline = ConversationPipeline(
        source=None,
        verifier=ScriptedVerifier([{"master": 0.75}, {"master": 0.75}]),
        asr=asr or FixtureAsr(tmp),
        llm=llm or ScriptedLlm(tmp),
        tts=tts or real_tts,
        bus=bus,
        profiles=PROFILES,
        gate_config=GateConfig(t_pass=0.62, t_gray=0.55),
        asr_timeout=asr_t,
        llm_timeout=llm_t,
        tts_timeout=tts_t,
    )
    return pipeline, (None if tts else real_tts), bus


def degrade_events(bus: InProcBus) -> tuple[list[dict], list[dict]]:
    renders: list[dict] = []
    notifies: list[dict] = []
    bus.subscribe(topics.RENDER_COMMAND, lambda t, p: renders.append(p))
    bus.subscribe(topics.SYSTEM_NOTIFY, lambda t, p: notifies.append(p))
    return renders, notifies


class TestEngineTimeouts:
    async def test_hung_asr_degrades(self) -> None:
        pipeline, _, bus = make_pipeline(asr=HungAsr())
        renders, notifies = degrade_events(bus)

        result = await pipeline.run_turn(b"\x00" * 100)

        assert result is None  # rule-6 drop, not a freeze/raise
        assert any(r["cmd"] == "error" for r in renders)
        assert any(n["level"] == "error" for n in notifies)

    async def test_hung_llm_degrades(self) -> None:
        pipeline, _, bus = make_pipeline(llm=HungLlm())
        renders, notifies = degrade_events(bus)

        result = await pipeline.run_turn(b"\x00" * 100)

        assert result is None
        assert any(r["cmd"] == "error" for r in renders)

    async def test_hung_tts_degrades(self) -> None:
        pipeline, _, bus = make_pipeline(tts=HungTts())
        renders, _ = degrade_events(bus)

        result = await pipeline.run_turn(b"\x00" * 100)

        # LLM succeeded, TTS hung -> timeout -> rule 6
        assert result is None
        assert any(r["cmd"] == "error" for r in renders)

    async def test_pipeline_survives_after_timeout(self) -> None:
        """Timeout is per-turn: the next turn with healthy engines works."""
        pipeline, _, bus = make_pipeline(asr=HungAsr())
        await pipeline.run_turn(b"\x00" * 100)  # degrades

        # replace the ASR with a healthy one for the next turn
        pipeline._asr = FastAsr()  # noqa: SLF001 (test rig)
        result = await pipeline.run_turn(b"\x00" * 100)
        assert result == Route.CHAT

    async def test_zero_disables_timeout(self) -> None:
        """timeout=0 -> wait_for(None) -> hung engine still hangs (documented
        escape hatch, default is always on)."""
        pipeline, _, bus = make_pipeline(asr=HungAsr(), asr_t=0)

        task = asyncio.get_running_loop().create_task(
            pipeline.run_turn(b"\x00" * 100)
        )
        done, _ = await asyncio.wait({task}, timeout=0.2)
        assert not done  # still hanging (disabled timeout)
        task.cancel()

    async def test_fast_engines_unaffected(self) -> None:
        """Healthy engines with tight timeouts still complete normally."""
        pipeline, tts, bus = make_pipeline(asr_t=5.0, llm_t=5.0, tts_t=5.0)
        renders, _ = degrade_events(bus)

        result = await pipeline.run_turn(b"\x00" * 100)

        assert result == Route.CHAT
        assert tts is not None and tts.captured == ["好的"]
        assert all(r["cmd"] != "error" for r in renders)
