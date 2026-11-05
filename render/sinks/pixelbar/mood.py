"""PixelBarMoodSink: the D39 mood-light channel (0x6A / 0x6B / 0x72).

The mood light is the work-state indicator (design doc §4.5.2): the text
channel (0xE8) says *what* happened, this channel says *which stage* wafer
is in. Only `MoodStateController` calls into this sink, and only the mood
commands exist here -- no text, no bitmap (0x17) surface at all.

Protocol facts (USBPcap capture of TempoHub 1.3.1, PIXELBAR_GUIDE §5.1):
one light zone per effect, effect code in R, real RGB for colour effects,
speed 0xFF = keep, brightness 0-100 as the vendor app sends it.

Single-writer principle: the render process owns all HID writes (rule 3);
this sink opens its own handle (verified 2026-10-18: two handles coexist).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from render.sinks.pixelbar import discover_pixelbar
from render.sinks.pixelbar import protocol as px

logger = logging.getLogger(__name__)

RECONNECT_INTERVAL = 2.0


class PixelBarMoodSink:
    """Mood-channel HID sink: query state, set effect/colour, switch, reset.

    Never raises on a write (W.10 spirit): failures degrade the call to
    False and a reconnect is attempted on the next call.
    """

    def __init__(
        self,
        vid: int = px.VID,
        pid: int | None = None,
        hid_module: Any = None,
        ack_timeout: float = 1.0,
    ) -> None:
        self._vid = vid
        self._pid = pid  # None = any PID of this vendor (0xA106/0xA160)
        self._hid_module = hid_module  # injectable for tests
        self._ack_timeout = ack_timeout
        self._device: Any = None
        self._degraded = False
        self._error_count = 0
        self._reconnect_count = 0
        self._next_reconnect_at = 0.0
        # light_array_index is device-reported (0x6A); 0x20 is the measured
        # fallback so the first write works even before the first query.
        self._light_array_index = px.MOOD_LIGHT_ARRAY_INDEX_PIXELBAR
        # every successful write is recorded as (command, payload)
        self.command_log: list[tuple[int, list[int]]] = []

    # --- lifecycle ---------------------------------------------------------

    def _hid(self) -> Any:
        if self._hid_module is None:
            import hid  # lazy: home machine only

            self._hid_module = hid
        return self._hid_module

    def open(self) -> None:
        """Open the HID device (fail fast: RuntimeError if absent)."""
        hid_mod = self._hid()
        target = discover_pixelbar(hid_mod, self._vid, self._pid)
        self._device = hid_mod.device()
        self._device.open_path(target["path"])
        self._degraded = False
        logger.info(
            "PixelBarMoodSink opened (interface %s, pid 0x%04X)",
            target.get("interface_number", "?"),
            target.get("product_id", 0),
        )

    def close(self) -> None:
        if self._device is not None:
            try:
                self._device.close()
            except Exception:
                pass
            self._device = None

    @property
    def is_open(self) -> bool:
        return self._device is not None

    @property
    def error_count(self) -> int:
        return self._error_count

    @property
    def light_array_index(self) -> int:
        return self._light_array_index

    def _on_write_error(self, err: Exception) -> None:
        self._error_count += 1
        self._degraded = True
        logger.warning("mood sink write failed (#%d): %s",
                       self._error_count, err)
        try:
            self._device.close()
        except Exception:
            pass
        self._device = None

    def _maybe_reconnect(self) -> bool:
        now = time.monotonic()
        if now < self._next_reconnect_at:
            return False
        self._next_reconnect_at = now + RECONNECT_INTERVAL
        try:
            self.open()
        except Exception as e:
            logger.debug("mood sink reconnect failed: %s", e)
            return False
        self._reconnect_count += 1
        logger.info("mood sink reconnected (attempt #%d)",
                    self._reconnect_count)
        return True

    # --- transport ---------------------------------------------------------

    def _write_and_ack(self, command: int, payload: list[int]) -> bool:
        """Write one report and wait for the matching ACK. Never raises."""
        if self._device is None and not self._maybe_reconnect():
            return False
        try:
            self._device.write(px.build_report(command, payload))
            deadline = time.monotonic() + self._ack_timeout
            while time.monotonic() < deadline:
                raw = self._device.read(px.REPORT_LEN, timeout_ms=80)
                parsed = px.parse_response(raw)
                if parsed is None:
                    continue
                cmd, _ = parsed
                if cmd != command:
                    continue
                self.command_log.append((command, list(payload)))
                return True
            logger.warning("mood sink: no ACK for cmd 0x%02X", command)
            return False
        except Exception as e:  # noqa: BLE001
            self._on_write_error(e)
            return False

    # --- public API --------------------------------------------------------

    def mood_state(self) -> dict | None:
        """Query the mood light state (0x6A). None when the device is away."""
        if self._device is None and not self._maybe_reconnect():
            return None
        try:
            self._device.write(px.build_report(px.CMD_MOOD_QUERY, []))
            deadline = time.monotonic() + self._ack_timeout
            while time.monotonic() < deadline:
                raw = self._device.read(px.REPORT_LEN, timeout_ms=80)
                parsed = px.parse_response(raw)
                if parsed is None:
                    continue
                cmd, payload = parsed
                if cmd != px.CMD_MOOD_QUERY:
                    continue
                state = px.parse_mood_state(payload)
                if state["light_array_index"] is not None:
                    self._light_array_index = state["light_array_index"]
                return state
            logger.warning("mood sink: no 0x6A reply")
            return None
        except Exception as e:  # noqa: BLE001
            self._on_write_error(e)
            return None

    def set_mood_light(
        self,
        r: int,
        g: int,
        b: int,
        brightness: int = px.MOOD_BRIGHTNESS_MAX,
        speed: int = px.MOOD_SPEED_DEFAULT,
        index: int = px.MOOD_INDEX_MAIN,
    ) -> bool:
        """Write a colour (or effect code) to one light zone."""
        payload = px.payload_mood_set(
            self._light_array_index, index, r, g, b, brightness, speed)
        return self._write_and_ack(px.CMD_MOOD_SET, payload)

    def set_mood_effect(
        self,
        effect: str,
        brightness: int = px.MOOD_BRIGHTNESS_MAX,
        color: tuple[int, int, int] | None = None,
        speed: int = px.MOOD_SPEED_DEFAULT,
    ) -> bool:
        """Run a vendor app effect; `color` paints breathing/solid."""
        payload = px.payload_mood_effect(
            self._light_array_index, effect, brightness, color, speed)
        return self._write_and_ack(px.CMD_MOOD_SET, payload)

    def set_mood_switch(self, on: bool) -> bool:
        """Master switch (0x6B, zone 7; the flag rides in B)."""
        payload = px.payload_mood_switch(self._light_array_index, on)
        return self._write_and_ack(px.CMD_MOOD_SET, payload)

    def reset_mood_light(self, index: int = px.MOOD_INDEX_MAIN) -> bool:
        """Factory reset of one zone (0x72)."""
        payload = px.payload_mood_reset(self._light_array_index, index)
        return self._write_and_ack(px.CMD_MOOD_RESET, payload)
