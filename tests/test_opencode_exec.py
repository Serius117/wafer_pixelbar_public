"""OpencodeExecutor tests (L5 / RELEASE #1).

Parsing and binary resolution are pure/testable without opencode; the live
CLI execution is guarded by skipif (dev machine with opencode installed).
"""

import json
import shutil
from pathlib import Path

import pytest

from bridge.opencode_exec import (
    OpencodeExecutor,
    parse_event_line,
    resolve_opencode_bin,
)


def ev(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


class TestParseEventLine:
    def test_text_event(self) -> None:
        line = ev({"type": "text", "sessionID": "ses_1",
                   "part": {"type": "text", "text": "29"}})
        parsed = parse_event_line(line)
        assert parsed["kind"] == "text"
        assert parsed["text"] == "29"
        assert parsed["session_id"] == "ses_1"

    def test_tool_use_event(self) -> None:
        line = ev({"type": "tool_use", "sessionID": "ses_1", "part": {
            "type": "tool", "tool": "bash",
            "state": {"status": "completed", "title": "ls tests"}}})
        parsed = parse_event_line(line)
        assert parsed["kind"] == "tool"
        assert parsed["tool"] == "bash"
        assert "ls tests" in parsed["title"]

    def test_step_finish(self) -> None:
        line = ev({"type": "step_finish", "sessionID": "ses_1",
                   "part": {"reason": "stop"}})
        assert parse_event_line(line)["kind"] == "finish"

    def test_step_start(self) -> None:
        line = ev({"type": "step_start", "sessionID": "ses_1",
                   "part": {"type": "step-start"}})
        assert parse_event_line(line)["kind"] == "start"

    def test_garbage_never_crashes(self) -> None:
        assert parse_event_line("not json")["kind"] == "unknown"
        assert parse_event_line("")["kind"] == "unknown"
        assert parse_event_line('{"type":"weird"}')["kind"] == "unknown"

    def test_chinese_text(self) -> None:
        line = ev({"type": "text", "part": {"text": "共有 29 个测试文件"}})
        assert parse_event_line(line)["text"] == "共有 29 个测试文件"


class TestResolveBinary:
    def test_explicit_wins(self) -> None:
        assert resolve_opencode_bin("C:/x/opencode.exe") == "C:/x/opencode.exe"

    def test_shim_derivation(self, monkeypatch, tmp_path) -> None:
        shim = tmp_path / "opencode.CMD"
        shim.write_text("npm shim", encoding="utf-8")
        real = (tmp_path / "node_modules" / "opencode-ai" / "bin"
                / "opencode.exe")
        real.parent.mkdir(parents=True)
        real.write_bytes(b"MZ")
        monkeypatch.setattr(shutil, "which", lambda name: (
            str(shim) if name == "opencode" else None))
        assert resolve_opencode_bin() == str(real)

    def test_not_found_raises(self, monkeypatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(FileNotFoundError):
            resolve_opencode_bin()


class TestExecutorConstruction:
    def test_protocol_shape(self, tmp_path) -> None:
        ex = OpencodeExecutor(session_book_path=tmp_path / "sb.json")
        assert ex.remote is False
        assert ex.last_run_s is None

    def test_session_book_reload(self, tmp_path) -> None:
        from bridge.server import SessionBook

        book_a = SessionBook(tmp_path / "sb.json")
        book_a.set("proj", "ses_1")
        book_b = SessionBook(tmp_path / "sb.json")
        assert book_b.get("proj") == "ses_1"
        book_a.reset("proj")
        book_b.reload()  # picks up the on-disk reset from the other instance
        assert book_b.get("proj") is None


@pytest.mark.skipif(shutil.which("opencode") is None and not Path(
    "C:/Users/admin/nodejs/node_modules/opencode-ai/bin/opencode.exe").exists(),
    reason="opencode CLI not installed")
class TestLiveExecution:
    async def test_real_query_and_session_continuation(self, tmp_path) -> None:
        """Live: run a trivial query twice; the second run must continue the
        same session (opencode remembers the first question)."""
        from bridge.composer import ComposedTask
        from core.bus.hub import InProcBus

        bus = InProcBus()
        events: list[tuple[str, dict]] = []
        bus.subscribe("*", lambda t, p: events.append((t, dict(p))))
        ex = OpencodeExecutor(session_book_path=tmp_path / "sb.json")

        t1 = ComposedTask(prompt="用一句话回答：1+1等于几？只回答答案。",
                          original_text="问一加一", speaker_id="master",
                          task_id="t-live-1")
        await ex.run(bus, t1, str(Path.cwd()))

        results = [p for t, p in events if t == "task.result"]
        assert results and "2" in results[0]["summary"]
        assert results[0].get("ok") is True  # R.15: success carries the flag
        sid = ex._book.get(str(Path.cwd()))
        assert sid and sid.startswith("ses_")

        # second task continues the session (same sid passed via --session)
        t2 = ComposedTask(prompt="我刚才问你的第一个问题是什么？一句话回答。",
                          original_text="追问", speaker_id="master",
                          task_id="t-live-2")
        events.clear()
        await ex.run(bus, t2, str(Path.cwd()))
        progress = [p for t, p in events if t == "task.progress"]
        assert any("session" in p.get("detail", "") for p in progress)
        results2 = [p for t, p in events if t == "task.result"]
        assert results2 and results2[0]["summary"].strip()


class TestFailureSemantics:
    """TEST.RESULT.01-02 (R.15): failures publish task.result ok=False."""

    def _task(self, task_id: str = "t-fail-1"):
        from bridge.composer import ComposedTask

        return ComposedTask(prompt="anything", original_text="任何任务",
                            speaker_id="master", task_id=task_id)

    async def test_nonzero_exit_publishes_ok_false(self, tmp_path) -> None:
        """A broken binary (python fed opencode-style args -> rc!=0, no
        text) must yield ok=False + error, not a crash nor a fake success."""
        import sys

        from core.bus.hub import InProcBus

        bus = InProcBus()
        events: list[tuple[str, dict]] = []
        bus.subscribe("*", lambda t, p: events.append((t, dict(p))))
        ex = OpencodeExecutor(opencode_bin=sys.executable,
                              session_book_path=tmp_path / "sb.json")
        await ex.run(bus, self._task(), str(tmp_path))
        results = [p for t, p in events if t == "task.result"]
        assert len(results) == 1
        assert results[0]["ok"] is False
        assert results[0]["error"]

    async def test_spawn_failure_publishes_ok_false(self, tmp_path) -> None:
        """Binary path that cannot even spawn -> same failure semantics."""
        from core.bus.hub import InProcBus

        bus = InProcBus()
        events: list[tuple[str, dict]] = []
        bus.subscribe("*", lambda t, p: events.append((t, dict(p))))
        ex = OpencodeExecutor(opencode_bin=str(tmp_path / "no_such_bin.exe"),
                              session_book_path=tmp_path / "sb.json")
        await ex.run(bus, self._task("t-fail-2"), str(tmp_path))
        results = [p for t, p in events if t == "task.result"]
        assert len(results) == 1
        assert results[0]["ok"] is False
