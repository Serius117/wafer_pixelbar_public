"""`python -m gamedog` -- run the game-detection process (T5.6).

Polls the process list against the whitelist from config/gameapps.yaml and
drives mode switches on the hub. Stack stop/start commands come from the
same yaml (D26).

Usage: python -m gamedog --host 127.0.0.1 --port 8765 [--interval 2.0]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

import yaml

from core.bus.hub_client import HubClient
from gamedog.mode import ModeManager
from gamedog.service import GameDog
from gamedog.stack import CommandStackController, NoopStackController
from gamedog.watcher import ProcessWatcher, psutil_process_provider

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = Path("config/gameapps.yaml")


def load_game_config(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


async def _run(host: str, port: int, interval: float, config_path: Path) -> None:
    cfg = load_game_config(config_path)
    whitelist = cfg.get("whitelist") or []
    stack_cfg = cfg.get("stack") or {}

    bus = HubClient(host, port)
    await bus.connect()

    stop_cmds = stack_cfg.get("stop_commands") or []
    start_cmds = stack_cfg.get("start_commands") or []
    stack = (
        CommandStackController(stop_cmds, start_cmds)
        if (stop_cmds or start_cmds)
        else NoopStackController()
    )

    dog = GameDog(
        bus=bus,
        watcher=ProcessWatcher(whitelist, psutil_process_provider),
        mode=ModeManager(),
        stack=stack,
    )
    logger.info("gamedog ready: whitelist=%s interval=%.1fs", whitelist, interval)
    try:
        await dog.run(interval=interval)
    finally:
        await bus.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="wafer-gamedog process")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)

    asyncio.run(_run(args.host, args.port, args.interval, args.config))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    raise SystemExit(main())
