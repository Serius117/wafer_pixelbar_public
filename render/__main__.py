"""`python -m render` -- run the render process (LED output).

D36 (2026-10-17): the real PixelBar has NO real-time frame channel
(0x17 is a flash resource upload). With --device the process runs the
TEXT channel: TextFaceController subscribes to bus events and pushes
kaomoji / short messages via 0xE8 (~2ms), falling back to the device's
native clock after 10s idle. The pixel face loop only runs for the
simulator / console preview (no device output).

Usage:
  python -m render --host 127.0.0.1 --port 8765 [--console PORT] [--device]
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from core.bus.hub_client import HubClient
from render.event_bridge import RenderEventBridge

logger = logging.getLogger(__name__)

TEXT_IDLE_TIMEOUT_S = 10.0  # D36: no event -> restore native clock
IDLE_TIMEOUT_S = 10.0  # SLOT.02: expression slot idle -> clock


class CountingSink:
    """Sim sink: counts frames (no hardware)."""

    def __init__(self) -> None:
        self.frames = 0

    def __call__(self, frame) -> None:  # noqa: ANN001
        self.frames += 1


async def _run(host: str, port: int, fps: int, use_device: bool,
               console_port: int | None) -> None:
    bus = HubClient(host, port)
    await bus.connect()

    # --- D36: real device = text channel (event-driven) ---------------------
    # --- SLOT.02: E-scheme = expression slots (0xEF) + text fallback ----
    textface = None
    mood = None
    mood_sink = None
    slotface = None
    if use_device:
        # --- E-scheme expression slots (SLOT.02, D61: v0.1 default) -------
        # v0.1 ships SLOT expressions as the default face; text mode stays
        # available via WAFER_EXPRESSION_PATH=text.
        import os

        expression_path = os.environ.get("WAFER_EXPRESSION_PATH", "slot")
        if expression_path == "slot":
            try:
                from render.slotface import ExpressionSlotController

                slotface = ExpressionSlotController(bus)
                slotface._sink.open()
                await slotface.start()
                logger.info("PixelBar SLOT sink attached (E-scheme: "
                            "expression slots, idle %.0fs -> clock)",
                            IDLE_TIMEOUT_S)
            except Exception:
                logger.exception("PixelBarSlotSink init failed, "
                                 "falling back to text-only mode")
                slotface = None

        # --- TextFace: full display (text mode only) -----------------------
        # D61 "有嘴用嘴": in slot mode TextFace is NOT started -- every v0.1
        # event (result/cancel/clarify/permission) is voiced via D58
        # proactive TTS or chat replies, and a 0xE8 push would stomp the
        # 0xEF expression category (the D60 conflict). Text display returns
        # with WAFER_EXPRESSION_PATH=text.
        if slotface is None:
            try:
                from render.sinks.pixelbar.text import PixelBarTextSink
                from render.textface import TextFaceController

                text_sink = PixelBarTextSink()
                text_sink.open()
                textface = TextFaceController(bus, text_sink,
                                              timeout_s=TEXT_IDLE_TIMEOUT_S)
                await textface.start()
                logger.info("PixelBar TEXT sink attached (D36: 0xE8 ~2ms, "
                            "%.0fs idle -> native clock)",
                            TEXT_IDLE_TIMEOUT_S)
            except Exception:
                logger.exception("PixelBarTextSink init failed, "
                                 "continuing without device output")
        else:
            logger.info("slot mode: TextFace yields to expressions "
                        "(events are voiced; WAFER_EXPRESSION_PATH=text "
                        "restores text display)")

        # --- D39: mood light = work-state indicator (own HID handle) --------
        try:
            from render.mood import MoodStateController, load_mood_states
            from render.sinks.pixelbar.mood import PixelBarMoodSink

            mood_sink = PixelBarMoodSink()
            mood_sink.open()
            mood = MoodStateController(bus, mood_sink,
                                       states=load_mood_states())
            await mood.start()
            logger.info("PixelBar MOOD sink attached (D39: work-state "
                        "indicator, %s)", " -> ".join(mood.history))
        except Exception:
            logger.exception("PixelBarMoodSink init failed, "
                             "continuing without mood light")

    # --- pixel face: simulator / console preview only ------------------------
    # (with --device this loop feeds ONLY the console preview, never HID)
    run_face_loop = (not use_device) or (console_port is not None)

    console_server = None
    face_mgr = None
    bridge = None
    _latest_frame = {"frame": None}
    if run_face_loop:
        from render.faces.manager import FaceManager

        face_mgr = FaceManager(style="wafer")  # wafer = default (F.5)

        # F group: face style switching from console/bus
        async def _on_face_switch(topic, payload):  # noqa: ANN001
            style = payload.get("style", "claw")
            face_mgr.switch(style)

        from core.bus import topics as bus_topics

        bus.subscribe(bus_topics.FACE_SWITCH, _on_face_switch)

        bridge = RenderEventBridge(bus, face_mgr)
        await bridge.start()

    # wafer-console (T6.12/T6.13): read-only monitor + limited control
    if console_port:
        from render.console import serve as console_serve

        def _frame_provider():
            return _latest_frame["frame"]

        def _text_provider():
            # D36: what the real LED is showing right now
            if textface is not None and textface.pushed:
                return textface.pushed[-1]
            return None

        console_server, _bridge = console_serve(
            bus, port=console_port, loop=asyncio.get_running_loop(),
            frame_provider=_frame_provider,
            text_provider=_text_provider,
        )

    try:
        if run_face_loop:
            interval = 1.0 / fps
            frames = 0
            logger.info("face preview loop started (%dfps, sim only)", fps)
            while True:
                bridge.apply_timeouts()
                face_mgr.tick(interval)
                # L2 image overlay has the highest priority (T6.10)
                image = bridge.image_frame()
                frame = image if image is not None else face_mgr.render()
                _latest_frame["frame"] = frame  # E.5: LED frame for console
                frames += 1
                if frames % (fps * 10) == 0:  # stats every 10s
                    logger.info("rendered %d frames, scene=%s",
                                frames, bridge.scene)
                await asyncio.sleep(interval)
        else:
            # pure device mode: event-driven, nothing to loop
            logger.info("text-only device mode: event-driven, no frame loop")
            while True:
                await asyncio.sleep(3600)
    finally:
        if mood is not None:
            mood.stop()  # D39: reset the device instead of leaving a state
        if mood_sink is not None:
            mood_sink.close()
        if textface is not None:
            textface.stop()
        if slotface is not None:
            slotface.stop()
            slotface._sink.close()
        if console_server is not None:
            console_server.shutdown()
        await bus.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="wafer-render process")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--fps", type=int, default=30,
                        help="sim/console preview frame rate")
    parser.add_argument("--device", action="store_true",
                        help="attach PixelBar TEXT sink (D36: 0xE8 channel)")
    parser.add_argument("--console", type=int, default=8790, metavar="PORT",
                        help="wafer-console port (0 = disabled)")
    args = parser.parse_args(argv)

    console_port = args.console if args.console > 0 else None
    asyncio.run(_run(args.host, args.port, args.fps, args.device, console_port))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    raise SystemExit(main())
