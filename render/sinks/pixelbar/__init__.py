"""PixelBar HID sink: pushes rendered frames to the real LED display.

Implements the LedSink protocol (core/interfaces.py). Uses the HID protocol
module (render/sinks/pixelbar/protocol.py) for frame construction.

Single-writer principle (design doc §2.2): this is the ONLY module in the
entire system that writes to the HID device.

Error handling (W.10): a write failure (device unplugged, USB glitch) must
never crash the render loop. The sink degrades, counts errors, and tries to
reconnect with a backoff interval; frames are dropped silently while the
device is away. `open()` at startup still fails fast (RuntimeError) -- a
missing device at boot is a wiring problem the user must see.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from core.interfaces import Frame, Rect
from render.sinks.pixelbar import protocol as px

logger = logging.getLogger(__name__)

# Frame rate tiers (design doc §6.4: adaptive, measure then choose)
FPS_HIGH = 30
FPS_MEDIUM = 15
FPS_LOW = 10

# Reconnect backoff: at most one reconnect attempt per interval while degraded
RECONNECT_INTERVAL = 2.0


def discover_pixelbar(hid_module: Any, vid: int, pid: int | None) -> dict:
    """Enumerate and pick the PixelBar control collection.

    Shared by the frame sink (0x17) and the text sink (0xE8/0xEF).
    Raises RuntimeError when absent -- a missing device at boot is a
    wiring problem the user must see (fail fast, W.10).

    PID is not unique across batches (0xA106/0xA160): match by vendor,
    prefer interface 4 + usage page 0xFF14 (the control collection).
    """
    devices = [
        d for d in hid_module.enumerate()
        if d["vendor_id"] == vid
        and (pid is None or d["product_id"] == pid)
    ]
    if not devices:
        wanted = "any PID" if pid is None else f"PID=0x{pid:04X}"
        raise RuntimeError(
            f"PixelBar not found (VID=0x{vid:04X}, {wanted})."
            " Close TempoHub and check USB cable."
        )

    # The control collection is interface 4 / usage page 0xFF14; a second
    # collection on the same interface is not writable.
    target = (
        next(
            (d for d in devices
             if d.get("interface_number") == px.HID_INTERFACE
             and d.get("usage_page") == px.USAGE_PAGE),
            None,
        )
        or next(
            (d for d in devices
             if d.get("interface_number") == px.HID_INTERFACE),
            None,
        )
        or devices[0]
    )
    return target


def open_pixelbar_device(vid: int = 0x2D99, pid: int | None = None):
    """Open the PixelBar control collection (the only sanctioned HID opener).

    Calibration/probe scripts use this so the single-writer guard
    (tests/test_guards.py GUARD-04) stays green: raw `hid` usage lives only
    inside render/sinks/pixelbar/.
    """
    import hid

    target = discover_pixelbar(hid, vid, pid)
    device = hid.device()
    device.open_path(target["path"])
    return device


class PixelBarSink:
    """HID sink for the PixelBar LED display.

    Pushes frames via the 0x17 file-transfer protocol (ACK-driven):
      1. select the WORK scene (0xEF) so uploaded content is displayed
      2. start (0x17 state 0) -> device requests data (state 1)
      3. data chunks (0x17 state 2), one ACK per chunk (state 3)
      4. end (0x17 state 4) -> device confirms (state 5)

    The device panel is 256x32 monochrome; RGB frames are thresholded and
    packed into the 1064-byte page format (see protocol.encode_mono_bitmap).
    """

    def __init__(
        self,
        vid: int = px.VID,
        pid: int | None = None,
        fps: int = FPS_HIGH,
        use_dirty_rect: bool = True,
        hid_module: Any = None,
        ack_timeout: float = 4.0,
    ) -> None:
        self._vid = vid
        self._pid = pid  # None = accept any PID of this vendor (0xA106/0xA160)
        self._fps = fps
        self._use_dirty = use_dirty_rect
        self._hid_module = hid_module  # injectable for tests; None -> real hid
        self._ack_timeout = ack_timeout
        self._device: Any = None
        self._last_frame: Frame | None = None
        self._mood_light_array_index = px.MOOD_LIGHT_ARRAY_INDEX_PIXELBAR
        self._last_push_time = 0.0
        self._frame_count = 0
        # W.10 error handling state
        self._error_count = 0
        self._reconnect_count = 0
        self._degraded = False
        self._next_reconnect_at = 0.0

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
            "PixelBar opened: %s (interface %s, pid 0x%04X)",
            target.get("product_string", "unknown"),
            target.get("interface_number", "?"),
            target.get("product_id", 0),
        )

    def close(self) -> None:
        """Close the HID device."""
        self._safe_close_device()
        logger.info("PixelBar closed (pushed %d frames)", self._frame_count)

    def _safe_close_device(self) -> None:
        """Close the device handle, swallowing errors (it may already be gone)."""
        if self._device is not None:
            try:
                self._device.close()
            except Exception:
                pass
            self._device = None

    # --- health (W.10) -------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._device is not None

    @property
    def is_healthy(self) -> bool:
        """True when the device is connected and the last write succeeded."""
        return self._device is not None and not self._degraded

    @property
    def error_count(self) -> int:
        return self._error_count

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    def _on_write_error(self, err: Exception) -> None:
        """A write failed: degrade, drop the handle, schedule a reconnect."""
        self._error_count += 1
        self._degraded = True
        logger.warning("PixelBar write failed (#%d): %s", self._error_count, err)
        self._safe_close_device()  # the handle is likely dead

    def _maybe_reconnect(self) -> None:
        """Try to reopen the device, throttled to RECONNECT_INTERVAL."""
        now = time.monotonic()
        if now < self._next_reconnect_at:
            return
        self._next_reconnect_at = now + RECONNECT_INTERVAL
        try:
            self.open()
        except Exception as e:
            logger.debug("PixelBar reconnect failed: %s", e)
            return
        self._reconnect_count += 1
        logger.info("PixelBar reconnected (attempt #%d)", self._reconnect_count)

    # --- frame push -----------------------------------------------------------

    def push(self, frame: Frame, dirty_rect: Rect | None = None) -> None:
        """Push a frame to the display. Never raises (W.10).

        Args:
            frame: The 256x32 RGB frame to display.
            dirty_rect: Optional dirty region. If provided and dirty-rect
                        is enabled, only that region is sent.
        """
        if not self._device:
            # Device was never opened or dropped out: try to come back,
            # throttled; drop the frame while away.
            self._maybe_reconnect()
            if not self._device:
                return

        # Frame rate limiting
        now = time.monotonic()
        min_interval = 1.0 / self._fps
        if now - self._last_push_time < min_interval:
            return
        self._last_push_time = now

        try:
            if self._use_dirty and self._last_frame is not None and dirty_rect is not None:
                # Send only the dirty region
                self._send_dirty_region(frame, dirty_rect)
            else:
                # Send full frame
                self._send_full_frame(frame)
        except Exception as e:
            self._on_write_error(e)
            return

        self._last_frame = frame
        self._frame_count += 1

    def _read_response(self, command: int,
                       first_byte: int | None = None) -> list[int] | None:
        """Read device reports until the matching reply arrives (or timeout)."""
        deadline = time.monotonic() + self._ack_timeout
        while time.monotonic() < deadline:
            raw = self._device.read(px.REPORT_LEN, timeout_ms=80)
            parsed = px.parse_response(raw)
            if parsed is None:
                continue
            cmd, payload = parsed
            if cmd != command:
                continue
            if first_byte is not None and (
                not payload or payload[0] != first_byte
            ):
                continue
            return payload
        return None

    def _select_work_scene(self) -> None:
        """Select the WORK scene slot so uploaded 0x17 content is displayed."""
        self._device.write(px.build_report(
            px.CMD_PIXEL_SET, px.payload_scene_category(255, 255, 255, 2)))
        self._read_response(px.CMD_PIXEL_SET)
        self._device.write(px.build_report(
            px.CMD_PIXEL_SET, px.payload_scene_option(255, 255, 255, 2, 0)))
        self._read_response(px.CMD_PIXEL_SET)

    def _send_full_frame(self, frame: Frame) -> None:
        """Encode and upload a complete frame (ACK-driven 0x17 transfer)."""
        self._select_work_scene()
        data = px.encode_mono_bitmap(frame.pixels, frame.width, frame.height)
        size = len(data)

        self._device.write(px.build_report(
            px.CMD_PIXEL_FILE, px.payload_file_start(0, size)))
        payload = self._read_response(px.CMD_PIXEL_FILE, first_byte=0)
        if payload is None or len(payload) < 2:
            raise RuntimeError("PixelBar did not acknowledge file start")
        state = payload[1]
        if state != px.STATE_NOTIFY_APP_SEND_DATA:
            raise RuntimeError(
                f"PixelBar did not request data (state={state})")
        requested = ((payload[2] << 8) | payload[3]) if len(payload) > 3 else 0
        chunk_size = min(requested or px.PIXEL_FILE_CHUNK,
                         px.PIXEL_FILE_CHUNK)

        offset = 0
        index = 0
        while offset < size:
            chunk = data[offset:offset + chunk_size]
            self._device.write(px.build_report(
                px.CMD_PIXEL_FILE, px.payload_file_data(0, index, chunk)))
            offset += len(chunk)
            index += 1

            payload = self._read_response(px.CMD_PIXEL_FILE, first_byte=0)
            if payload is None or len(payload) < 2:
                raise RuntimeError("PixelBar did not acknowledge data chunk")
            state = payload[1]
            if state == px.STATE_NOTIFY_APP_CHECK_FINISH:
                return  # device may confirm right after the final chunk
            if state == px.STATE_NOTIFY_APP_SEND_NEXT_DATA:
                continue
            if state == px.STATE_NOTIFY_APP_CURSOR_ERROR:
                if len(payload) > 3:
                    index = (payload[2] << 8) | payload[3]
                    offset = index * chunk_size
                continue
            raise RuntimeError(f"PixelBar transfer failed (state={state})")

        self._device.write(px.build_report(
            px.CMD_PIXEL_FILE, px.payload_file_end(0)))
        while True:
            payload = self._read_response(px.CMD_PIXEL_FILE, first_byte=0)
            if payload is None or len(payload) < 2:
                raise RuntimeError("PixelBar did not confirm transfer")
            state = payload[1]
            if state == px.STATE_NOTIFY_APP_CHECK_FINISH:
                return
            if state == px.STATE_NOTIFY_APP_SEND_NEXT_DATA:
                continue
            raise RuntimeError(f"PixelBar transfer failed (state={state})")

    def _send_dirty_region(self, frame: Frame, rect: Rect) -> None:
        """Send only the dirty rows of a frame.

        For simplicity, we send complete rows (full width) within the
        dirty rect's vertical range. This is a common LED matrix optimization.
        """
        row_start = max(0, rect.y)
        row_end = min(frame.height, rect.y + rect.height)

        if row_start >= row_end:
            return

        # Dirty-rect: determine row range for future partial-update optimization
        # (currently falls back to full frame; see method docstring)

        # Send as a partial frame update
        # In a real implementation, we'd use a partial update command.
        # For now, fall back to full frame (dirty-rect optimization is
        # a performance enhancement, not a correctness requirement).
        self._send_full_frame(frame)

    def set_fps(self, fps: int) -> None:
        """Adjust the frame rate target."""
        self._fps = max(1, min(fps, FPS_HIGH))

    @property
    def frame_count(self) -> int:
        return self._frame_count

    # --- mood light (0x6A / 0x6B / 0x72) --------------------------------------

    def mood_state(self) -> dict | None:
        """Query the mood light state (0x6A). None while the device is away."""
        if not self._device:
            return None
        try:
            self._device.write(px.build_report(px.CMD_MOOD_QUERY, []))
            payload = self._read_response(px.CMD_MOOD_QUERY)
        except Exception as e:
            self._on_write_error(e)
            return None
        if payload is None:
            return None
        state = px.parse_mood_state(payload)
        if state["light_array_index"] is not None:
            self._mood_light_array_index = state["light_array_index"]
        return state

    def set_mood_light(
        self,
        r: int,
        g: int,
        b: int,
        brightness: int = px.MOOD_BRIGHTNESS_MAX,
        speed: int = px.MOOD_SPEED_DEFAULT,
        index: int = px.MOOD_INDEX_MAIN,
    ) -> bool:
        """Set the mood light colour. False when the device is away.

        `index` selects the light element, which is also the effect slot: zone 1
        (default) breathes the given colour, zone 3 (MOOD_ZONE_SOLID) shows it
        as a static colour. See protocol.MOOD_EFFECTS.

        `light_array_index` is resolved from the 0x6A query when available and
        cached; the fallback is the value measured on PID 0xA160 (0x20).
        """
        payload = px.payload_mood_set(
            self._mood_light_array_index, index, r, g, b, brightness, speed)
        return self._write_mood(px.CMD_MOOD_SET, payload)

    def set_mood_effect(
        self,
        effect: str,
        brightness: int = px.MOOD_BRIGHTNESS_MAX,
        color: tuple[int, int, int] | None = None,
        speed: int = px.MOOD_SPEED_DEFAULT,
    ) -> bool:
        """Run one of the vendor app effects (see protocol.MOOD_EFFECTS).

        Pass `color` to paint the effect's zone with a static colour (breathing,
        solid); omit it to select the effect with its stored colour.
        """
        payload = px.payload_mood_effect(
            self._mood_light_array_index, effect, brightness, color, speed)
        return self._write_mood(px.CMD_MOOD_SET, payload)

    def set_mood_switch(self, on: bool) -> bool:
        """Turn the mood light on or off (vendor master switch, light index 7)."""
        payload = px.payload_mood_switch(self._mood_light_array_index, on)
        return self._write_mood(px.CMD_MOOD_SET, payload)

    def reset_mood_light(self, index: int = px.MOOD_INDEX_MAIN) -> bool:
        """Reset the mood light to the factory effect (0x72)."""
        payload = px.payload_mood_reset(self._mood_light_array_index, index)
        return self._write_mood(px.CMD_MOOD_RESET, payload)

    def _write_mood(self, command: int, payload: list[int]) -> bool:
        """Write a mood-light payload, repeated as the device requires."""
        if not self._device:
            self._maybe_reconnect()
            if not self._device:
                return False
        try:
            report = px.build_report(command, payload)
            for _ in range(px.MOOD_WRITE_REPEAT):
                self._device.write(report)
                time.sleep(px.MOOD_WRITE_DELAY)
        except Exception as e:
            self._on_write_error(e)
            return False
        return True
