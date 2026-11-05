"""Real opencode CLI executor (RELEASE #1 / L5): bridge -> opencode.

Runs `opencode run --format json` as a subprocess in the task's project
directory, streams its JSON event lines, relays progress onto the bus and
persists the session id for continuation (D16/D23: same working session
per project, survives restarts).

Event line shapes (verified live 2026-10-17 against opencode CLI):
    {"type":"step_start", "sessionID":"ses_...", ...}
    {"type":"tool_use",   "sessionID":"ses_...", "part":{"tool":"bash", ...}}
    {"type":"text",       "sessionID":"ses_...", "part":{"text":"29"}}
    {"type":"step_finish","sessionID":"ses_...", "part":{"reason":"stop"}}

The final assistant text (last "text" event before reason=="stop") becomes
the task result summary -- an LLM-generated summary by construction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

from bridge.composer import ComposedTask
from bridge.server import SESSION_BOOK_PATH, SessionBook
from core.bus import topics
from core.bus.hub import InProcBus

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300.0

# Flash-tier model per provider (same tier, different vendor naming):
# huoshan = Volcano Ark (dev machine), deepseek = deepseek.com (home).
_PROVIDER_MODELS = {
    "huoshan": "huoshan/deepseek-v4.1-flash",
    "deepseek": "deepseek/deepseek-flash",
}


def resolve_default_model() -> str:
    """Model = env WAFER_OPENCODE_MODEL > local opencode config provider.

    A hardcoded default breaks on whichever machine lacks that provider
    (home has no huoshan, dev has no deepseek.com) -- resolve from the
    machine's own opencode config instead.
    """
    env = os.environ.get("WAFER_OPENCODE_MODEL")
    if env:
        return env
    try:
        from engines.llm_api import _load_opencode_config

        providers = (_load_opencode_config().get("provider") or {})
        for name, model in _PROVIDER_MODELS.items():
            if name in providers:
                return model
        if providers:
            first = next(iter(providers))
            models = providers[first].get("models") or {}
            if isinstance(models, dict) and models:
                return f"{first}/{next(iter(models))}"
    except Exception:  # noqa: BLE001 -- config unreadable: fall through
        logger.debug("opencode config unreadable, using fallback model")
    return "deepseek/deepseek-flash"


DEFAULT_MODEL: str | None = None  # resolved lazily per machine


def resolve_opencode_bin(explicit: str | None = None) -> str:
    """Find an exec()-able opencode binary.

    The npm shim (opencode.CMD / .ps1) cannot be exec'd directly; the real
    binary is <npm-prefix>/node_modules/opencode-ai/bin/opencode.exe.
    """
    if explicit:
        return explicit
    direct = shutil.which("opencode.exe")
    if direct:
        return direct
    shim = shutil.which("opencode")
    if shim:
        candidate = (Path(shim).parent / "node_modules" / "opencode-ai"
                     / "bin" / "opencode.exe")
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(
        "opencode binary not found -- pass opencode_bin explicitly "
        "(expected at <npm-prefix>/node_modules/opencode-ai/bin/opencode.exe)"
    )


def parse_event_line(line: str) -> dict:
    """Parse one JSON event line into a normalized shape.

    Returns one of:
        {"kind": "session", "session_id": ...}
        {"kind": "tool",    "tool": ..., "title": ...}
        {"kind": "text",    "text": ...}
        {"kind": "finish",  "reason": ...}
        {"kind": "unknown"} (unparseable/irrelevant lines never crash us)
    """
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return {"kind": "unknown"}
    session_id = event.get("sessionID")
    if session_id:
        pass  # recorded by the caller from any event
    etype = event.get("type")
    part = event.get("part") or {}
    if etype == "text" and part.get("text") is not None:
        return {"kind": "text", "text": str(part["text"]),
                "session_id": session_id}
    if etype == "tool_use":
        state = part.get("state") or {}
        title = state.get("title") or part.get("tool", "tool")
        return {"kind": "tool", "tool": str(part.get("tool", "?")),
                "title": str(title)[:80], "session_id": session_id}
    if etype == "step_finish":
        return {"kind": "finish", "reason": str(part.get("reason", "?")),
                "session_id": session_id}
    if etype == "step_start":
        return {"kind": "start", "session_id": session_id}
    return {"kind": "unknown"}


class OpencodeExecutor:
    """TaskExecutor that runs tasks through the real opencode CLI."""

    def __init__(
        self,
        model: str | None = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        opencode_bin: str | None = None,
        session_book: SessionBook | None = None,
        session_book_path: Path = SESSION_BOOK_PATH,
    ) -> None:
        self.remote = False
        # model=None -> resolve from this machine's opencode config
        self._model = model or resolve_default_model()
        self._timeout = timeout
        self._bin = opencode_bin  # lazy-resolved on first run
        self._book = session_book or SessionBook(session_book_path)
        self.last_run_s: float | None = None

    async def run(self, bus: InProcBus, task: ComposedTask, project: str) -> None:
        """Execute one task in the project dir; publish progress + result."""
        import time

        if self._bin is None:
            self._bin = resolve_opencode_bin()
        self._book.reload()  # pick up resets done by other processes (D23)
        session_id = self._book.get(project)

        await bus.publish(topics.TASK_PROGRESS, {
            "task_id": task.task_id, "phase": "dispatched",
            "detail": task.prompt[:80],
        })
        if session_id:
            await bus.publish(topics.TASK_PROGRESS, {
                "task_id": task.task_id, "phase": "running",
                "detail": f"opencode (session {session_id[:12]}...)",
            })
        else:
            await bus.publish(topics.TASK_PROGRESS, {
                "task_id": task.task_id, "phase": "running",
                "detail": "opencode (new session)",
            })

        cmd = [self._bin, "run", "--format", "json", "-m", self._model]
        if session_id:
            cmd += ["--session", session_id]
        cmd += [task.prompt]

        logger.info("opencode exec: bin=%s cwd=%s model=%s prompt=%r",
                    self._bin, project, self._model, task.prompt[:50])

        started = time.monotonic()
        texts: list[str] = []
        seen_session: str | None = session_id
        tool_count = 0
        proc: asyncio.subprocess.Process | None = None

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=project,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=4 * 1024 * 1024,  # 4MB line limit (default 64KB
                # is too small for opencode's JSON event lines with
                # large tool outputs -- "Separator is found, but chunk
                # is longer than limit" on Windows StreamReader)
            )
            assert proc.stdout is not None
            try:
                while True:
                    raw = await asyncio.wait_for(proc.stdout.readline(),
                                                 timeout=self._timeout)
                    if not raw:
                        break
                    event = parse_event_line(raw.decode("utf-8", "replace"))
                    sid = event.get("session_id")
                    if sid:
                        seen_session = sid
                    if event["kind"] == "text":
                        texts.append(event["text"])
                    elif event["kind"] == "tool":
                        tool_count += 1
                        await bus.publish(topics.TASK_PROGRESS, {
                            "task_id": task.task_id, "phase": "tool",
                            "detail": event["title"],
                        })
            except TimeoutError:
                proc.kill()
                raise RuntimeError(
                    f"opencode timed out after {self._timeout}s") from None
            except asyncio.CancelledError:
                # bridge cancel: kill the opencode subprocess
                if proc:
                    proc.kill()
                raise
            await proc.wait()
        except Exception as exc:  # noqa: BLE001 -- R.15: failures publish, never crash
            self.last_run_s = time.monotonic() - started
            await bus.publish(topics.TASK_RESULT, {
                "task_id": task.task_id,
                "summary": f"任务失败：{exc}",
                "ok": False,
                "error": str(exc)[:200],
            })
            return

        self.last_run_s = time.monotonic() - started
        if seen_session and seen_session != session_id:
            self._book.set(project, seen_session)  # persist for continuation

        # R.15: success requires exit 0 AND some output -- a silent run is a
        # failure (wrong model / broken install used to exit 0 with no text).
        summary = "".join(texts).strip()
        rc = proc.returncode if proc is not None else -1
        if rc != 0 or not summary:
            error = (f"opencode exited rc={rc}" if rc != 0
                     else "opencode finished without text")
            await bus.publish(topics.TASK_RESULT, {
                "task_id": task.task_id,
                "summary": f"任务失败：{error}",
                "ok": False,
                "error": error,
            })
            return

        await bus.publish(topics.TASK_RESULT, {
            "task_id": task.task_id,
            "summary": summary[:200],
            "ok": True,
            "tools_used": tool_count,
            "elapsed_s": round(self.last_run_s, 1),
        })

    async def reset_session(self, bus: InProcBus, project: str) -> None:
        """Session book resets are owned by the BridgeService book (same file);
        we just reload so the next run starts a fresh session."""
        self._book.reload()
