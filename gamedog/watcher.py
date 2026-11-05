"""Process watcher: detect whitelist game processes entering/leaving (T5.6).

The process-list source is a callable (`ProcessProvider`) so tests inject a
scripted sequence while production uses psutil (lazy import, CI has no psutil).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

ProcessProvider = Callable[[], set[str]]


def psutil_process_provider() -> set[str]:
    """Real provider: names of all running processes, lowercased (lazy psutil)."""
    import psutil  # lazy: home machine only

    names: set[str] = set()
    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info.get("name")
            if name:
                names.add(name.lower())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return names


class ProcessWatcher:
    """Poll a process provider against a whitelist, emit enter/exit events."""

    def __init__(self, whitelist: Iterable[str], provider: ProcessProvider) -> None:
        self._whitelist = {name.lower() for name in whitelist}
        self._provider = provider
        self._running: set[str] = set()

    @property
    def whitelist(self) -> set[str]:
        return set(self._whitelist)

    @property
    def running(self) -> set[str]:
        return set(self._running)

    def poll(self) -> list[tuple[str, str]]:
        """One poll: returns [("enter"|"exit", process_name), ...]."""
        current = self._provider() & self._whitelist
        entered = sorted(current - self._running)
        exited = sorted(self._running - current)
        self._running = current
        return [("enter", name) for name in entered] + [("exit", name) for name in exited]
