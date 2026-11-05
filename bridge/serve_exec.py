"""ServeExecutor: opencode serve API client (D51, TASK.SERVE.01).

Replaces the `opencode run` subprocess mode: run auto-approves every
permission (verified experimentally), so the voice-confirmation loop is
impossible there. In serve mode the SSE stream surfaces `permission.asked`
events and we answer via POST /session/{sid}/permissions/{per_id}.

Architecture (one serve instance per project, lazily started):
    dispatch  POST /session/{sid}/prompt_async
    events    GET /event (SSE) -> text parts / permission.asked / session.idle
    approve   TASK_APPROVE on the hub -> POST .../permissions/{per_id}
    cancel    POST /session/{sid}/abort

Permission config lives in the PROJECT ROOT opencode.json
({"permission": {"edit": "ask", "bash": "ask"}}) -- .opencode/settings.json
is NOT loaded (verified the hard way, DEVLOG 2026-10-27).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import zlib
from pathlib import Path

import httpx

from bridge.composer import ComposedTask
from bridge.opencode_exec import (
    SESSION_BOOK_PATH,
    resolve_default_model,
    resolve_opencode_bin,
)
from bridge.server import SessionBook
from core.bus import topics

logger = logging.getLogger(__name__)

SERVE_PORT_BASE = 14100
SERVE_PORT_SPAN = 500
HEALTH_TIMEOUT_S = 30.0
TASK_TIMEOUT_S = 600.0
REPLY_RESPONSES = {"allow": "once", "deny": "reject"}  # hub -> opencode vocab


def _project_port(project: str, base: int = SERVE_PORT_BASE) -> int:
    """Deterministic port per project so restarts reuse the same serve.

    zlib.crc32, NOT hash(): Python randomizes str hashes per process
    (PYTHONHASHSEED) so hash() would pick a new port after every bridge
    restart and the serve-reuse chain would break.
    """
    return base + (zlib.crc32(project.encode("utf-8")) % SERVE_PORT_SPAN)


def _auth_headers() -> dict[str, str]:
    user = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
    pw = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
    if not pw:
        return {}
    import base64

    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _question_from_permission(props: dict) -> str:
    """Human-readable question for TTS from a permission.asked payload."""
    perm = props.get("permission", "操作")
    patterns = props.get("patterns") or []
    target = Path(patterns[0]).name if patterns else "文件"
    meta = props.get("metadata") or {}
    diff = meta.get("diff") or ""
    lines = diff.splitlines() if diff else []
    added = sum(1 for ln in lines
                if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines
                  if ln.startswith("-") and not ln.startswith("---"))
    detail = f"，加 {added} 行" if added > 0 else ""
    detail += f"，删 {removed} 行" if removed > 0 else ""
    return f"想{perm} {target}{detail}，批准吗"


class _ServeInstance:
    """One opencode serve process bound to a project directory."""

    def __init__(self, project: str, port: int,
                 bin_path: str, model: str) -> None:
        self.project = project
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self._bin = bin_path
        self._model = model
        self.proc: asyncio.subprocess.Process | None = None
        self.session_id: str | None = None
        self.sse_events: list[dict] = []
        self._sse_task: asyncio.Task | None = None
        self._sse_waiters: list[asyncio.Event] = []

    # --- lifecycle --------------------------------------------------------

    async def ensure(self, client: httpx.AsyncClient) -> None:
        """Start the serve process if this port has no healthy server."""
        try:
            r = await client.get(f"{self.base}/global/health")
            if r.status_code == 200:
                logger.info("serve reused on :%d for %s",
                            self.port, self.project)
                return
        except httpx.HTTPError:
            pass
        self.proc = await asyncio.create_subprocess_exec(
            self._bin, "serve", "--port", str(self.port),
            cwd=self.project,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                r = await client.get(f"{self.base}/global/health")
                if r.status_code == 200:
                    logger.info("serve started pid=%s :%d for %s",
                                self.proc.pid, self.port, self.project)
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
        raise RuntimeError(
            f"opencode serve failed to become healthy on :{self.port}")

    def stop(self) -> None:
        if self._sse_task is not None:
            self._sse_task.cancel()
            self._sse_task = None
        if self.proc is not None:
            self.proc.kill()
            self.proc = None

    # --- SSE event pump ---------------------------------------------------

    async def start_sse(self, client: httpx.AsyncClient) -> None:
        if self._sse_task is not None:
            return

        async def _pump() -> None:
            try:
                async with client.stream(
                        "GET", f"{self.base}/event",
                        timeout=httpx.Timeout(None, connect=10)) as resp:
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            ev = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        self.sse_events.append(ev)
                        for waiter in self._sse_waiters:
                            waiter.set()
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 -- pump must survive
                logger.exception("SSE pump died for :%d", self.port)

        self._sse_task = asyncio.get_running_loop().create_task(_pump())

    async def wait_event(self, predicate, timeout: float) -> dict | None:
        """Wait until an SSE event matching predicate arrives (or None)."""
        for ev in self.sse_events:  # already arrived?
            if predicate(ev):
                return ev
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            waiter = asyncio.Event()
            self._sse_waiters.append(waiter)
            try:
                await asyncio.wait_for(waiter.wait(), timeout=1.0)
            except TimeoutError:
                pass
            finally:
                self._sse_waiters.remove(waiter)
            for ev in self.sse_events:
                if predicate(ev):
                    return ev
        return None


class ServeExecutor:
    """TaskExecutor that talks to per-project opencode serve instances."""

    def __init__(
        self,
        model: str | None = None,
        timeout: float = TASK_TIMEOUT_S,
        opencode_bin: str | None = None,
        session_book: SessionBook | None = None,
        session_book_path: Path = SESSION_BOOK_PATH,
        port_base: int = SERVE_PORT_BASE,
    ) -> None:
        self.remote = False
        self._model = model or resolve_default_model()
        self._timeout = timeout
        self._bin = opencode_bin
        self._book = session_book or SessionBook(session_book_path)
        self._port_base = port_base
        self._instances: dict[str, _ServeInstance] = {}
        self._client: httpx.AsyncClient | None = None
        self._pending: dict[str, dict] = {}  # session_id -> permission.asked
        self.last_run_s: float | None = None

    # --- plumbing ---------------------------------------------------------

    def _client_(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=_auth_headers(), timeout=15)
        return self._client

    def _instance(self, project: str) -> _ServeInstance:
        if self._bin is None:
            self._bin = resolve_opencode_bin()
        if project not in self._instances:
            port = _project_port(project, self._port_base)
            self._instances[project] = _ServeInstance(
                project, port, self._bin, self._model)
        return self._instances[project]

    async def close(self) -> None:
        for inst in self._instances.values():
            inst.stop()
        self._instances.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- permission answering (TASK.SERVE.02 bridge side) -----------------

    async def on_approve(self, payload: dict) -> None:
        """TASK_APPROVE {task_id, allow} -> reply to the pending request."""
        allow = bool(payload.get("allow"))
        for sid, perm in list(self._pending.items()):
            response = REPLY_RESPONSES["allow" if allow else "deny"]
            try:
                r = await self._client_().post(
                    f"{self._instance(perm['project']).base}"
                    f"/session/{sid}/permissions/{perm['id']}",
                    json={"response": response})
                logger.info("permission %s replied %s -> %s",
                            perm["id"][:16], response, r.status_code)
            except Exception:  # noqa: BLE001 -- never crash the bridge
                logger.exception("permission reply failed")
                continue
            self._pending.pop(sid, None)

    # --- TaskExecutor interface --------------------------------------------

    async def run(self, bus, task: ComposedTask, project: str) -> None:
        """Execute one task via serve; publish progress/result/permission."""
        started = time.monotonic()
        client = self._client_()
        inst = self._instance(project)
        self._book.reload()
        session_id = self._book.get(project)

        await bus.publish(topics.TASK_PROGRESS, {
            "task_id": task.task_id, "phase": "dispatched",
            "detail": task.prompt[:80],
        })
        try:
            await inst.ensure(client)
            await inst.start_sse(client)
            if session_id is None:
                r = await client.post(f"{inst.base}/session",
                                      json={"title": "wafer"})
                session_id = r.json()["id"]
                self._book.set(project, session_id)
            await bus.publish(topics.TASK_PROGRESS, {
                "task_id": task.task_id, "phase": "running",
                "detail": "opencode 执行中",
            })

            event_count = len(inst.sse_events)
            r = await client.post(
                f"{inst.base}/session/{session_id}/prompt_async",
                json={"parts": [{"type": "text", "text": task.prompt}],
                      "modelID": self._model})
            if r.status_code not in (200, 204):
                raise RuntimeError(f"prompt_async {r.status_code}: "
                                   f"{r.text[:120]}")

            # wait for the session to go idle (task finished), surfacing
            # permission requests while we wait
            texts: list[str] = []
            idle = False
            deadline = time.monotonic() + self._timeout
            while time.monotonic() < deadline and not idle:
                for ev in inst.sse_events[event_count:]:
                    props = ev.get("properties") or {}
                    etype = ev.get("type")
                    if etype == "permission.asked":
                        if session_id == props.get("sessionID"):
                            self._pending[session_id] = {
                                "id": props.get("id"), "project": project,
                                "perm": props.get("permission"),
                            }
                            await bus.publish(topics.TASK_PERMISSION, {
                                "task_id": task.task_id,
                                "kind": props.get("permission"),
                                "target": (props.get("patterns") or [""])[0],
                                "question": _question_from_permission(props),
                            })
                            logger.info("permission.asked: %s",
                                        props.get("permission"))
                    elif etype == "message.part.updated":
                        part = props.get("part") or props
                        role = str(part.get("role")
                                   or props.get("role") or "").lower()
                        # Never let the user prompt become the result text.
                        if role in ("user", "human"):
                            continue
                        if part.get("type") == "text" and part.get("text"):
                            texts.append(str(part["text"]))
                    elif etype == "session.idle":
                        if session_id == props.get("sessionID"):
                            idle = True
                event_count = len(inst.sse_events)
                if not idle:
                    await asyncio.sleep(0.5)

            if not idle:
                await client.post(f"{inst.base}/session/{session_id}/abort")
                raise RuntimeError(
                    f"opencode timed out after {self._timeout}s")

        except asyncio.CancelledError:
            if session_id:
                try:
                    await client.post(
                        f"{inst.base}/session/{session_id}/abort")
                except Exception:  # noqa: BLE001
                    pass
            raise
        except Exception as exc:  # noqa: BLE001 -- R.15 publish, never crash
            self.last_run_s = time.monotonic() - started
            await bus.publish(topics.TASK_RESULT, {
                "task_id": task.task_id, "prompt": task.prompt,
                "summary": f"任务失败：{exc}",
                "ok": False, "error": str(exc)[:200],
            })
            return

        self.last_run_s = time.monotonic() - started
        summary = "".join(texts).strip()
        if not summary:
            summary = "（任务结束，无文本输出）"
        await bus.publish(topics.TASK_RESULT, {
            "task_id": task.task_id, "prompt": task.prompt,
            "summary": summary[:200], "ok": True,
        })
