"""Runtime audit logger (TASK.AUDIT.01): decision trail to logs/audit.jsonl.

Closes the gap found in the 2026-10-23 doc audit: route/gate/tool decisions
had no runtime persistence -- the "mis-response = 0" red line was only
CI-guarded, with no production trail to investigate from. This logger
subscribes to the decision-carrying topics and appends one JSON line per
event (schema: ts + topic + payload as published -- the richer fields of
design doc 5.2, e.g. prompt_sha256, land when their publishers add them).

Wired into the bridge process (system-level resident per D23) so it
captures cross-process events from the hub; usable with InProcBus in tests.
Rotation/cleanup is a future cleanup script (requirement 12#7), not here.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "logs" / "audit.jsonl"

AUDIT_TOPICS = (
    "gate.decision",
    "wake.word",
    "llm.emotion",
    "task.progress",
    "task.result",
    "task.permission",
    "task.approve",
    "task.cancel",
    "mode.switch",
)


class AuditLogger:
    """Appends {ts, topic, **payload} per event; never raises into the bus."""

    def __init__(self, bus: Any, path: Path | str = DEFAULT_PATH) -> None:
        self._bus = bus
        self._path = Path(path)
        self._lock = threading.Lock()
        self._fh = None

    async def start(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a", encoding="utf-8")
        for topic in AUDIT_TOPICS:
            self._bus.subscribe(topic, self._on_event)

    def stop(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def _on_event(self, topic: str, payload: dict) -> None:
        if self._fh is None:
            return
        record = {"ts": round(time.time(), 3), "topic": topic, **payload}
        try:
            with self._lock:
                self._fh.write(
                    json.dumps(record, ensure_ascii=False) + "\n")
                self._fh.flush()
        except Exception:  # noqa: BLE001 -- audit must never break the bus
            pass
