"""v0.1 ASR wake fallback: transcript gates the standby session."""

from __future__ import annotations

import pytest


class FakeBridge:
    def __init__(self) -> None:
        self._projects = None
        self.submitted: list[tuple[str, str]] = []

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


class TestAsrWake:
    @pytest.fixture
    def agent(self, monkeypatch):
        import olv.wafer_hooks as wh

        monkeypatch.setenv("WAFER_WAKE_MODE", "asr")
        monkeypatch.setattr(wh, "_current_speaker",
                            {"id": "master", "role": "admin"})
        wh._session = wh._SessionMode()
        a = wh.WaferAgent(llm=None)
        a._bridge = FakeBridge()
        a._pump_started = True
        self.llm_calls: list[str] = []

        async def fake_chat_llm(text):
            self.llm_calls.append(text)
            yield ("llm-reply", "llm-reply")

        monkeypatch.setattr(a, "_chat_llm", fake_chat_llm)
        return a

    async def _say(self, agent, text):
        return [pair async for pair in agent.chat(text)]

    async def test_no_wake_is_silent_and_stays_standby(self, agent):
        import olv.wafer_hooks as wh

        pairs = await self._say(agent, "今天天气怎么样")
        assert pairs == []
        assert self.llm_calls == []
        assert agent._bridge.submitted == []
        assert wh._session.is_standby() is True

    async def test_bare_chat_wake_opens_session(self, agent):
        import olv.wafer_hooks as wh

        pairs = await self._say(agent, "小饼小饼")
        assert pairs
        assert wh._session.is_standby() is False

    async def test_punctuated_chat_wake_opens_session(self, agent):
        import olv.wafer_hooks as wh

        pairs = await self._say(agent, "小饼，小饼，讲个笑话")
        assert pairs
        assert self.llm_calls == ["小饼，小饼，讲个笑话"]
        assert wh._session.is_standby() is False

    async def test_work_wake_forces_task(self, agent):
        import olv.wafer_hooks as wh

        pairs = await self._say(agent, "小饼干活，查一下测试文件")
        assert pairs
        assert agent._bridge.submitted == [("查一下测试文件", "master")]
        assert self.llm_calls == []
        assert wh._session.is_standby() is False

    async def test_active_session_allows_follow_up_without_wake(self, agent):
        import olv.wafer_hooks as wh

        await self._say(agent, "小饼小饼，讲个笑话")
        pairs = await self._say(agent, "再讲一个")
        assert pairs
        assert self.llm_calls == ["小饼小饼，讲个笑话", "再讲一个"]
        assert wh._session.is_standby() is False

    def test_wake_mode_defaults_to_asr(self, monkeypatch):
        import olv.wafer_hooks as wh

        monkeypatch.delenv("WAFER_WAKE_MODE", raising=False)
        assert wh._wake_mode() == "asr"

    def test_unknown_wake_mode_falls_back_to_asr(self, monkeypatch):
        import olv.wafer_hooks as wh

        monkeypatch.setenv("WAFER_WAKE_MODE", "bogus")
        assert wh._wake_mode() == "asr"
