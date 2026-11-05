"""`python -m ctl` -- run the hotkey/notification process (T4.8).

Connects to the hub, shows toast notifications for system.notify and
task.permission events. Global hotkeys (pynput) attach at home; in sim
mode hotkeys can be fired via ctl.hotkey events from tests.

Usage: python -m ctl --host 127.0.0.1 --port 8765
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from core.bus.hub_client import HubClient
from ctl import CtlService

logger = logging.getLogger(__name__)


async def _run(host: str, port: int, hotkeys: bool) -> None:
    bus = HubClient(host, port)
    await bus.connect()

    ctl = CtlService(bus=bus)
    await ctl.handle_bus_events()
    await ctl.start()

    if hotkeys:
        try:
            import pynput  # noqa: F401 -- home machine only

            logger.info("pynput available: register global hotkeys here")
            # Home wiring: pynput.keyboard.GlobalHotKeys({
            #     ctl.hotkeys["wake"]: lambda: asyncio.create_task(ctl.fire_hotkey("wake")),
            #     ...
            # }).start()
        except ImportError:
            logger.warning("pynput not installed -- hotkeys disabled (sim mode)")

    logger.info("ctl process ready")
    try:
        await asyncio.Event().wait()
    finally:
        await ctl.stop()
        await bus.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="wafer-ctl process")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-hotkeys", action="store_true")
    args = parser.parse_args(argv)

    asyncio.run(_run(args.host, args.port, not args.no_hotkeys))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    raise SystemExit(main())
