"""W.7: HubServer + HubClient end-to-end over a real TCP socket.

Guards the cross-process bus contract: two clients on one server must see
InProcBus-identical semantics (publish/subscribe, wildcard, local filtering,
sender-hears-own, Chinese payloads, concurrent clients, disconnect safety).
"""

import asyncio

from core.bus import topics
from core.bus.hub import HubServer
from core.bus.hub_client import HubClient


async def start_hub() -> HubServer:
    server = HubServer("127.0.0.1", 0)  # ephemeral port
    await server.start()
    return server


async def connect(port: int) -> HubClient:
    client = HubClient("127.0.0.1", port)
    await client.connect()
    return client


async def until(cond, timeout: float = 2.0, step: float = 0.02) -> None:
    """Wait until cond() is true (event propagation across sockets)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while not cond():
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError("condition not met in time")
        await asyncio.sleep(step)


class TestHubEndToEnd:
    async def test_cross_client_pubsub(self) -> None:
        """A publishes, B (subscribed) receives -- the whole point of the hub."""
        server = await start_hub()
        try:
            a = await connect(server.port)
            b = await connect(server.port)
            received: list[dict] = []
            b.subscribe(topics.GATE_DECISION, lambda t, p: received.append(p))

            await a.publish(topics.GATE_DECISION, {"verdict": "pass", "score": 0.9})
            await until(lambda: len(received) == 1)

            assert received[0]["verdict"] == "pass"
            assert received[0]["score"] == 0.9
            await a.close()
            await b.close()
        finally:
            await server.close()

    async def test_local_topic_filtering(self) -> None:
        """Server broadcasts everything; HubClient delivers only subscribed topics."""
        server = await start_hub()
        try:
            a = await connect(server.port)
            b = await connect(server.port)
            gate: list[dict] = []
            b.subscribe(topics.GATE_DECISION, lambda t, p: gate.append(p))

            await a.publish(topics.GATE_DECISION, {"verdict": "drop"})
            await a.publish(topics.LLM_EMOTION, {"tag": "happy"})  # not subscribed
            await until(lambda: len(gate) == 1)
            await asyncio.sleep(0.1)

            assert len(gate) == 1  # emotion never delivered
            await a.close()
            await b.close()
        finally:
            await server.close()

    async def test_sender_hears_own_message(self) -> None:
        """Broadcast semantics: a client subscribed to a topic receives its own
        publish on that topic -- identical to InProcBus (W.7 fix, the old
        server had a `task.`-prefix special case that broke this parity)."""
        server = await start_hub()
        try:
            a = await connect(server.port)
            seen: list[dict] = []
            a.subscribe(topics.TASK_PROGRESS, lambda t, p: seen.append(p))

            await a.publish(topics.TASK_PROGRESS, {"task_id": "t-1", "phase": "running"})
            await until(lambda: len(seen) == 1)

            assert seen[0]["phase"] == "running"
            await a.close()
        finally:
            await server.close()

    async def test_task_request_round_trip(self) -> None:
        """The W.9 contract: pipeline's RemoteExecutor -> bridge process."""
        server = await start_hub()
        try:
            sender = await connect(server.port)
            receiver = await connect(server.port)
            requests: list[dict] = []
            receiver.subscribe(topics.TASK_REQUEST, lambda t, p: requests.append(p))

            await sender.publish(topics.TASK_REQUEST, {
                "op": "execute", "task": {"task_id": "t-9", "prompt": "x"},
                "project": ".",
            })
            await until(lambda: len(requests) == 1)

            assert requests[0]["op"] == "execute"
            assert requests[0]["task"]["task_id"] == "t-9"
            await sender.close()
            await receiver.close()
        finally:
            await server.close()

    async def test_chinese_payload_round_trip(self) -> None:
        server = await start_hub()
        try:
            a = await connect(server.port)
            b = await connect(server.port)
            received: list[dict] = []
            b.subscribe(topics.TASK_RESULT, lambda t, p: received.append(p))

            summary = "任务完成：已修复登录报错，测试 3 过"
            await a.publish(topics.TASK_RESULT, {"task_id": "t-1", "summary": summary})
            await until(lambda: len(received) == 1)

            assert received[0]["summary"] == summary
            await a.close()
            await b.close()
        finally:
            await server.close()

    async def test_wildcard_subscription(self) -> None:
        server = await start_hub()
        try:
            a = await connect(server.port)
            b = await connect(server.port)
            topics_seen: list[str] = []
            b.subscribe("*", lambda t, p: topics_seen.append(t))

            await a.publish(topics.GATE_DECISION, {"verdict": "drop"})
            await a.publish(topics.MODE_SWITCH, {"mode": "game"})
            await until(lambda: len(topics_seen) == 2)

            assert sorted(topics_seen) == [topics.GATE_DECISION, topics.MODE_SWITCH]
            await a.close()
            await b.close()
        finally:
            await server.close()

    async def test_server_survives_client_disconnect(self) -> None:
        server = await start_hub()
        try:
            a = await connect(server.port)
            await a.close()
            await asyncio.sleep(0.05)

            # server still serves new clients
            b = await connect(server.port)
            received: list[dict] = []
            b.subscribe(topics.SESSION_STATE, lambda t, p: received.append(p))
            await b.publish(topics.SESSION_STATE, {"state": "listening"})
            await until(lambda: len(received) == 1)
            await b.close()
        finally:
            await server.close()

    async def test_three_clients_broadcast(self) -> None:
        """All clients subscribed to a topic all receive the message."""
        server = await start_hub()
        try:
            clients = [await connect(server.port) for _ in range(3)]
            got: list[list[dict]] = [[], [], []]
            for c, g in zip(clients, got, strict=True):
                c.subscribe(topics.BENCH_TICK, lambda t, p, g=g: g.append(p))

            await clients[0].publish(topics.BENCH_TICK, {"stage": "e2e"})
            await until(lambda: all(len(g) == 1 for g in got))

            for c in clients:
                await c.close()
        finally:
            await server.close()


class TestBusParity:
    """InProcBus and HubClient must be interchangeable (same consumer code)."""

    @staticmethod
    async def run_pubsub_scenario(bus) -> list[tuple[str, dict]]:
        received: list[tuple[str, dict]] = []
        bus.subscribe(topics.LLM_EMOTION, lambda t, p: received.append((t, dict(p))))
        await bus.publish(topics.LLM_EMOTION, {"tag": "sad"})
        await asyncio.sleep(0.05)  # allow dispatch (socket or in-proc)
        return received

    async def test_inproc(self) -> None:
        from core.bus.hub import InProcBus

        bus = InProcBus()
        received = await self.run_pubsub_scenario(bus)
        assert received == [(topics.LLM_EMOTION, {"tag": "sad"})]
        await bus.close()

    async def test_hubclient(self) -> None:
        server = await start_hub()
        try:
            bus = await connect(server.port)
            received = await self.run_pubsub_scenario(bus)
            assert received == [(topics.LLM_EMOTION, {"tag": "sad"})]
            await bus.close()
        finally:
            await server.close()


class TestEphemeralPort:
    async def test_port_zero_reports_actual_port(self) -> None:
        server = await start_hub()
        try:
            assert server.port != 0
            assert server.port > 0
        finally:
            await server.close()
