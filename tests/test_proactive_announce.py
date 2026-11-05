"""TASK.RELEASE.08: task-result proactive announcement.

The full OLV websocket queue is exercised in the live acceptance test; here
we lock the deterministic parts: summary cleaning, metadata bypass, dedupe
and the next-turn fallback.
"""

from __future__ import annotations

import pytest

from olv import wafer_hooks as wh


def test_clean_result_summary_strips_markdown_and_limits() -> None:
    raw = "## 结果\n\n| 项目 | 数量 |\n|---|---|\n| tests | **53** |"
    cleaned = wh.clean_result_summary(raw, limit=80)
    assert "##" not in cleaned
    assert "|" not in cleaned
    assert "**" not in cleaned
    assert "53" in cleaned
    assert len(cleaned) <= 81


async def test_announcement_metadata_is_spoken_verbatim(monkeypatch) -> None:
    monkeypatch.setattr(wh, "_current_speaker",
                        {"id": "master", "role": "admin"})
    agent = wh.WaferAgent(llm=None)
    pairs = [pair async for pair in agent.chat(
        "搞定，项目里有 53 个测试文件。",
        metadata={"wafer_announcement": True})]
    assert pairs == [("搞定，项目里有 53 个测试文件。",
                      "搞定，项目里有 53 个测试文件。")]


def test_pump_retries_after_hub_becomes_ready(monkeypatch) -> None:
    class FakeHub:
        def __init__(self) -> None:
            self.topics: list[str] = []

        def subscribe(self, topic, callback) -> None:
            self.topics.append(topic)

    monkeypatch.setattr(wh._state, "hub", None)
    monkeypatch.setattr(wh._state, "ready", False)
    agent = wh.WaferAgent(llm=None)
    agent._ensure_pump()
    assert agent._pump_started is False

    hub = FakeHub()
    monkeypatch.setattr(wh._state, "hub", hub)
    monkeypatch.setattr(wh._state, "ready", True)
    agent._ensure_pump()
    assert agent._pump_started is True
    assert "task.result" in hub.topics


def test_result_callback_and_task_dedupe(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(wh, "_announce_sender",
                        lambda task_id, text: calls.append((task_id, text)))
    agent = wh.WaferAgent(llm=None)
    payload = {"task_id": "t-1", "ok": True,
               "summary": "## 结果\n**53** 个测试文件"}
    agent._handle_task_result(payload)
    agent._handle_task_result(payload)
    assert calls == [("t-1", "搞定，结果 53 个测试文件")]
    assert agent._announce == []


def test_result_falls_back_to_next_turn_queue(monkeypatch) -> None:
    monkeypatch.setattr(wh, "_announce_sender", None)
    agent = wh.WaferAgent(llm=None)
    agent._handle_task_result(
        {"task_id": "t-2", "ok": False, "summary": "opencode 失败"})
    assert agent._announce == ["没弄成，opencode 失败"]


def test_proactive_sender_errors_keep_fallback(monkeypatch) -> None:
    def boom(task_id: str, text: str) -> None:
        raise RuntimeError("tts queue down")

    monkeypatch.setattr(wh, "_announce_sender", boom)
    agent = wh.WaferAgent(llm=None)
    agent._handle_task_result(
        {"task_id": "t-3", "ok": True, "summary": "完成"})
    assert agent._announce == ["搞定，完成"]


@pytest.fixture(autouse=True)
def _reset_sender():
    yield
    wh.set_announce_sender(None)
