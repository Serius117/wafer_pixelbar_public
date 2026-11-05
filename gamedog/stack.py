"""Stack controller: stop/start the local model stack (T5.6, D26).

When a game starts, the local stack (ASR/LLM/TTS, ~10.5 GB VRAM) must be
stopped to free VRAM for the game. Design decision D26: gamedog does NOT
psutil-kill processes itself; it runs the configured command lists via
subprocess. Reasons:
  - commands are reviewable in config (no hidden kill list)
  - graceful shutdown where the command provides it
  - trivially testable with a spy

config/gameapps.yaml:
    stack:
      stop_commands: []   # e.g. ["scripts/stop_stack.ps1"] -- filled at home
      start_commands: []
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Sequence
from pathlib import Path

logger = logging.getLogger(__name__)


class StackController:
    """Interface: stop/start the local model stack."""

    def stop(self) -> None: ...

    def start(self) -> None: ...


class NoopStackController(StackController):
    """Does nothing (CI, or when the stack is managed externally)."""


class CommandStackController(StackController):
    """Runs configured command lists via subprocess (blocking, logged)."""

    def __init__(
        self,
        stop_commands: Sequence[str],
        start_commands: Sequence[str],
        cwd: Path | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._stop_commands = list(stop_commands)
        self._start_commands = list(start_commands)
        self._cwd = cwd
        self._timeout = timeout

    def _run(self, commands: Sequence[str], label: str) -> None:
        for cmd in commands:
            logger.info("stack %s: %s", label, cmd)
            result = subprocess.run(  # noqa: S603 -- commands come from reviewed config
                cmd,
                shell=True,
                cwd=str(self._cwd) if self._cwd else None,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
            if result.returncode != 0:
                logger.error(
                    "stack %s command failed (rc=%d): %s\n%s",
                    label, result.returncode, cmd, result.stderr.strip(),
                )

    def stop(self) -> None:
        self._run(self._stop_commands, "stop")

    def start(self) -> None:
        self._run(self._start_commands, "start")
