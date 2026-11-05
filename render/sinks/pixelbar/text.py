"""PixelBarTextSink: the D36 real-time text channel (0xE8 / 0xEF).

The ONLY real-time output path on the real device (PIXELBAR_GUIDE):
0xE8 text pushes at ~2ms with ACK, 0xEF for layout/color and for
restoring the device's native clock scene. 0x17 (flash resource
upload, 1.4s/frame + firmware upload UI) and 0x6B (unreliable ambient
light) are NEVER used here -- structurally, this class has no method
that could emit them (TEXTFACE-05 red line).

Single-writer principle: this module and the frame sink share the
"only the render process writes HID" rule (rule 3).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from render.sinks.pixelbar import discover_pixelbar
from render.sinks.pixelbar import protocol as px

logger = logging.getLogger(__name__)

RECONNECT_INTERVAL = 2.0


class PixelBarTextSink:
    """Text-channel HID sink: show_text() + restore_clock(), nothing else.

    Never raises on push (W.10 spirit): write failures degrade and a
    reconnect is attempted on the next call.
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
        # every successful write is recorded as (command, payload) --
        # assertions and the console read this (TEXTFACE-05 red line
        # checks no 0x17/0x6B ever appears here)
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
            "PixelBarTextSink opened (interface %s, pid 0x%04X)",
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

    def _on_write_error(self, err: Exception) -> None:
        self._error_count += 1
        self._degraded = True
        logger.warning("text sink write failed (#%d): %s",
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
            logger.debug("text sink reconnect failed: %s", e)
            return False
        self._reconnect_count += 1
        logger.info("text sink reconnected (attempt #%d)",
                    self._reconnect_count)
        return True

    # --- transport -----------------------------------------------------------

    def _write_and_ack(self, command: int, payload: list[int]) -> bool:
        """Write one report and wait for the matching ACK. Never raises."""
        if self._device is None:  # dropped between calls in this push
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
            logger.warning("text sink: no ACK for cmd 0x%02X", command)
            return False
        except Exception as e:  # noqa: BLE001
            self._on_write_error(e)
            return False

    # --- public API (deliberately tiny: text + clock, that's all) -----------

    def show_text(self, text: str,
                  rgb: tuple[int, int, int] = (120, 200, 255)) -> bool:
        """Push text with a color, centered and static (0xEF then 0xE8).

        Raises ValueError BEFORE touching the device when the text
        exceeds the device limit (16 chars / 48 UTF-8 bytes) -- the
        caller (TextFaceController) pre-validates, the sink enforces
        (TEXTFACE-02).
        """
        payload = px.payload_text_set(text)  # validates, may raise
        if self._device is None and not self._maybe_reconnect():
            return False
        r, g, b = rgb
        ok_layout = self._write_and_ack(
            px.CMD_PIXEL_SET, px.payload_text_static(r, g, b, 1))
        ok_text = self._write_and_ack(px.CMD_TEXT_SET, payload)
        return ok_layout and ok_text

    def restore_clock(
        self, rgb: tuple[int, int, int] = (255, 255, 255)
    ) -> bool:
        """Restore the device's native clock scene (clears any text)."""
        if self._device is None and not self._maybe_reconnect():
            return False
        r, g, b = rgb
        return self._write_and_ack(
            px.CMD_PIXEL_SET, px.payload_clock(r, g, b))
