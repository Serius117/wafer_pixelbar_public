"""HubClient: TCP client for HubServer with InProcBus-identical semantics.

This is what every non-hub process (render / ctl / gamedog / bridge / olv)
uses to join the event bus. It satisfies BusProtocol, so modules written
against InProcBus (pipeline, bridge, GameDog, CtlService, ...) work
unchanged over a real socket:

    bus = HubClient("127.0.0.1", 8765)
    await bus.connect()
    bus.subscribe(LLM_EMOTION, on_emotion)
    await bus.publish(TTS_AMPLITUDE, {"rms": 0.5})

The server broadcasts every message to every client; HubClient filters
locally and only dispatches topics with registered subscribers -- which
makes the observable behaviour identical to InProcBus (W.7).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)

Subscriber = Callable[[str, dict[str, Any]], None]
AsyncSubscriber = Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]


class HubClient:
    """Async TCP client for the wafer-hub. Same interface as InProcBus."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._subscribers: dict[str, list[Subscriber | AsyncSubscriber]] = defaultdict(list)
        self._wildcard: list[Subscriber | AsyncSubscriber] = []
        self._closed = False

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        """Connect to the hub and start the dispatch loop."""
        if self.connected:
            return
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        self._closed = False
        self._reader_task = asyncio.get_running_loop().create_task(self._read_loop())
        logger.info("HubClient connected to %s:%d", self.host, self.port)

    async def _read_loop(self) -> None:
        """Read JSON lines from the hub, dispatch to local subscribers."""
        try:
            assert self._reader is not None
            while not self._closed:
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    logger.warning("HubClient received malformed line")
                    continue
                topic = msg.get("topic", "")
                if not topic:
                    continue
                payload = {k: v for k, v in msg.items() if k != "topic"}
                await self._dispatch(topic, payload)
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:
            raise
        finally:
            logger.info("HubClient read loop ended")

    async def _dispatch(self, topic: str, payload: dict[str, Any]) -> None:
        """Deliver to local subscribers (sync + async), never raising."""
        # Local filtering: only topics someone subscribed to
        callbacks = list(self._subscribers.get(topic, ())) + list(self._wildcard)
        for cb in callbacks:
            try:
                result = cb(topic, payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("HubClient subscriber error on topic=%s", topic)

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Send a message to the hub (broadcast to all clients by the server).

        HUB-12: serialization failures are caught and logged -- a bad payload
        must degrade to a dropped message, never crash the caller.
        """
        if self._writer is None or self._closed:
            logger.warning("HubClient publish before connect (topic=%s)", topic)
            return
        try:
            msg = json.dumps({"topic": topic, **payload}, ensure_ascii=False) + "\n"
        except (TypeError, ValueError) as e:
            logger.error("HubClient payload not serializable (topic=%s): %s", topic, e)
            return
        try:
            self._writer.write(msg.encode("utf-8"))
            await self._writer.drain()
        except Exception:
            logger.exception("HubClient publish failed (topic=%s)", topic)

    def subscribe(self, topic: str, callback: Subscriber | AsyncSubscriber) -> None:
        if topic == "*":
            self._wildcard.append(callback)
        else:
            self._subscribers[topic].append(callback)

    def unsubscribe(self, topic: str, callback: Subscriber | AsyncSubscriber) -> None:
        pool = self._wildcard if topic == "*" else self._subscribers.get(topic, [])
        if callback in pool:
            pool.remove(callback)

    async def close(self) -> None:
        """Disconnect from the hub."""
        self._closed = True
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
        self._subscribers.clear()
        self._wildcard.clear()
