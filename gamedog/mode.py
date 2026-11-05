"""Mode manager: normal <-> game_silent transitions (T5.6, D19/D20).

v1 modes:
  - normal: full local stack (ASR/LLM/TTS), ~10.5 GB VRAM
  - game_silent: stack stopped, LED HUD + toast only, ~0 VRAM
  - game_chat: v1.1 ghost -- provider exists but is a placeholder (T5.7)

Transitions are guarded: only one active game drives the mode; re-entering
while already in game mode is a no-op; exiting a game we are not tracking
is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass

MODE_NORMAL = "normal"
MODE_GAME_SILENT = "game_silent"
MODE_GAME_CHAT = "game_chat"


@dataclass(frozen=True, slots=True)
class ModeSwitch:
    """One executed mode transition (published to the bus as mode.switch)."""

    mode: str
    tier: str  # "silent" | "chat" | "none"
    source: str  # "gamedog" | "hotkey" | ...
    game: str | None


class ModeManager:
    """Tracks the current mode and validates transitions."""

    def __init__(self) -> None:
        self._mode = MODE_NORMAL
        self._active_game: str | None = None

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def active_game(self) -> str | None:
        return self._active_game

    def on_game_enter(self, game: str) -> ModeSwitch | None:
        """Enter game_silent for `game`; None if not applicable."""
        if self._mode != MODE_NORMAL:
            return None
        self._mode = MODE_GAME_SILENT
        self._active_game = game
        return ModeSwitch(MODE_GAME_SILENT, "silent", "gamedog", game)

    def on_game_exit(self, game: str) -> ModeSwitch | None:
        """Return to normal when the tracked game exits; None if not applicable."""
        if self._mode != MODE_GAME_SILENT or self._active_game != game:
            return None
        self._mode = MODE_NORMAL
        self._active_game = None
        return ModeSwitch(MODE_NORMAL, "none", "gamedog", None)

    def on_game_chat_requested(self) -> ModeSwitch | None:
        """v1.1 placeholder: always rejected in v1 (ghost provider)."""
        return None
