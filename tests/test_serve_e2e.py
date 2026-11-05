"""TEST.SERVE.09: REAL opencode serve permission E2E (D51, TASK.SERVE.03).

Full chain against a real `opencode serve` with edit=ask configured:
    dispatch -> permission.asked (SSE) -> task.permission (hub)
    -> on_approve -> POST reply(once) -> task continues
    -> out.txt created -> task.result(ok=True)

Skips on machines without the opencode binary or the serve password
(CI keeps a stub-only policy; this runs on dev + home).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.composer import ComposedTask  # noqa: E402
from bridge.opencode_exec import resolve_opencode_bin  # noqa: E402
from core.bus.hub import InProcBus  # noqa: E402

try:
    _BIN = resolve_opencode_bin()
except Exception:  # noqa: BLE001 -- not installed
    _BIN = None

pytestmark = pytest.mark.skipif(
    _BIN is None or not os.environ.get("OPENCODE_SERVER_PASSWORD"),
    reason="needs opencode binary + OPENCODE_SERVER_PASSWORD (dev/home)")


@pytest.fixture
def ask_project(tmp_path):
    """Temp project with edit=ask permission config at the PROJECT ROOT."""
    (tmp_path / "opencode.json").write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "permission": {"edit": "ask", "bash": "ask"},
    }, ensure_ascii=False), encoding="utf-8")
    return str(tmp_path)


async def test_09_real_serve_permission_loop(ask_project):
    """TEST.SERVE.09: dispatch -> permission.asked -> approve -> result."""
    from bridge.serve_exec import ServeExecutor

    bus = InProcBus()
    seen: list[tuple[str, dict]] = []
    bus.subscribe("task.permission",
                  lambda t, p: seen.append((t, dict(p))))
    bus.subscribe("task.result", lambda t, p: seen.append((t, dict(p))))

    ex = ServeExecutor(timeout=180)
    task = ComposedTask(task_id="t-e2e", prompt="请创建 out.txt，内容写 done",
                        original_text="请创建 out.txt", speaker_id="master")
    try:
        run_task = asyncio.get_running_loop().create_task(
            ex.run(bus, task, ask_project))

        # wait for the REAL permission.asked to surface as task.permission
        for _ in range(240):  # up to 120s (cold serve + model round trip)
            if any(t == "task.permission" for t, _ in seen):
                break
            await asyncio.sleep(0.5)
        perms = [p for t, p in seen if t == "task.permission"]
        assert perms, "real serve must surface permission.asked as " \
                      "task.permission (edit=ask)"
        assert perms[0]["kind"] == "edit"
        assert "out.txt" in perms[0]["question"]

        # voice-approve: the hub TASK_APPROVE equivalent
        await ex.on_approve({"task_id": "t-e2e", "allow": True})

        await asyncio.wait_for(run_task, timeout=180)
        results = [p for t, p in seen if t == "task.result"]
        assert results, "task.result must arrive"
        assert results[0]["ok"] is True
        assert (Path(ask_project) / "out.txt").exists(), \
            "approved edit must have executed"
    finally:
        await ex.close()
