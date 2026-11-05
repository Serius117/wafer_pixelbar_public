"""wafer-hub: local WebSocket pub/sub event bus + in-process bus for tests.

The hub is a pure relay: it receives JSON messages tagged with a topic and
forwards them to all subscribers of that topic. It stores nothing, decides
nothing, and validates nothing (those responsibilities live with publishers
and subscribers per the data-plane/control-plane split in design doc §2.3).

Two implementations share the same BusProtocol interface:
- HubServer: real WebSocket server (production, cross-process)
- InProcBus: in-process pub/sub (tests, simulator, CI)
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)

# Type alias for subscriber callbacks
Subscriber = Callable[[str, dict[str, Any]], None]
AsyncSubscriber = Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]


class BusProtocol:
    """Minimal interface both HubServer and InProcBus satisfy."""

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Publish a message to all subscribers of the topic."""
        raise NotImplementedError

    def subscribe(self, topic: str, callback: Subscriber | AsyncSubscriber) -> None:
        """Register a callback for a topic. Callback receives (topic, payload)."""
        raise NotImplementedError

    def unsubscribe(self, topic: str, callback: Subscriber | AsyncSubscriber) -> None:
        """Remove a previously registered callback."""
        raise NotImplementedError

    async def close(self) -> None:
        """Shut down the bus."""
        raise NotImplementedError


class InProcBus(BusProtocol):
    """In-process pub/sub bus for tests and the simulator.

    Synchronous callbacks are called directly; async callbacks are scheduled
    on the running event loop. Zero network overhead.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Subscriber | AsyncSubscriber]] = defaultdict(list)
        self._wildcard: list[Subscriber | AsyncSubscriber] = []  # subscribe to "*"
        self._closed = False

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        if self._closed:
            return
        for cb in list(self._subscribers[topic]) + list(self._wildcard):
            try:
                result = cb(topic, payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("InProcBus subscriber error on topic=%s", topic)

    def subscribe(self, topic: str, callback: Subscriber | AsyncSubscriber) -> None:
        if topic == "*":
            self._wildcard.append(callback)
        else:
            self._subscribers[topic].append(callback)

    def unsubscribe(self, topic: str, callback: Subscriber | AsyncSubscriber) -> None:
        pool = self._wildcard if topic == "*" else self._subscribers[topic]
        if callback in pool:
            pool.remove(callback)

    async def close(self) -> None:
        self._closed = True
        self._subscribers.clear()
        self._wildcard.clear()


class HubServer:
    """WebSocket pub/sub server for cross-process event relay.

    Runs on localhost, accepts WebSocket connections, and relays JSON messages.
    Each message is a JSON object with a "topic" field; the server forwards
    the full message object to all subscribers of that topic.

    Designed to be ~200 lines with zero external dependencies beyond
    the `websockets` package (already in project dependencies).
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.host = host
        self.port = port
        self._server: asyncio.Server | None = None
        self._clients: set[asyncio.StreamWriter] = set()  # raw TCP for simplicity
        self._sub_map: dict[str, set[int]] = defaultdict(set)  # topic -> client ids
        self._client_subs: dict[int, set[str]] = defaultdict(set)  # client_id -> topics
        self._next_id = 0
        self._closed = False

    async def start(self) -> None:
        """Start the TCP server. port=0 picks a free ephemeral port (tests);
        the actual port is then readable from `self.port`."""
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        if self.port == 0:
            self.port = self._server.sockets[0].getsockname()[1]
        logger.info("HubServer listening on %s:%d", self.host, self.port)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        client_id = self._next_id
        self._next_id += 1
        self._clients.add(writer)
        logger.debug("Hub client %d connected", client_id)

        try:
            while not self._closed:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8"))
                    topic = msg.get("topic", "")
                    if not topic:
                        continue
                    # Pure relay: broadcast to ALL clients (including the
                    # sender). Topic filtering is client-side (HubClient),
                    # which makes TCP-hub semantics identical to InProcBus:
                    # a subscriber hears every message on its topics,
                    # regardless of who published. (W.7: the old
                    # `task.`-prefix special case is removed.)
                    payload_bytes = json.dumps(msg, ensure_ascii=False) + "\n"
                    for w in list(self._clients):
                        try:
                            w.write(payload_bytes.encode("utf-8"))
                        except Exception:
                            self._clients.discard(w)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    logger.warning("Hub received malformed message from client %d", client_id)
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            self._clients.discard(writer)
            # Release the server-side transport on disconnect: Python 3.12+
            # Server.wait_closed() blocks on live connections, so the handler
            # itself must close its writer (CI ran 3.11, which did not wait).
            try:
                writer.close()
            except Exception:
                pass
            logger.debug("Hub client %d disconnected", client_id)

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Publish from within the server process (not used in production;
        external processes connect via TCP and send JSON lines)."""
        msg = json.dumps({"topic": topic, **payload}, ensure_ascii=False) + "\n"
        for w in list(self._clients):
            try:
                w.write(msg.encode("utf-8"))
            except Exception:
                self._clients.discard(w)

    async def close(self) -> None:
        self._closed = True
        if self._server:
            self._server.close()
        # Close client transports BEFORE awaiting wait_closed: Python 3.12+
        # wait_closed() waits for every live connection to finish, so leaving
        # them open here would deadlock the shutdown.
        for w in list(self._clients):
            w.close()
        self._clients.clear()
        if self._server:
            await self._server.wait_closed()
