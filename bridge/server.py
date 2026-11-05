"""Bridge: sole opencode channel + session book + result relay (T4.2, T4.7).

This is the ONLY module in the system that talks to opencode (design doc §4.6).
It maintains a session book (project -> session_id, persisted to disk, D23)
and relays task lifecycle events to the hub.

Grace window (D15): submit() composes and holds the task; arm() starts the
grace countdown (default 1.5s, configurable, 0 = dispatch on next loop tick);
amend()/cancel() during the window act BEFORE dispatch (zero cost). After the
window the task is dispatched; later amendments go through cancel + re-dispatch.

v1: in-process implementation (called by pipeline). The MCP wrapper for
OLV integration will be added when the P2 patch is formalized.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from bridge.composer import ComposedTask, TaskComposer
from core.bus import topics
from core.bus.hub import InProcBus

logger = logging.getLogger(__name__)

# Session book persistence path (D23)
SESSION_BOOK_PATH = Path("data/session_book.json")

DEFAULT_GRACE_SECONDS = 1.5


class TaskExecutor(Protocol):
    """Execution backend for dispatched tasks (W.9).

    LocalExecutor  -- in-process simulation/execution (tests, single-process)
    RemoteExecutor -- publishes TASK_REQUEST events consumed by the
                      independent bridge process (D23)
    """

    remote: bool

    async def run(self, bus: InProcBus, task: ComposedTask, project: str) -> None: ...

    async def reset_session(self, bus: InProcBus, project: str) -> None: ...


class LocalExecutor:
    """In-process execution: progress events + simulated work + result."""

    def __init__(self, execution_delay: float = 0.01) -> None:
        self.remote = False
        self._execution_delay = execution_delay

    async def run(self, bus: InProcBus, task: ComposedTask, project: str) -> None:
        await bus.publish(topics.TASK_PROGRESS, {
            "task_id": task.task_id,
            "phase": "dispatched",
            "detail": task.prompt[:80],
        })
        await bus.publish(topics.TASK_PROGRESS, {
            "task_id": task.task_id,
            "phase": "running",
            "detail": "opencode is working...",
        })

        # TODO: Real opencode execution:
        #   cmd = ["opencode", "run", "--session", session_id, task.prompt]
        #   result = subprocess.run(cmd, cwd=project, capture_output=True, text=True, timeout=300)
        #   session_id = extract_session_id(result)

        await asyncio.sleep(self._execution_delay)
        await bus.publish(topics.TASK_RESULT, {
            "task_id": task.task_id,
            "summary": f"任务完成：{task.original_text[:40]}",
            "ok": True,
        })

    async def reset_session(self, bus: InProcBus, project: str) -> None:
        """No-op: session book lives inside the Bridge in local mode."""


class RemoteExecutor:
    """Cross-process execution: send TASK_REQUEST to the bridge process."""

    def __init__(self) -> None:
        self.remote = True

    async def run(self, bus: InProcBus, task: ComposedTask, project: str) -> None:
        await bus.publish(topics.TASK_REQUEST, {
            "op": "execute",
            "task": asdict(task),
            "project": project,
        })

    async def reset_session(self, bus: InProcBus, project: str) -> None:
        await bus.publish(topics.TASK_REQUEST, {
            "op": "reset_session",
            "project": project,
        })


class SessionBook:
    """Maintains project -> session_id mapping, persisted to disk (D23)."""

    def __init__(self, path: Path = SESSION_BOOK_PATH) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._sessions = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                logger.warning("Session book corrupted, starting fresh")
                self._sessions = {}

    def reload(self) -> None:
        """Re-read the persisted book (cross-instance sync: another process
        may have reset a session on disk since we last loaded)."""
        self._load()

    def _save(self) -> None:
        self._path.write_text(
            json.dumps(self._sessions, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def get(self, project: str) -> str | None:
        return self._sessions.get(project)

    def set(self, project: str, session_id: str) -> None:
        self._sessions[project] = session_id
        self._save()

    def reset(self, project: str) -> None:
        self._sessions.pop(project, None)
        self._save()

    @property
    def projects(self) -> list[str]:
        return list(self._sessions.keys())


class Bridge:
    """The sole channel to opencode.

    Responsibilities:
      1. Compose and dispatch tasks (via opencode CLI)
      2. Maintain session book (per-project working session)
      3. Relay progress/permission/result events to hub
      4. Grace window for cheap amendments (D15)
      5. Summarize results (v1: template, future: LLM)
    """

    def __init__(
        self,
        bus: InProcBus,
        default_project: str = ".",
        session_book_path: Path = SESSION_BOOK_PATH,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        execution_delay: float = 0.01,
        executor: TaskExecutor | None = None,
    ) -> None:
        self._bus = bus
        self._default_project = default_project
        self._session_book = SessionBook(session_book_path)
        self._composer = TaskComposer()
        self._grace = grace_seconds
        self._executor: TaskExecutor = executor or LocalExecutor(execution_delay)
        self._active_task: ComposedTask | None = None
        self._dispatched_ids: set[str] = set()
        self._flush_task: asyncio.Task | None = None
        self._pending_project: str = default_project
        # D33: subscribe to project switches from the bus
        bus.subscribe(topics.PROJECT_SWITCH, self._on_project_switch)

    # --- introspection -------------------------------------------------

    @property
    def session_book(self) -> SessionBook:
        return self._session_book

    @property
    def active_task(self) -> ComposedTask | None:
        return self._active_task

    @property
    def grace_seconds(self) -> float:
        return self._grace

    def has_work(self) -> bool:
        """A task is held (pending or dispatched, not yet completed)."""
        return self._active_task is not None

    def is_dispatched(self) -> bool:
        """The held task has already been sent to opencode."""
        return (
            self._active_task is not None
            and self._active_task.task_id in self._dispatched_ids
        )

    # --- task lifecycle --------------------------------------------------

    async def dispatch(
        self,
        transcript: str,
        speaker_id: str,
        project: str | None = None,
        recent_context: list[str] | None = None,
    ) -> ComposedTask:
        """Compose and dispatch immediately (bypasses the grace window).

        Kept for callers that already confirmed (tests, manual API).
        The pipeline uses submit() + arm() instead.
        """
        task = self._composer.compose(transcript, speaker_id, recent_context)
        self._active_task = task
        self._pending_project = project or self._default_project
        await self._execute(task, self._pending_project)
        return task

    async def submit(
        self,
        transcript: str,
        speaker_id: str,
        recent_context: list[str] | None = None,
        project: str | None = None,
    ) -> ComposedTask:
        """Compose and HOLD the task; amend/cancel apply before arm() fires.

        Does not start the grace countdown -- the caller (pipeline) speaks
        the TTS confirmation first, then calls arm() so the window starts
        AFTER the confirmation finishes (D15: "复述播完 + 1.5s").
        """
        task = self._composer.compose(transcript, speaker_id, recent_context)
        self._active_task = task
        self._pending_project = project or self._default_project
        self._cancel_flush()
        return task

    async def arm(self) -> None:
        """Start the grace countdown; flush() fires when it expires."""
        self._cancel_flush()
        self._flush_task = asyncio.get_running_loop().create_task(
            self._delayed_flush()
        )

    async def flush(self) -> None:
        """Dispatch the held task now (idempotent: no-op if none/already sent)."""
        task = self._active_task
        if task is None or task.task_id in self._dispatched_ids:
            return
        await self._execute(task, self._pending_project)

    async def amend(self, correction_text: str) -> ComposedTask | None:
        """Amend the held task with user correction (D15).

        Works both before dispatch (zero cost: cancel flush, re-compose,
        re-arm) and after (cancel the running task, re-dispatch amended).
        """
        if not self._active_task:
            return None

        original = self._active_task  # capture BEFORE cancel clears it
        was_dispatched = original.task_id in self._dispatched_ids

        await self.cancel(reason="amendment")

        amended = self._composer.amend(original, correction_text)
        self._active_task = amended
        self._pending_project = self._default_project

        if was_dispatched:
            # task already running: re-dispatch in the background (mirrors
            # flush semantics so the pipeline turn returns immediately)
            self._flush_task = asyncio.get_running_loop().create_task(
                self._execute(amended, self._pending_project)
            )
        else:
            # still in grace: fresh window, caller re-arms after TTS
            logger.info("task %s amended before dispatch (zero cost)", amended.task_id)
        return amended

    async def cancel(self, reason: str = "user_request") -> None:
        """Cancel the held task and any pending grace flush."""
        self._cancel_flush()
        if self._active_task:
            await self._bus.publish(topics.TASK_CANCEL, {
                "task_id": self._active_task.task_id,
                "reason": reason,
            })
            self._active_task = None

    async def reset_session(self, project: str | None = None) -> None:
        """Reset the working session for a project (D16/D18)."""
        proj = project or self._default_project
        if self._executor.remote:
            await self._executor.reset_session(self._bus, proj)
        else:
            self._session_book.reset(proj)
        logger.info("Session book reset for %s", proj)

    async def approve(self, allow: bool) -> None:
        """Handle permission approval from user (voice/hotkey)."""
        if self._active_task:
            await self._bus.publish(topics.TASK_APPROVE, {
                "task_id": self._active_task.task_id,
                "allow": allow,
            })

    async def close(self) -> None:
        """Cancel any pending grace flush (shutdown/tests)."""
        self._cancel_flush()

    async def _on_project_switch(self, topic: str, payload: dict) -> None:
        """D33: console/pipeline switched the current project."""
        path = payload.get("path")
        if path:
            self._default_project = str(path)
            self._pending_project = str(path)
            logger.info("project switched to %s (%s)",
                        payload.get("alias", "?"), path)

    # --- internals -------------------------------------------------------

    def _cancel_flush(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
        self._flush_task = None

    async def _delayed_flush(self) -> None:
        try:
            await asyncio.sleep(self._grace)
            await self.flush()
        except asyncio.CancelledError:
            pass

    async def _execute(self, task: ComposedTask, project: str) -> None:
        """Execute (or hand off) a dispatched task via the executor backend."""
        self._dispatched_ids.add(task.task_id)
        await self._executor.run(self._bus, task, project)
        self._active_task = None
