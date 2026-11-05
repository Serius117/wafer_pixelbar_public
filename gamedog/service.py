"""GameDog service: whitelist watcher -> mode switch -> bus events (T5.6).

Wiring (design doc §6):
  watcher (psutil poll) -> ModeManager -> bus publishes:
    - mode.switch  {mode, tier, source, game}   -- orchestrator/UI react here
    - system.notify {level, text}               -- wafer-ctl shows a toast
    - render.command {cmd: "scene", scene: ...} -- LED HUD in game mode
  and StackController.stop()/start() around the transition (D26).

The service never talks to opencode and never touches the HID sink; it only
publishes events (single-writer and sole-channel rules intact).
"""

from __future__ import annotations

import asyncio
import logging

from core.bus import topics
from core.bus.hub import InProcBus
from core.naming import DEFAULT_ROBOT_NAME
from gamedog.mode import ModeManager, ModeSwitch
from gamedog.providers import ChatProvider
from gamedog.stack import NoopStackController, StackController
from gamedog.watcher import ProcessWatcher

logger = logging.getLogger(__name__)


class GameDog:
    """Polls the watcher, drives mode transitions, publishes bus events.

    Exit handling (GAME-04/05):
      - multi-game: the mode only returns to normal when the watcher reports
        NO whitelisted game running (a second game keeps silent mode alive)
      - debounce: an exit must hold for exit_debounce_s (default 30s) before
        the switch back fires (launcher crashes / anti-cheat restarts must
        not tear the stack up and down); re-entering cancels the pending exit
    """

    def __init__(
        self,
        bus: InProcBus,
        watcher: ProcessWatcher,
        mode: ModeManager | None = None,
        stack: StackController | None = None,
        providers: dict[str, ChatProvider] | None = None,
        robot_name: str = DEFAULT_ROBOT_NAME,
        exit_debounce_s: float = 30.0,
    ) -> None:
        self._bus = bus
        self._watcher = watcher
        self._mode = mode or ModeManager()
        self._stack = stack or NoopStackController()
        self._providers = providers or {}
        self._robot_name = robot_name
        self._exit_debounce_s = exit_debounce_s
        self._exit_pending: dict[str, float] = {}  # game -> first-seen-gone ts

    @property
    def mode(self) -> ModeManager:
        return self._mode

    async def poll_once(self) -> list[ModeSwitch]:
        """One watcher poll; execute and publish any transitions."""
        import time

        switches: list[ModeSwitch] = []
        now = time.monotonic()

        for kind, game in self._watcher.poll():
            if kind == "enter":
                self._exit_pending.pop(game, None)  # came back: cancel debounce
                sw = self._mode.on_game_enter(game)
                if sw is None:
                    continue
                self._stack.stop()
                await self._publish_enter(sw)
                switches.append(sw)
            else:
                # record the candidate; confirm only after the debounce window
                self._exit_pending.setdefault(game, now)

        # debounce confirmation: game still gone after the window AND
        # nothing else whitelisted is running
        running = self._watcher.running
        for game, first_gone in list(self._exit_pending.items()):
            if now - first_gone < self._exit_debounce_s:
                continue
            if running:
                break  # another game keeps silent mode alive (GAME-05)
            del self._exit_pending[game]
            sw = self._mode.on_game_exit(game)
            if sw is None:
                continue
            await self._publish_exit(sw)
            self._stack.start()
            switches.append(sw)

        # enter switches were appended inside the loop above
        return switches

    async def _publish_enter(self, sw: ModeSwitch) -> None:
        logger.info("game enter: %s -> %s", sw.game, sw.mode)
        await self._bus.publish(
            topics.MODE_SWITCH,
            {"mode": "game", "tier": sw.tier, "source": sw.source, "game": sw.game},
        )
        await self._bus.publish(
            topics.SYSTEM_NOTIFY,
            {"level": "info", "text": f"检测到游戏 {sw.game}，已切换安静模式（本地语音栈已暂停）"},
        )
        await self._bus.publish(
            topics.RENDER_COMMAND, {"cmd": "scene", "scene": "game_hud"}
        )

    async def _publish_exit(self, sw: ModeSwitch) -> None:
        logger.info("game exit: %s -> normal", sw.game)
        await self._bus.publish(
            topics.MODE_SWITCH,
            {"mode": "normal", "tier": sw.tier, "source": sw.source, "game": sw.game},
        )
        await self._bus.publish(
            topics.SYSTEM_NOTIFY,
            {"level": "info", "text": f"游戏退出，{self._robot_name}回来了"},
        )
        await self._bus.publish(
            topics.RENDER_COMMAND, {"cmd": "scene", "scene": "idle"}
        )

    async def run(self, interval: float = 2.0) -> None:
        """Production loop (real psutil provider wired by the launcher)."""
        while True:
            await self.poll_once()
            await asyncio.sleep(interval)
