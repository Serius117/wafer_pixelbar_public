"""TEST.CORE.01~05: chat multi-turn context (TASK.CORE.02, 2026-11-06).

Real-log motivation: "中国一共多少个朝代？" -> "具体说一下呢？" answered
"主人是在叫我吗" -- _chat_llm sent only [system, user] and _memory was
write-only. Now the last 3 chat turns are injected as messages; a session
gap (> _session.timeout_s) or an explicit 拜拜 resets the context.
"""
from __future__ import annotations

import pytest


class FakeLLM:
    """Streaming LLM double: records message lists, replies per turn."""

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies or ["好的呀[normal]"])
        self.calls: list[list[dict]] = []

    async def chat_completion(self, messages):
        self.calls.append(messages)
        reply = (self.replies.pop(0) if self.replies
                 else "嗯嗯[normal]")
        mid = max(1, len(reply) // 2)
        for chunk in (reply[:mid], reply[mid:]):  # simulate streaming
            yield chunk


class FakeBridge:
    def __init__(self) -> None:
        self._projects = None
        self.submitted: list[str] = []
        self._recent: list[str] = []
        self._active = False
        self._current_task_id: str | None = None

    def _ensure_projects(self) -> None:
        pass

    def _extract_project(self, text):
        return None, "", text

    def has_work(self) -> bool:
        return False

    async def submit(self, text, speaker_id):
        self.submitted.append(text)
        return "收到，开工！"

    async def amend(self, text):
        return ""

    async def cancel(self):
        return "已取消啦"

    async def _do_switch(self, alias):
        pass


class TestChatMemory:
    @pytest.fixture
    def agent(self, monkeypatch, tmp_path):
        import olv.wafer_hooks as wh

        monkeypatch.setattr(wh, "_current_speaker",
                            {"id": "master", "role": "admin"})
        wh._session = wh._SessionMode()
        wh._session.wake()  # listening: ASR-wake mode must not drop turns
        self.llm = FakeLLM(["中国历史上有许多朝代[normal]",
                            "宋朝，公元960年开始[normal]",
                            "嗯嗯[normal]",
                            "好的呀[normal]"])
        a = wh.WaferAgent(llm=self.llm)
        a._bridge = FakeBridge()
        a._pump_started = True
        # TASK.CORE.05: isolate disk (no stale session data leaks in)
        a.__class__._MEMORY_DIR = tmp_path / "noop"
        import time as _time
        a._memory_ts = _time.monotonic()  # no gap on first turn
        yield a
        a.__class__._MEMORY_DIR = None

    async def _say(self, agent, text):
        return [pair async for pair in agent.chat(text)]

    def _roles(self, call) -> list:
        return [m["role"] for m in call]

    async def test_01_context_carries_over(self, agent):
        """TEST.CORE.01: turn 2's messages contain turn 1's user+ai."""
        await self._say(agent, "中国一共多少个朝代")
        await self._say(agent, "具体说一下呢")
        assert len(self.llm.calls) == 2
        second = self.llm.calls[1]
        roles = self._roles(second)
        assert roles == ["system", "user", "assistant", "user"]
        assert second[1]["content"] == "中国一共多少个朝代"
        assert "朝代" in second[2]["content"]

    async def test_02_wake_residue_stripped(self, agent):
        """TEST.CORE.02: stored user text has the wake residue removed."""
        await self._say(agent, "小饼小饼，今天天气怎么样")
        await self._say(agent, "明天呢")
        stored_user = self.llm.calls[1][1]["content"]
        assert stored_user == "今天天气怎么样"

    async def test_03_memory_cap(self, agent):
        """TEST.CORE.03: at most MEMORY_MAX_MESSAGES context entries."""
        import olv.wafer_hooks as wh

        for _ in range(5):
            await self._say(agent, "再说点什么")
        last = self.llm.calls[-1]
        assert len(last) == 1 + wh.MEMORY_MAX_MESSAGES + 1  # sys+ctx+user

    async def test_04_session_gap_resets(self, agent):
        """TEST.CORE.04: a > timeout gap starts a clean slate."""
        await self._say(agent, "中国一共多少个朝代")
        agent._memory_ts -= 301.0  # simulate the 5-minute session timeout
        await self._say(agent, "具体说一下呢")
        second = self.llm.calls[1]
        assert self._roles(second) == ["system", "user"], \
            "stale cross-session context must not leak"

    async def test_05_end_resets_and_task_not_stored(self, agent):
        """TEST.CORE.05a: 拜拜 clears memory; 05b: task dispatch turns do
        not enter the chat context."""
        await self._say(agent, "中国一共多少个朝代")
        await self._say(agent, "拜拜")  # SESSION_END
        assert agent._memory == []

        await self._say(agent, "小饼干活，帮我查一下项目里的测试文件")
        # task confirm replied, memory stayed empty (task turns excluded)
        assert agent._bridge.submitted, "task must dispatch"
        assert agent._memory == [], "task turns must not enter chat memory"

    async def test_06_result_injected_into_memory(self, agent):
        """TEST.CORE.06 (Bridge A): task.result -> _memory synthetic
        exchange; the next chat turn can answer '刚才结果是多少'."""

        await self._say(agent, "中国有多少个朝代")  # a chat turn first
        n_before = len(agent._memory)

        # simulate a task.result arriving through the pump callback path
        agent._handle_task_result({
            "task_id": "t-core04", "ok": True,
            "summary": "项目里有 41 个测试文件",
            "prompt": "查一下项目里有多少个测试文件",
        })
        assert len(agent._memory) == n_before + 2, \
            "result must inject a user+assistant pair"
        assert any("[派活]" in m["content"] for m in agent._memory)
        assert any("41 个测试文件" in m["content"] for m in agent._memory)

        # the next chat turn's messages include the injected context
        self.llm.replies.append("刚才你说查了测试文件，结果是 41 个[normal]")
        await self._say(agent, "刚才结果是多少")
        msgs = self.llm.calls[-1]
        joined = "".join(m["content"] for m in msgs)
        assert "41 个测试文件" in joined, \
            "chat brain must see the injected result"

    async def test_07_recent_fed_to_composer(self, agent):
        """TEST.CORE.07 (Bridge B): recent utterances are recorded and
        passed to submit() as recent_context for anaphora resolution."""

        # chat about a technical topic (populates _recent)
        self.llm.replies.extend(["登录页面有个报错[normal]", "嗯嗯[normal]"])
        await self._say(agent, "登录页面有个报错")

        # then dispatch a task with an anaphora
        await self._say(agent, "小饼干活，帮我查一下刚才说的那个")
        assert agent._bridge.submitted, "task must dispatch"
        # _recent should contain the chat utterance about the bug
        assert any("报错" in r for r in agent._bridge._recent), \
            "chat utterance must be in _recent for the composer"

    async def test_08_privacy_no_chat_in_prompt(self, agent):
        """TEST.CORE.08 (D14 red line): the task prompt sent to opencode
        must NOT contain raw chat content -- only the resolved,
        self-contained sentence."""
        from bridge.composer import TaskComposer

        composer = TaskComposer()
        # chat content that must never leak
        recent = ["老婆今天做了红烧肉真好吃", "登录页面有个报错"]
        # dispatch with an anaphora referencing the bug
        task = composer.compose("帮我查一下刚才说的那个", "master",
                                recent_context=recent)
        # the prompt must NOT contain the raw chat about 红烧肉
        assert "红烧肉" not in task.prompt, \
            "D14 red line: raw chat must never appear in the opencode prompt"
        # it SHOULD contain the resolved reference (the bug topic)
        assert "报错" in task.prompt or "登录" in task.prompt, \
            "the anaphora must be resolved to a self-contained prompt"


class TestSessionPersistence:
    """TEST.CORE.09~12: L2 session summary + restart recovery + L3 worklog."""

    @pytest.fixture
    def agent(self, monkeypatch, tmp_path):
        """Agent with memory_dir pointed at tmp_path for isolation."""
        import olv.wafer_hooks as wh

        monkeypatch.setattr(wh, "_current_speaker",
                            {"id": "master", "role": "admin"})
        wh._session = wh._SessionMode()
        wh._session.wake()
        self.llm = FakeLLM(["中国历史上有许多朝代[normal]",
                            "宋朝，公元960年开始[normal]",
                            "好的[normal]",
                            "好的[normal]"])
        a = wh.WaferAgent(llm=self.llm)
        a._bridge = FakeBridge()
        a._pump_started = True
        # point memory dir at tmp_path
        a.__class__._MEMORY_DIR = tmp_path / "memory"
        yield a
        a.__class__._MEMORY_DIR = None  # reset class-level cache

    async def _say(self, agent, text):
        return [pair async for pair in agent.chat(text)]

    async def test_09_session_context_injected(self, agent):
        """TEST.CORE.09: after a session gap, the new session's messages
        include summaries + worklog (loaded from disk)."""
        import json
        import time as _time

        d = agent._memory_dir()
        (d / "summaries").mkdir(parents=True, exist_ok=True)
        (d / "summaries" / (_time.strftime("%Y-%m-%d") + ".json")).write_text(
            json.dumps([{"timestamp": _time.time(),
                         "summary": "聊了天气和朝代历史"}]),
            encoding="utf-8")
        (d / "worklog.json").write_text(
            json.dumps([{"timestamp": _time.time(),
                         "prompt": "查测试文件", "summary": "41 个",
                         "ok": True}]),
            encoding="utf-8")

        # simulate a session gap
        agent._memory_ts = 0.0
        await self._say(agent, "你好")
        msgs = self.llm.calls[-1]
        joined = "".join(m["content"] for m in msgs)
        assert "朝代历史" in joined, "summary must be injected"
        assert "查测试文件" in joined, "worklog must be injected"

    async def test_10_restart_recovery(self, agent):
        """TEST.CORE.10: previous session's turns from disk are injected
        on restart (same-day check)."""
        import json
        import time as _time

        # simulate a previous session on disk
        agent._memory_dir().mkdir(parents=True, exist_ok=True)
        (agent._memory_dir() / "last_session.json").write_text(
            json.dumps({
                "timestamp": _time.time(),  # today
                "memory": [
                    {"role": "user", "content": "中国有多少个朝代"},
                    {"role": "assistant", "content": "历史上有许多朝代"},
                ],
            }, ensure_ascii=False),
            encoding="utf-8")

        # restart: memory empty, gap triggers context load
        agent._memory_ts = 0.0
        agent._memory = []
        await self._say(agent, "具体说一下呢")
        msgs = self.llm.calls[-1]
        joined = "".join(m["content"] for m in msgs)
        assert "朝代" in joined, "restart recovery must inject prev turns"

    async def test_11_worklog_persisted(self, agent):
        """TEST.CORE.11 (L3): task results are appended to worklog.json."""
        import json

        agent._handle_task_result({
            "task_id": "t-l3", "ok": True,
            "summary": "53 个测试文件",
            "prompt": "查一下测试文件数量",
        })
        p = agent._memory_dir() / "worklog.json"
        assert p.exists()
        entries = json.loads(p.read_text(encoding="utf-8"))
        assert len(entries) == 1
        assert entries[0]["prompt"] == "查一下测试文件数量"
        assert "53" in entries[0]["summary"]

    async def test_12_bye_archives_session(self, agent):
        """TEST.CORE.12: 拜拜 triggers archive (summary) + clears context."""
        import asyncio

        await self._say(agent, "中国有多少个朝代")
        assert len(agent._memory) == 2
        # give the archive task a chance to run
        await self._say(agent, "拜拜")
        await asyncio.sleep(0.1)  # let background archive task complete
        assert agent._memory == []
        assert agent._session_context == []
        # original layer should have been persisted (from the chat turn)
        p = agent._memory_dir() / "last_session.json"
        assert p.exists(), "persist_memory must have been called"
