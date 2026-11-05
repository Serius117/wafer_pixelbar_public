"""Tests for wafer-hub: topic constants, InProcBus pub/sub, wildcard."""

import asyncio

from core.bus.hub import InProcBus
from core.bus.topics import ALL_TOPICS, AUDIO_STATE, GATE_DECISION, LLM_EMOTION, TASK_RESULT


class TestTopics:
    def test_all_topics_nonempty(self) -> None:
        assert len(ALL_TOPICS) == 20

    def test_topic_names_use_dots(self) -> None:
        for t in ALL_TOPICS:
            assert "." in t, f"topic missing dot: {t}"

    def test_no_duplicates(self) -> None:
        assert len(ALL_TOPICS) == len(list(ALL_TOPICS))


class TestInProcBus:
    async def test_basic_pubsub(self) -> None:
        bus = InProcBus()
        received: list[tuple[str, dict]] = []

        def on_msg(topic: str, payload: dict) -> None:
            received.append((topic, payload))

        bus.subscribe(GATE_DECISION, on_msg)
        await bus.publish(GATE_DECISION, {"verdict": "pass", "speaker_id": "master"})
        assert len(received) == 1
        assert received[0][1]["verdict"] == "pass"
        await bus.close()

    async def test_topic_isolation(self) -> None:
        bus = InProcBus()
        gate_msgs: list[dict] = []
        emotion_msgs: list[dict] = []

        bus.subscribe(GATE_DECISION, lambda t, p: gate_msgs.append(p))
        bus.subscribe(LLM_EMOTION, lambda t, p: emotion_msgs.append(p))

        await bus.publish(GATE_DECISION, {"verdict": "drop"})
        await bus.publish(LLM_EMOTION, {"tag": "happy"})

        assert len(gate_msgs) == 1
        assert len(emotion_msgs) == 1
        assert gate_msgs[0]["verdict"] == "drop"
        assert emotion_msgs[0]["tag"] == "happy"
        await bus.close()

    async def test_async_subscriber(self) -> None:
        bus = InProcBus()
        received: list[str] = []

        async def on_msg(topic: str, payload: dict) -> None:
            await asyncio.sleep(0.001)  # simulate async work
            received.append(payload.get("tag", ""))

        bus.subscribe(LLM_EMOTION, on_msg)
        await bus.publish(LLM_EMOTION, {"tag": "sad"})
        assert received == ["sad"]
        await bus.close()

    async def test_wildcard_subscription(self) -> None:
        bus = InProcBus()
        received: list[str] = []

        bus.subscribe("*", lambda t, p: received.append(t))
        await bus.publish(GATE_DECISION, {"verdict": "pass"})
        await bus.publish(LLM_EMOTION, {"tag": "happy"})
        await bus.publish(TASK_RESULT, {"task_id": "t-1"})
        assert sorted(received) == [GATE_DECISION, LLM_EMOTION, TASK_RESULT]
        await bus.close()

    async def test_unsubscribe(self) -> None:
        bus = InProcBus()
        received: list[dict] = []

        def on_msg(topic: str, payload: dict) -> None:
            received.append(payload)

        bus.subscribe(AUDIO_STATE, on_msg)
        await bus.publish(AUDIO_STATE, {"vad": "start"})
        assert len(received) == 1

        bus.unsubscribe(AUDIO_STATE, on_msg)
        await bus.publish(AUDIO_STATE, {"vad": "end"})
        assert len(received) == 1  # still 1, not 2
        await bus.close()

    async def test_no_subscriber_no_error(self) -> None:
        bus = InProcBus()
        await bus.publish(AUDIO_STATE, {"vad": "start"})  # should not raise
        await bus.close()

    async def test_subscriber_error_does_not_crash(self) -> None:
        bus = InProcBus()
        good: list[dict] = []

        def bad_cb(topic: str, payload: dict) -> None:
            raise RuntimeError("subscriber bug")

        def good_cb(topic: str, payload: dict) -> None:
            good.append(payload)

        bus.subscribe(AUDIO_STATE, bad_cb)
        bus.subscribe(AUDIO_STATE, good_cb)
        await bus.publish(AUDIO_STATE, {"vad": "start"})
        assert len(good) == 1  # good callback still ran despite bad one
        await bus.close()

    async def test_publish_after_close_is_noop(self) -> None:
        bus = InProcBus()
        await bus.close()
        await bus.publish(AUDIO_STATE, {"vad": "start"})  # should not raise

    async def test_multiple_subscribers_same_topic(self) -> None:
        bus = InProcBus()
        a: list[dict] = []
        b: list[dict] = []
        bus.subscribe(LLM_EMOTION, lambda t, p: a.append(p))
        bus.subscribe(LLM_EMOTION, lambda t, p: b.append(p))
        await bus.publish(LLM_EMOTION, {"tag": "surprised"})
        assert len(a) == 1
        assert len(b) == 1
        await bus.close()

    async def test_payload_passthrough_verbatim(self) -> None:
        """Payload fields must arrive exactly as published (no mutation)."""
        bus = InProcBus()
        received: list[dict] = []

        bus.subscribe(TASK_RESULT, lambda t, p: received.append(p))
        payload = {
            "task_id": "t-20261015-001",
            "summary": "已修复登录报错：路径中文导致的导入失败，测试 3 过",
            "ok": True,
        }
        await bus.publish(TASK_RESULT, payload)
        assert received[0] == payload
        await bus.close()
