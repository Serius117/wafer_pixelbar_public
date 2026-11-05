"""wafer-ctl: global hotkeys + Windows notifications (T4.8, D22/D23).

Runs as an independent process (design doc §2.1). Provides:
  - Wake/sleep hotkey (D22: session toggle)
  - Gate toggle hotkey (temporary disable for cold/voice change)
  - Task approve/deny hotkeys (for game silent mode)
  - Mode toggle hotkey (future, for game mode)
  - Windows toast notifications via winotify
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from core.bus import topics
from core.bus.hub import InProcBus
from core.naming import DEFAULT_ROBOT_NAME

logger = logging.getLogger(__name__)

# Default hotkey bindings (configurable)
DEFAULT_HOTKEYS = {
    "wake": "ctrl+alt+w",       # Toggle wake/sleep (D22)
    "gate_toggle": "ctrl+alt+g",  # Temporary gate disable (D7)
    "approve": "ctrl+alt+y",     # Approve permission request
    "deny": "ctrl+alt+n",        # Deny permission request
    "mode_toggle": "ctrl+alt+m",  # Toggle normal/game mode (future)
}


class CtlService:
    """Hotkey listener + notification dispatcher.

    In production this runs as a separate process with pynput for global
    hotkeys and winotify for Windows toasts. In tests, hotkeys can be
    simulated by calling `fire_hotkey(action)` directly.
    """

    def __init__(
        self,
        bus: InProcBus,
        hotkeys: dict[str, str] | None = None,
        robot_name: str = DEFAULT_ROBOT_NAME,
    ) -> None:
        self._bus = bus
        self._hotkeys = hotkeys or DEFAULT_HOTKEYS
        self._robot_name = robot_name
        self._listeners: dict[str, list[Callable]] = {}
        self._running = False
        self.toast_log: list[dict] = []  # shown toasts (test/introspection)

    @property
    def hotkeys(self) -> dict[str, str]:
        return dict(self._hotkeys)

    async def start(self) -> None:
        """Start listening for hotkeys (production: pynput hook)."""
        self._running = True
        logger.info("CtlService started, hotkeys: %s", self._hotkeys)

        # Production code would use:
        # from pynput import keyboard
        # ... register global hotkey callbacks

    async def stop(self) -> None:
        """Stop listening."""
        self._running = False
        logger.info("CtlService stopped")

    async def fire_hotkey(self, action: str) -> None:
        """Fire a hotkey action (called by pynput in production, by tests directly).

        Publishes ctl.hotkey event to the bus.
        """
        logger.info("Hotkey fired: %s", action)
        await self._bus.publish(topics.CTL_HOTKEY, {"action": action})

    async def notify(self, level: str, text: str) -> None:
        """Send a Windows toast notification.

        In production: uses winotify. In tests: logs to console.
        """
        logger.info("Notify [%s]: %s", level, text)

        # Also publish to bus for console display
        await self._bus.publish(topics.SYSTEM_NOTIFY, {
            "level": level,
            "text": text,
        })

    def _show_toast(self, level: str, text: str) -> None:
        """Display a toast WITHOUT republishing (anti-loop, W.8 fix).

        Called for bus-originated notifications. `notify()` (which does
        publish) is for notifications the ctl process itself originates --
        subscribing to SYSTEM_NOTIFY and calling notify() in the handler
        would loop forever on the bus.
        """
        logger.info("Toast [%s]: %s", level, text)
        # Production code:
        # from winotify import Notification
        # toast = Notification(app_id="wafer-pixel", title=robot_name, msg=text)
        # toast.show()
        self.toast_log.append({"level": level, "text": text})

    async def handle_bus_events(self) -> None:
        """Subscribe to bus events and dispatch notifications.

        This is the main event loop for the ctl process.
        """
        async def on_notify(topic: str, payload: dict) -> None:
            self._show_toast(payload.get("level", "info"), payload.get("text", ""))

        async def on_permission(topic: str, payload: dict) -> None:
            desc = payload.get("detail", payload.get("kind", "unknown"))
            self._show_toast("warn", f"{self._robot_name}在等你确认：{desc}")

        async def on_task_result(topic: str, payload: dict) -> None:
            # T4.7/GAME-10: finished tasks toast (the silent-mode reply channel)
            summary = payload.get("summary", "")
            if summary:
                self._show_toast("info", str(summary))

        self._bus.subscribe(topics.SYSTEM_NOTIFY, on_notify)
        self._bus.subscribe(topics.TASK_PERMISSION, on_permission)
        self._bus.subscribe(topics.TASK_RESULT, on_task_result)
