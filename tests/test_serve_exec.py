"""TEST.SERVE.01~08: opencode serve integration (D51 / TASK.SERVE.01-02).

TEST.SERVE.01-04: ServeExecutor against a FakeServe HTTP server that
speaks the opencode serve API (health/session/prompt_async/SSE events/
permission reply/abort) -- protocol-faithful, no opencode needed.

TEST.SERVE.05-08: WaferAgent voice approve/deny routing (the
pending_permission pattern mirrors pending_clarify, D48 test harness).

The REAL-serve E2E (TEST.SERVE.09) lives in tests/test_serve_e2e.py
(skipif opencode binary / server password missing).
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from bridge.composer import ComposedTask
from bridge.serve_exec import (
    SERVE_PORT_SPAN,
    ServeExecutor,
    _project_port,
    _question_from_permission,
)
from core.bus.hub import InProcBus

# --- FakeServe: speaks enough of the opencode serve API ---------------------

class FakeServe:
    """Scripted opencode serve double.

    prompt_async pushes a permission.asked immediately; once the test
    answers via ServeExecutor.on_approve, the reply handler pushes the
    remaining events (text part + session.idle).
    """

    def __init__(self) -> None:
        self.replies: list[tuple[str, dict]] = []
        self.aborts: list[str] = []
        self.prompts: list[str] = []
        self.sessions: list[str] = []
        self.sse_q: queue.Queue[str | None] = queue.Queue()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # noqa: N802 (quiet)
                pass

            def _json(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if self.path == "/global/health":
                    self._json({"healthy": True, "version": "fake"})
                elif self.path == "/event":
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "text/event-stream")
                    self.end_headers()
                    try:
                        while True:
                            item = outer.sse_q.get(timeout=30)
                            if item is None:
                                break
                            self.wfile.write(f"data: {item}\n\n".encode())
                            self.wfile.flush()
                    except Exception:  # noqa: BLE001 (client disconnected)
                        pass
                else:
                    self._json({"error": "not found"}, 404)

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/session":
                    outer.sessions.append(body)
                    self._json({"id": "ses_fake123"})
                elif self.path.endswith("/prompt_async"):
                    outer.prompts.append(body["parts"][0]["text"])
                    self.send_response(204)
                    self.end_headers()
                    # script: permission fires immediately
                    outer.push_event({
                        "type": "permission.asked",
                        "properties": {
                            "id": "per_fake1", "sessionID": "ses_fake123",
                            "permission": "edit",
                            "patterns": ["C:/proj/out.txt"],
                            "metadata": {"diff": "+done\n+more\n"},
                        }})
                elif "/permissions/" in self.path:
                    outer.replies.append((self.path, body))
                    self._json(True)
                    # script: reply unblocks the task
                    outer.push_event({
                        "type": "message.part.updated",
                        "properties": {"part": {"type": "text",
                                                "text": "已创建 out.txt"}},
                    })
                    outer.push_event({
                        "type": "session.idle",
                        "properties": {"sessionID": "ses_fake123"},
                    })
                elif self.path.endswith("/abort"):
                    outer.aborts.append(self.path)
                    self._json(True)
                else:
                    self._json({"error": "not found"}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def push_event(self, ev: dict) -> None:
        self.sse_q.put(json.dumps(ev, ensure_ascii=False))

    def stop(self) -> None:
        self.sse_q.put(None)
        self.server.shutdown()
        self.server.server_close()


def _task() -> ComposedTask:
    return ComposedTask(task_id="t-serve-1", prompt="创建 out.txt",
                        original_text="创建 out.txt", speaker_id="master")


class TestServeExecutor:
    async def test_01_port_deterministic(self, tmp_path):
        """TEST.SERVE.01: crc32 port mapping is stable across calls."""
        p = str(tmp_path)
        assert _project_port(p) == _project_port(p)
        assert _project_port(p) != _project_port(p + "x")
        assert _project_port(p) >= 14100
        assert _project_port(p) < 14100 + SERVE_PORT_SPAN

    def test_02_question_from_permission(self):
        """TEST.SERVE.02: permission payload -> spoken question."""
        q = _question_from_permission({
            "permission": "edit",
            "patterns": ["C:/some/long/path/out.txt"],
            "metadata": {"diff": "x\n+a\n+b\n-c\n"},
        })
        assert "edit" in q and "out.txt" in q
        assert "加 2 行" in q and "删 1 行" in q and "批准" in q

    @pytest.fixture
    def wired(self, tmp_path, monkeypatch):
        """FakeServe + ServeExecutor with the port injected."""
        import bridge.serve_exec as se

        fake = FakeServe()
        monkeypatch.setattr(se, "_project_port",
                           lambda project, base=14100: fake.port)
        return fake, str(tmp_path)

    async def test_03_run_full_loop_with_permission(self, wired):
        """TEST.SERVE.03: dispatch -> permission.asked -> approve ->
        reply(once) -> text -> idle -> task.result(ok)."""
        fake, project = wired
        bus = InProcBus()
        seen: list[tuple[str, dict]] = []
        bus.subscribe("task.permission",
                      lambda t, p: seen.append((t, dict(p))))
        bus.subscribe("task.result",
                      lambda t, p: seen.append((t, dict(p))))

        ex = ServeExecutor(session_book_path=Path(project) / "book.json",
                           timeout=15)
        try:
            run_task = asyncio.get_running_loop().create_task(
                ex.run(bus, _task(), project))
            # wait for the permission to surface on the hub
            for _ in range(50):
                if any(t == "task.permission" for t, _ in seen):
                    break
                await asyncio.sleep(0.1)
            perms = [p for t, p in seen if t == "task.permission"]
            assert perms, "task.permission must be published"
            assert "out.txt" in perms[0]["question"]
            assert perms[0]["kind"] == "edit"

            # voice-approved -> executor.on_approve (hub TASK_APPROVE
            # is wired by BridgeService in production)
            await ex.on_approve({"task_id": "t-serve-1", "allow": True})

            await asyncio.wait_for(run_task, timeout=15)
            results = [p for t, p in seen if t == "task.result"]
            assert results and results[0]["ok"] is True
            assert "out.txt" in results[0]["summary"]
            # the reply hit the fake with the correct vocabulary
            assert fake.replies and fake.replies[0][1] == {"response": "once"}
        finally:
            await ex.close()
            fake.stop()

    async def test_04_deny_and_timeout_paths(self, wired):
        """TEST.SERVE.04a: deny -> reply(reject); 04b: timeout -> fail."""
        fake, project = wired
        bus = InProcBus()
        seen: list[dict] = []
        bus.subscribe("task.result", lambda t, p: seen.append(dict(p)))

        ex = ServeExecutor(session_book_path=Path(project) / "book.json",
                           timeout=15)
        # deny path
        rt = asyncio.get_running_loop().create_task(
            ex.run(bus, _task(), project))
        for _ in range(50):
            if ex._pending:
                break
            await asyncio.sleep(0.1)
        await ex.on_approve({"allow": False})
        await asyncio.wait_for(rt, timeout=15)
        assert fake.replies[-1][1] == {"response": "reject"}
        assert seen and seen[-1]["ok"] is True  # deny-then-finish ok
        await ex.close()

        # timeout path: no reply ever arrives -> abort + failure
        fake.push_event = lambda ev: None  # silence the script
        fake.aborts.clear()
        ex2 = ServeExecutor(session_book_path=Path(project) / "b2.json",
                            timeout=2)
        try:
            await ex2.run(bus, _task(), project)
            assert seen[-1]["ok"] is False
            assert "失败" in seen[-1]["summary"]
        finally:
            await ex2.close()
            fake.stop()

    async def test_12_multi_task_scenario(self, wired):
        """TEST.SERVE.12: scenario -- two tasks back to back (approve then
        deny), one executor, session reused via the book, both results
        correct and attributed to the right task_id."""
        fake, project = wired
        bus = InProcBus()
        results: list[dict] = []
        perms: list[dict] = []
        bus.subscribe("task.result",
                      lambda t, p: results.append(dict(p)))
        bus.subscribe("task.permission",
                      lambda t, p: perms.append(dict(p)))

        ex = ServeExecutor(session_book_path=Path(project) / "book.json",
                           timeout=15)
        try:
            # task 1: approve
            t1 = ComposedTask(task_id="t-scen-1", prompt="创建 a.txt",
                              original_text="创建 a.txt", speaker_id="master")
            rt1 = asyncio.get_running_loop().create_task(
                ex.run(bus, t1, project))
            for _ in range(50):
                if perms:
                    break
                await asyncio.sleep(0.1)
            assert perms, "task1 permission must arrive"
            await ex.on_approve({"task_id": "t-scen-1", "allow": True})
            await asyncio.wait_for(rt1, timeout=15)
            assert results[-1]["task_id"] == "t-scen-1"
            assert results[-1]["ok"] is True

            # task 2 (same project): deny -- session must be reused,
            # not re-created
            sessions_before = len(fake.sessions)
            t2 = ComposedTask(task_id="t-scen-2", prompt="创建 b.txt",
                              original_text="创建 b.txt", speaker_id="master")
            rt2 = asyncio.get_running_loop().create_task(
                ex.run(bus, t2, project))
            for _ in range(50):
                if len(perms) >= 2:
                    break
                await asyncio.sleep(0.1)
            assert len(perms) == 2, "task2 permission must arrive"
            await ex.on_approve({"task_id": "t-scen-2", "allow": False})
            await asyncio.wait_for(rt2, timeout=15)
            assert results[-1]["task_id"] == "t-scen-2"
            assert len(fake.sessions) == sessions_before, \
                "session book must reuse the serve session (D16/D18)"
            # reply vocabulary per decision
            assert fake.replies[0][1] == {"response": "once"}
            assert fake.replies[1][1] == {"response": "reject"}
        finally:
            await ex.close()
            fake.stop()


class FakeBridge:
    """Mirror of test_dual_wake's FakeBridge."""

    def __init__(self) -> None:
        self._projects = None

    def _ensure_projects(self) -> None:
        pass

    def _extract_project(self, text):
        return None, "", text

    def has_work(self) -> bool:
        return False

    async def submit(self, text, speaker_id):
        return "收到"

    async def amend(self, text):
        return ""

    async def cancel(self):
        return "已取消啦"

    async def _do_switch(self, alias):
        pass


class TestVoiceApprovalRouting:
    @pytest.fixture
    def agent(self, monkeypatch):
        import olv.wafer_hooks as wh

        monkeypatch.setenv("WAFER_WAKE_MODE", "kws")
        monkeypatch.setattr(wh, "_current_speaker",
                            {"id": "master", "role": "admin"})
        wh._session = wh._SessionMode()
        a = wh.WaferAgent(llm=None)
        a._bridge = FakeBridge()
        a._pump_started = True  # no hub in unit tests
        self.approves: list[dict] = []

        async def fake_publish(topic, payload):
            if topic == wh.topics.TASK_APPROVE:
                self.approves.append(dict(payload))

        monkeypatch.setattr(wh, "_publish", fake_publish)
        return a

    async def _say(self, agent, text):
        return [pair async for pair in agent.chat(text)]

    async def test_05_approve_phrase(self, agent):
        """TEST.SERVE.05: 批准 -> TASK_APPROVE{allow:true} + ack speech."""
        agent._pending_permission = {"question": "想改 out.txt"}
        pairs = await self._say(agent, "批准")
        assert self.approves == [{"task_id": "current", "allow": True}]
        assert any("批准" in p[0] for p in pairs)

    async def test_06_deny_phrase(self, agent):
        """TEST.SERVE.06: 拒绝 -> TASK_APPROVE{allow:false}."""
        agent._pending_permission = {"question": "想改 out.txt"}
        await self._say(agent, "不行")
        assert self.approves == [{"task_id": "current", "allow": False}]

    async def test_07_other_text_announces(self, agent):
        """TEST.SERVE.07: non-cue text announces the question, keeps
        pending, and the next cue still works."""
        agent._pending_permission = {"question": "想改 out.txt，批准吗"}
        pairs = await self._say(agent, "今天天气怎么样")
        assert any("批准还是拒绝" in p[0] for p in pairs)
        assert agent._pending_permission is not None
        await self._say(agent, "可以")
        assert self.approves == [{"task_id": "current", "allow": True}]

    async def test_08_result_announce_flush(self, agent, monkeypatch):
        """TEST.SERVE.08: queued task results are spoken at the next turn
        (OLV has no proactive TTS)."""
        agent._announce = ["搞定，41 个测试文件"]
        self.llm_calls = []

        async def fake_chat_llm(text):
            self.llm_calls.append(text)
            yield ("llm-reply", "llm-reply")

        monkeypatch.setattr(agent, "_chat_llm", fake_chat_llm)
        pairs = await self._say(agent, "怎么样了")
        assert any("搞定" in p[0] for p in pairs)
        assert agent._announce == []
        assert self.llm_calls, "after flushing, the turn proceeds normally"

    async def test_10_wake_residue_approve(self, agent):
        """TEST.SERVE.10: standby re-entry keeps the wake word in the
        transcript (D48) -- "小饼小饼，批准" must still route to approval."""
        agent._pending_permission = {"question": "想改 out.txt"}
        pairs = await self._say(agent, "小饼小饼，批准。")
        assert self.approves == [{"task_id": "current", "allow": True}]
        assert any("批准" in p[0] for p in pairs)
        # work-wake residue works too
        agent._pending_permission = {"question": "想改 out.txt"}
        await self._say(agent, "小饼干活 拒绝")
        assert self.approves[-1] == {"task_id": "current", "allow": False}

    async def test_11_member_cannot_approve(self, agent, monkeypatch):
        """TEST.SERVE.11: SECURITY -- a registered non-admin saying 批准
        must NOT reach the approval path (D12 identity tiers)."""
        import olv.wafer_hooks as wh

        monkeypatch.setattr(wh, "_current_speaker",
                            {"id": "wife", "role": "member"})
        agent._pending_permission = {"question": "想改 out.txt"}
        self.llm_calls = []

        async def fake_chat_llm(text):
            self.llm_calls.append(text)
            yield ("llm-reply", "llm-reply")

        monkeypatch.setattr(agent, "_chat_llm", fake_chat_llm)
        await self._say(agent, "批准")
        assert self.approves == [], "member approve must be ignored"
        assert self.llm_calls, "member utterance falls through to chat"
        # pending survives; a later admin turn can still answer it
        monkeypatch.setattr(wh, "_current_speaker",
                            {"id": "master", "role": "admin"})
        await self._say(agent, "批准")
        assert self.approves == [{"task_id": "current", "allow": True}]


class FakeLLM:
    """Streaming chat-LLM double: records calls, replays a fixed reply."""

    def __init__(self, reply: str = "") -> None:
        self.reply = reply
        self.calls: list[list[dict]] = []

    async def chat_completion(self, messages):
        self.calls.append(messages)
        mid = max(1, len(self.reply) // 2)
        for chunk in (self.reply[:mid], self.reply[mid:]):  # streaming
            yield chunk

    @property
    def last_prompt(self) -> str:
        return self.calls[-1][0]["content"] if self.calls else ""


class TestLLMSemanticApproval:
    """TEST.SERVE.13-18: TASK.SERVE.04 -- LLM two-tier approval."""

    @pytest.fixture
    def agent(self, monkeypatch):
        import olv.wafer_hooks as wh

        monkeypatch.setenv("WAFER_WAKE_MODE", "kws")
        monkeypatch.setattr(wh, "_current_speaker",
                            {"id": "master", "role": "admin"})
        wh._session = wh._SessionMode()
        self.approves: list[dict] = []

        async def fake_publish(topic, payload):
            if topic == wh.topics.TASK_APPROVE:
                self.approves.append(dict(payload))

        monkeypatch.setattr(wh, "_publish", fake_publish)
        a = wh.WaferAgent(llm=None)
        # announce-then-fall-through also invokes the chat branch; make it
        # inert so LLM-call counting reflects classifier/announce only
        async def fake_chat_llm(text):
            yield ("llm-reply", "llm-reply")

        monkeypatch.setattr(a, "_chat_llm", fake_chat_llm)
        return a

    async def _say(self, agent, text):
        return [pair async for pair in agent.chat(text)]

    async def test_13_cue_hit_skips_llm(self, agent):
        """TEST.SERVE.13: fast lane -- exact cue never calls the LLM."""
        agent._llm = FakeLLM("[approve]")
        agent._pending_permission = {"question": "想改 out.txt"}
        await self._say(agent, "批准")
        assert self.approves == [{"task_id": "current", "allow": True}]
        assert not agent._llm.calls, "cue hit must skip the classifier"

    async def test_14_natural_approve(self, agent):
        """TEST.SERVE.14: 「行吧你看着办」 -> LLM [approve] -> allow."""
        agent._llm = FakeLLM("[approve]")
        agent._pending_permission = {"question": "想改 out.txt，加 2 行"}
        pairs = await self._say(agent, "行吧你看着办")
        assert self.approves == [{"task_id": "current", "allow": True}]
        assert any("批准" in p[0] for p in pairs)
        assert "行吧你看着办" in agent._llm.last_prompt

    async def test_15_natural_deny(self, agent):
        """TEST.SERVE.15: 「别了吧总觉得不靠谱」 -> [deny] -> reject."""
        agent._llm = FakeLLM("[deny]")
        agent._pending_permission = {"question": "想改 out.txt"}
        await self._say(agent, "别了吧，总觉得不靠谱")
        assert self.approves == [{"task_id": "current", "allow": False}]

    async def test_16_unclear_asks_again(self, agent):
        """TEST.SERVE.16: [unclear] -> announce question, pending kept,
        and the next natural answer still classifies."""
        agent._llm = FakeLLM("[unclear]")
        agent._pending_permission = {"question": "想改 out.txt"}
        pairs = await self._say(agent, "这个改动会不会有问题啊")
        assert self.approves == [], "unclear must never approve"
        assert any("批准还是拒绝" in p[0] for p in pairs)
        assert agent._pending_permission is not None
        agent._llm.reply = "[approve]"
        await self._say(agent, "嗯问题不大就弄吧")
        assert self.approves == [{"task_id": "current", "allow": True}]

    async def test_17_llm_down_fails_safe(self, agent):
        """TEST.SERVE.17: LLM unavailable (None / raises / no tag) ->
        template announce, pending kept -- never a silent approval."""
        agent._llm = None  # unit-test shape: no LLM at all
        agent._pending_permission = {"question": "想改 out.txt"}
        pairs = await self._say(agent, "嗯你看着办")
        assert self.approves == []
        assert any("想改 out.txt" in p[0] for p in pairs), \
            "template question must still announce"

        class ExplodingLLM:
            async def chat_completion(self, messages):
                raise RuntimeError("engine down")
                yield  # pragma: no cover (make it a generator)

        agent._llm = ExplodingLLM()
        await self._say(agent, "就这样吧")
        assert self.approves == []

        agent._llm = FakeLLM("我觉得可以批准这个操作")  # no valid tag
        await self._say(agent, "你定吧")
        assert self.approves == [], "tagless reply must count as unclear"

    async def test_18_llm_announcement(self, agent):
        """TEST.SERVE.18: LLM summarizes the permission for speech;
        result cached; template used when the LLM is down."""
        agent._llm = FakeLLM("它想改 out.txt，就是上次说的那个，加两行。")
        agent._pending_permission = {"question": "想edit out.txt，加 2 行"}
        pairs = await self._say(agent, "在吗")
        assert any("就是上次说的那个" in p[0] for p in pairs), \
            "LLM announcement must be spoken"
        assert agent._pending_permission.get("spoken"), "cached for reuse"
        n_calls = len(agent._llm.calls)
        await self._say(agent, "嗯")
        # the classifier re-runs per utterance (its reply is the cached
        # announcement text -> no tag -> unclear), but the ANNOUNCEMENT
        # itself must not re-generate: exactly one new LLM call
        assert len(agent._llm.calls) == n_calls + 1, \
            "announcement cached; only the classifier re-runs"

        agent._llm = None  # down -> template fallback
        agent._pending_permission = {"question": "想edit b.txt"}
        pairs = await self._say(agent, "在吗")
        assert any("想edit b.txt" in p[0] for p in pairs)
