"""`python -m core.bus` -- run the wafer-hub event bus server (process 1)."""

from __future__ import annotations

import argparse
import asyncio
import logging

from core.bus.hub import HubServer

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="wafer-hub event bus server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    async def _serve() -> None:
        server = HubServer(args.host, args.port)
        await server.start()
        logger.info("hub serving on %s:%d", args.host, args.port)
        try:
            await asyncio.Event().wait()
        finally:
            await server.close()

    asyncio.run(_serve())
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    raise SystemExit(main())
