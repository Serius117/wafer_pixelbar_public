"""TEST.WAKE.01~07: dual wake word (D48 / R.22) -- dev-machine unit tests.

Design: docs/design/OLV_INTEGRATION.md §4.12. 01-02 (engine + wake_check)
live in test_wafer_hooks.py; this file covers the WaferAgent routing
semantics (03-07) with a fake bridge.
"""

from __future__ import annotations

import pytest


class FakeBridge:
    """Records submits; project extraction is a no-op passthrough."""

    def __init__(self) -> None:
        self.submitted: list[tuple[str, str]] = []
        self._projects = None

    def _ensure_projects(self) -> None:
        pass

    def _extract_project(self, text: str):
        return None, "", text

    def has_work(self) -> bool:
        return False

    async def submit(self, text: str, speaker_id: str) -> str:
        self.submitted.append((text, speaker_id))
        return "收到，开工！"

    async def amend(self, text: str) -> str:
        return ""

    async def cancel(self) -> str:
        return "已取消啦"

    async def _do_switch(self, alias: str) -> None:
        pass


class TestDualWakeRouting:
    @pytest.fixture
    def agent(self, monkeypatch):
        import olv.wafer_hooks as wh

        monkeypatch.setenv("WAFER_WAKE_MODE", "kws")
        monkeypatch.setattr(wh, "_current_speaker",
                            {"id": "master", "role": "admin"})
        wh._session = wh._SessionMode()
        a = wh.WaferAgent(llm=None)
        a._bridge = FakeBridge()
        self.llm_calls: list[str] = []

        async def fake_chat_llm(text):
            self.llm_calls.append(text)
            yield ("llm-reply", "llm-reply")

        monkeypatch.setattr(a, "_chat_llm", fake_chat_llm)
        return a

    async def _say(self, agent, text):
        return [pair async for pair in agent.chat(text)]

    async def test_03_work_wake_forces_task(self, agent):
        """TEST.WAKE.03: transcript with 小饼干活 + task -> stripped +
        forced TASK_DIRECT (no intent scoring, no LLM)."""
        pairs = await self._say(agent, "小饼干活帮我查一下测试文件")
        assert agent._bridge.submitted == [
            ("帮我查一下测试文件", "master")], "wake word must be stripped"
        assert not self.llm_calls, "forced route must skip the LLM"
        assert pairs and pairs[0][0] == "收到，开工！"

    async def test_04_bare_work_wake_arms_pending(self, agent):
        """TEST.WAKE.04: bare 小饼干活 -> '好，说吧' + pending_work armed,
        zero dispatch."""
        import olv.wafer_hooks as wh

        pairs = await self._say(agent, "小饼干活")
        assert any("好，说吧" in p[0] for p in pairs)
        assert agent._bridge.submitted == []
        assert wh._session.pending_work is True

    async def test_05_pending_work_next_utterance_direct(self, agent):
        """TEST.WAKE.05: after bare work-wake, the NEXT utterance is forced
        to the task route and pending_work is one-shot."""
        import olv.wafer_hooks as wh

        await self._say(agent, "小饼干活")
        pairs = await self._say(agent, "帮我跑一下测试")
        assert agent._bridge.submitted == [("帮我跑一下测试", "master")]
        assert wh._session.pending_work is False
        assert pairs[0][0] == "收到，开工！"

    async def test_06_chat_wake_not_hijacked(self, agent):
        """TEST.WAKE.06: 小饼小饼 residue keeps the normal route (chat)."""
        pairs = await self._say(agent, "小饼小饼今天天气怎么样")
        assert agent._bridge.submitted == []
        assert self.llm_calls == ["小饼小饼今天天气怎么样"], \
            "chat wake word must stay in the transcript for the LLM"
        assert pairs[0][0] == "llm-reply"

    async def test_07_d35_prefix_compat(self, agent):
        """TEST.WAKE.07: the 派活 prefix route still dispatches (D35 compat;
        the prefix is NOT stripped by the fast-track -- bridge gets the full
        text, as it always has)."""
        pairs = await self._say(agent, "派活，帮我跑一下测试")
        assert len(agent._bridge.submitted) == 1
        assert "跑一下测试" in agent._bridge.submitted[0][0]
        assert not self.llm_calls
        assert pairs[0][0] == "收到，开工！"
