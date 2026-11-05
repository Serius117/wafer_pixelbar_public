"""Audit logger tests (TEST.AUDIT.01-02, TASK.AUDIT.01)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.audit import AUDIT_TOPICS, AuditLogger  # noqa: E402
from core.bus.hub import InProcBus  # noqa: E402


class TestAuditLogger:
    async def test_decision_events_persisted(self, tmp_path) -> None:
        """TEST.AUDIT.01: gate/task events land as one JSON line each,
        with ts + topic + the full payload."""
        path = tmp_path / "audit.jsonl"
        bus = InProcBus()
        logger = AuditLogger(bus, path)
        await logger.start()
        try:
            await bus.publish("gate.decision",
                              {"verdict": "pass", "speaker_id": "master",
                               "score": 0.78})
            await bus.publish("task.result",
                              {"task_id": "t1", "summary": "done", "ok": True})
        finally:
            logger.stop()

        lines = [json.loads(x) for x in
                 path.read_text(encoding="utf-8").splitlines()]
        assert len(lines) == 2
        by_topic = {r["topic"]: r for r in lines}
        assert by_topic["gate.decision"]["verdict"] == "pass"
        assert by_topic["gate.decision"]["speaker_id"] == "master"
        assert by_topic["task.result"]["ok"] is True
        assert all("ts" in r for r in lines)

    async def test_non_audit_topics_ignored(self, tmp_path) -> None:
        """TEST.AUDIT.02: high-frequency / non-decision topics (e.g.
        tts.amplitude) never reach the audit trail."""
        path = tmp_path / "audit.jsonl"
        bus = InProcBus()
        logger = AuditLogger(bus, path)
        await logger.start()
        try:
            await bus.publish("tts.amplitude", {"rms": 0.5})
            await bus.publish("session.state", {"state": "listening"})
            await bus.publish("task.cancel",
                              {"task_id": "t2", "reason": "user_request"})
        finally:
            logger.stop()

        topics = [json.loads(x)["topic"] for x in
                  path.read_text(encoding="utf-8").splitlines()]
        assert topics == ["task.cancel"]

    def test_topic_registry_covers_decisions(self) -> None:
        """The audited set must include every decision-carrying topic."""
        required = {"gate.decision", "llm.emotion", "task.result",
                    "task.approve", "task.cancel", "mode.switch"}
        assert required <= set(AUDIT_TOPICS)
        assert "tts.amplitude" not in AUDIT_TOPICS  # bandwidth discipline
