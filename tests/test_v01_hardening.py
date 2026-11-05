"""TASK.SERVE.05: v0.1 permission/result closure hardening."""

from __future__ import annotations

import asyncio

import pytest

from bridge.composer import ComposedTask
from bridge.service import BridgeService
from core.bus import topics
from core.bus.hub import InProcBus
from olv import wafer_hooks as wh


class FakeExecutor:
    def __init__(self) -> None:
        self.approvals: list[dict] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def on_approve(self, payload: dict) -> None:
        self.approvals.append(dict(payload))

    async def run(self, bus, task: ComposedTask, project: str) -> None:
        self.started.set()
        await self.release.wait()


def _task(task_id: str = "t-1") -> ComposedTask:
    return ComposedTask(task_id=task_id, prompt="查测试文件",
                        original_text="查测试文件", speaker_id="master")


class TestBridgeHardening:
    async def test_task_approve_adapter_receives_payload(self) -> None:
        bus = InProcBus()
        ex = FakeExecutor()
        svc = BridgeService(bus, executor=ex)
        await svc.start()
        await bus.publish(topics.TASK_APPROVE,
                          {"task_id": "current", "allow": True})
        assert ex.approvals == [{"task_id": "current", "allow": True}]

    async def test_second_task_is_rejected_while_active(self) -> None:
        bus = InProcBus()
        ex = FakeExecutor()
        svc = BridgeService(bus, executor=ex)
        await svc.start()
        results: list[dict] = []
        bus.subscribe(topics.TASK_RESULT,
                      lambda _t, p: results.append(dict(p)))

        await bus.publish(topics.TASK_REQUEST, {
            "op": "execute", "task": _task("t-1").__dict__,
            "project": ".",
        })
        await asyncio.wait_for(ex.started.wait(), timeout=2)
        await bus.publish(topics.TASK_REQUEST, {
            "op": "execute", "task": _task("t-2").__dict__,
            "project": ".",
        })
        assert results and "上一个任务还在跑" in results[0]["summary"]
        assert results[0]["task_id"] == "t-2"
        ex.release.set()
        await asyncio.sleep(0.05)


class FakeLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[list[dict]] = []

    async def chat_completion(self, messages):
        self.calls.append(messages)
        yield self.reply


class TestAgentHardening:
    def test_permission_is_announced_once(self, monkeypatch) -> None:
        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(wh, "_announce_sender",
                            lambda task_id, text: calls.append((task_id, text)))
        agent = wh.WaferAgent(llm=None)
        agent._announce_permission("p-1", "想运行命令")
        agent._announce_permission("p-1", "想运行命令")
        assert len(calls) == 1
        assert "批准" in calls[0][1] and "小饼，批准" in calls[0][1]

    async def test_result_is_summarized_not_prompt_echo(self, monkeypatch) -> None:
        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(wh, "_announce_sender",
                            lambda task_id, text: calls.append((task_id, text)))
        agent = wh.WaferAgent(llm=FakeLLM("项目里共有 53 个测试文件。"))
        await agent._handle_task_result_async({
            "task_id": "t-1", "prompt": "帮我查一下项目里有多少个测试文件",
            "summary": "处理完成：tests/ 下有 53 个 test_*.py",
            "ok": True,
        })
        assert calls == [("t-1", "项目里共有 53 个测试文件。")]
        assert "搞定" not in calls[0][1]

    async def test_prompt_echo_without_llm_has_explicit_fallback(
            self, monkeypatch) -> None:
        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(wh, "_announce_sender",
                            lambda task_id, text: calls.append((task_id, text)))
        agent = wh.WaferAgent(llm=None)
        await agent._handle_task_result_async({
            "task_id": "t-2", "prompt": "查一下文件",
            "summary": "查一下文件", "ok": True,
        })
        assert calls == [("t-2", "任务结束，但没有拿到有效结果")]

    async def test_result_summary_async_dedupes(self, monkeypatch) -> None:
        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(wh, "_announce_sender",
                            lambda task_id, text: calls.append((task_id, text)))
        agent = wh.WaferAgent(llm=FakeLLM("完成。"))
        payload = {"task_id": "t-3", "prompt": "做", "summary": "结果",
                   "ok": True}
        await agent._handle_task_result_async(payload)
        await agent._handle_task_result_async(payload)
        assert len(calls) == 1

    async def test_pending_permission_does_not_fall_through_to_chat(
            self, monkeypatch) -> None:
        monkeypatch.setattr(wh, "_current_speaker",
                            {"id": "master", "role": "admin"})
        wh._session = wh._SessionMode()
        wh._session.wake()
        agent = wh.WaferAgent(llm=None)
        agent._pending_permission = {"question": "想 edit out.txt",
                                     "key": "p1"}
        chat_called: list[str] = []

        async def fake_chat_llm(text):
            chat_called.append(text)
            yield ("unrelated", "unrelated")

        monkeypatch.setattr(agent, "_chat_llm", fake_chat_llm)
        pairs = [pair async for pair in agent.chat("结果怎么样了")]
        assert pairs and "批准还是拒绝" in pairs[0][0]
        assert chat_called == []

    def test_bridge_unique_ids_and_single_active(self, monkeypatch) -> None:
        published: list[dict] = []

        async def fake_publish(topic, payload):
            published.append(dict(payload))

        monkeypatch.setattr(wh, "_publish", fake_publish)
        client = wh._BridgeClient(hub=None)
        first = asyncio.run(client.submit("任务一", "master"))
        second = asyncio.run(client.submit("任务二", "master"))
        task_id = published[0]["task"]["task_id"]
        assert "收到" in first
        assert "上一个任务还在跑" in second
        assert task_id.startswith("t-wafer-")
        assert client.has_work() is True
        client.finish(task_id)
        assert client.has_work() is False

    @pytest.fixture(autouse=True)
    def _reset_sender(self):
        yield
        wh.set_announce_sender(None)
