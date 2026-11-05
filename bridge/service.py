"""BridgeService: the independent bridge process entry (D23, W.9).

Subscribes to TASK_REQUEST on the hub and executes tasks on behalf of the
pipeline process. Why a separate process: when game mode stops the olv
audio host, long-running opencode tasks must survive (D23); the session
book also lives here and survives olv restarts.

Message contract (TASK_REQUEST payloads):
    {"op": "execute", "task": {ComposedTask fields}, "project": str}
    {"op": "reset_session", "project": str}

Run standalone:  python -m bridge --host 127.0.0.1 --port 8765
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from bridge.composer import ComposedTask
from bridge.opencode_exec import OpencodeExecutor  # noqa: F401 (used via --real)
from bridge.server import SESSION_BOOK_PATH, LocalExecutor, SessionBook
from core.bus import topics
from core.bus.hub_client import HubClient

logger = logging.getLogger(__name__)


class BridgeService:
    """Executes tasks requested by remote Bridge/RemoteExecutor instances."""

    def __init__(
        self,
        bus: HubClient,
        session_book_path: Path = SESSION_BOOK_PATH,
        execution_delay: float = 0.01,
        executor: object | None = None,
    ) -> None:
        self._bus = bus
        self._session_book = SessionBook(session_book_path)
        # executor: None = LocalExecutor (sim/tests); pass OpencodeExecutor
        # for real CLI execution (--real flag in main())
        self._executor = executor or LocalExecutor(execution_delay)
        self._running: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        self._bus.subscribe(topics.TASK_REQUEST, self._on_request)
        self._bus.subscribe(topics.TASK_CANCEL, self._on_cancel)
        self._bus.subscribe(topics.TASK_MANUAL, self._on_manual)
        # D51/TASK.SERVE.02: voice approve/deny answers pending permissions
        if getattr(self._executor, "on_approve", None) is not None:
            # Hub subscribers receive (topic, payload); ServeExecutor.on_approve
            # intentionally takes only the payload. Adapt the signature here.
            self._bus.subscribe(topics.TASK_APPROVE, self._on_approve)
            logger.info("TASK_APPROVE wired to executor permission replies")
        logger.info("BridgeService listening on task.request/task.manual")

    async def _on_approve(self, topic: str, payload: dict) -> None:
        handler = getattr(self._executor, "on_approve", None)
        if handler is None:
            return
        await handler(payload)

    async def _on_manual(self, topic: str, payload: dict) -> None:
        """T6.14 (D23): manual opencode-app tasks flow back to the robot.

        The opencode plugin (home machine) publishes task.manual events;
        we relay them as regular task.progress / task.result so every
        existing consumer (LED status area, toast, core playback) lights
        up without knowing the task was manual.
        """
        phase = payload.get("phase")
        summary = payload.get("summary")
        if summary:
            await self._bus.publish(topics.TASK_RESULT, {
                "task_id": payload.get("task_id", "manual"),
                "summary": str(summary),
            })
        elif phase:
            await self._bus.publish(topics.TASK_PROGRESS, {
                "task_id": payload.get("task_id", "manual"),
                "phase": str(phase),
                "detail": payload.get("detail", ""),
            })
        else:
            logger.warning("task.manual without phase/summary: %s", payload)

    async def _on_request(self, topic: str, payload: dict) -> None:
        op = payload.get("op")
        if op == "execute":
            task = ComposedTask(**payload["task"])
            # v0.1 single-flight: one active task at a time. Cancel/amend
            # remain on their own topics; a second dispatch is rejected.
            if self._running:
                active = next(iter(self._running))
                logger.warning("task %s rejected: %s is still running",
                               task.task_id, active)
                await self._bus.publish(topics.TASK_RESULT, {
                    "task_id": task.task_id,
                    "ok": False,
                    "summary": "上一个任务还在跑，请先等它完成或说取消",
                })
                return
            # project resolution chain: payload > cwd; plus existence
            # check (projects.yaml may carry another machine's paths)
            project = payload.get("project") or str(Path.cwd())
            if not Path(project).is_dir():
                logger.warning("project dir %r not found, falling back "
                               "to %s", project, Path.cwd())
                project = str(Path.cwd())
            t = asyncio.get_running_loop().create_task(
                self._run_tracked(task.task_id, task, project))
            self._running[task.task_id] = t
        elif op == "reset_session":
            self._session_book.reset(payload.get("project", "."))
            logger.info("session book reset for %s", payload.get("project"))
        else:
            logger.warning("unknown task.request op: %s", op)

    async def _run_tracked(self, task_id: str,
                            task: ComposedTask, project: str) -> None:
        """Run a task, clean up the tracking dict on completion."""
        try:
            await self._executor.run(self._bus, task, project)
        except asyncio.CancelledError:
            logger.info("task %s cancelled (asyncio)", task_id)
        except Exception:  # noqa: BLE001 -- never crash the bridge
            logger.exception("task %s executor crashed", task_id)
        finally:
            self._running.pop(task_id, None)

    async def _on_cancel(self, topic: str, payload: dict) -> None:
        """Audit/log cancellation; real opencode subprocess kill lands here."""
        task_id = payload.get("task_id")
        running = self._running.pop(task_id, None)
        if running is not None and not running.done():
            running.cancel()
        logger.info("task %s cancelled (%s)", task_id, payload.get("reason"))


async def _serve(host: str, port: int, book: Path, delay: float,
                 audit_path: Path | None, real: bool = False,
                 serve: bool = False) -> None:
    bus = HubClient(host, port)
    await bus.connect()

    executor = None
    if serve:
        # D51: opencode serve API client -- the voice-confirmation loop
        # (permission.asked -> task.permission -> TASK_APPROVE -> reply)
        from bridge.serve_exec import ServeExecutor

        executor = ServeExecutor(session_book_path=book)
        logger.info("using SERVE executor (opencode serve API client)")
    elif real:
        from bridge.opencode_exec import OpencodeExecutor

        executor = OpencodeExecutor(session_book_path=book)
        logger.info("using REAL opencode executor")

    service = BridgeService(bus, session_book_path=book,
                             execution_delay=delay, executor=executor)
    await service.start()

    # TASK.AUDIT.01: runtime decision trail. The bridge is the system-level
    # resident (D23) -- it sees cross-process hub events even when olv is
    # down, making it the right home for the audit sink.
    audit = None
    if audit_path is not None:
        from core.audit import AuditLogger

        audit = AuditLogger(bus, audit_path)
        await audit.start()
        logger.info("audit trail -> %s", audit_path)

    logger.info("bridge process ready (hub %s:%d)", host, port)
    try:
        await asyncio.Event().wait()  # run forever
    finally:
        if audit is not None:
            audit.stop()
        close = getattr(executor, "close", None) if executor else None
        if close is not None:
            await close()  # kill serve processes (D51)
        await bus.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="wafer-bridge process")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--session-book", type=Path, default=SESSION_BOOK_PATH)
    parser.add_argument("--exec-delay", type=float, default=0.01,
                        help="simulated execution seconds (0 for tests)")
    parser.add_argument("--real", action="store_true",
                        help="use the real opencode CLI executor "
                             "(default: simulated LocalExecutor)")
    parser.add_argument("--serve", action="store_true",
                        help="use the opencode serve API executor with the "
                             "voice-confirmation permission loop (D51)")
    parser.add_argument("--audit", type=Path,
                        default=Path("logs/audit.jsonl"),
                        help="runtime audit trail ('' / none to disable)")
    args = parser.parse_args(argv)

    audit_path = args.audit if str(args.audit).strip().lower() not in (
        "", "none", "off") else None
    asyncio.run(_serve(args.host, args.port, args.session_book,
                       args.exec_delay, audit_path, real=args.real,
                       serve=args.serve))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    raise SystemExit(main())
