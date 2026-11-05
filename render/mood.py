"""Mood light = wafer's work-state indicator (D39; design doc §4.5.2).

Text (0xE8) tells the user *what* happened; the mood light (0x6B) tells them
*which stage* wafer is in -- readable from the corner of the eye. The mapping
is fixed (config/mood.yaml), the LLM never picks it (rule 2), and only the
render process writes the device (rule 3).

States: idle / running / decision / done / failed. One state at a time:
a state change is a single 0x6B write to that effect's light zone
(PIXELBAR_GUIDE §5.1). Device keeps the state across sessions, so `start()`
forces idle and `stop()` resets to factory.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from core.bus import topics
from render.sinks.pixelbar import protocol as px

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("config/mood.yaml")

IDLE = "idle"
RUNNING = "running"
DECISION = "decision"
DONE = "done"
FAILED = "failed"

STATE_ORDER = (IDLE, RUNNING, DECISION, DONE, FAILED)


@dataclass(frozen=True, slots=True)
class MoodStateSpec:
    """One state's device recipe (see config/mood.yaml for the table)."""

    zone: int = px.MOOD_ZONE_BREATHING
    effect: str | None = None
    color: tuple[int, int, int] | None = None
    brightness: int = px.MOOD_BRIGHTNESS_MAX
    speed: int = px.MOOD_SPEED_DEFAULT
    hold_s: float | None = None


DEFAULT_STATES: dict[str, MoodStateSpec] = {
    IDLE: MoodStateSpec(zone=px.MOOD_ZONE_BREATHING, effect="breathing",
                        color=(100, 160, 220), speed=px.MOOD_BREATH_SPEED),
    RUNNING: MoodStateSpec(zone=px.MOOD_ZONE_FLOW, effect="flow"),
    DECISION: MoodStateSpec(zone=px.MOOD_ZONE_SHADOW, effect="shadow"),
    DONE: MoodStateSpec(zone=px.MOOD_ZONE_RIPPLE, effect="ripple", hold_s=5.0),
    FAILED: MoodStateSpec(zone=px.MOOD_ZONE_SOLID, effect="solid",
                          color=(255, 60, 60), hold_s=10.0),
}


def _spec_from(raw: dict, base: MoodStateSpec) -> MoodStateSpec:
    """Merge one yaml entry over its default (explicit null wins)."""
    color = base.color
    if "color" in raw:
        value = raw["color"]
        color = tuple(int(x) for x in value) if value else None
    return MoodStateSpec(
        zone=int(raw.get("zone", base.zone)),
        effect=raw["effect"] if "effect" in raw else base.effect,
        color=color,
        brightness=int(raw.get("brightness", base.brightness)),
        speed=int(raw.get("speed", base.speed)),
        hold_s=(None if raw["hold_s"] is None else float(raw["hold_s"]))
        if "hold_s" in raw else base.hold_s,
    )


def load_mood_states(
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> dict[str, MoodStateSpec]:
    """Parse config/mood.yaml; missing file or bad entry -> defaults."""
    path = Path(config_path)
    if not path.exists():
        logger.warning("mood config %s missing: using built-in defaults", path)
        return dict(DEFAULT_STATES)

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    states = dict(DEFAULT_STATES)
    for name, entry in (raw.get("states") or {}).items():
        name = str(name)
        base = states.get(name, MoodStateSpec())
        if not isinstance(entry, dict):
            logger.warning("skip bad mood state: %s", name)
            continue
        states[name] = _spec_from(entry, base)
    return states


class MoodStateController:
    """Bus events -> mood-light writes, with a minimum dwell and holds.

    `min_dwell_s` keeps the light from strobing when events arrive in bursts
    (task.progress is published per tool call); `decision_timeout_s` returns a
    "needs you" state to idle when nobody answers.
    """

    def __init__(
        self,
        bus: Any,
        sink: Any,
        states: dict[str, MoodStateSpec] | None = None,
        *,
        min_dwell_s: float = 2.0,
        decision_timeout_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._sink = sink
        self._states = dict(states) if states is not None else dict(DEFAULT_STATES)
        self._min_dwell_s = min_dwell_s
        self._decision_timeout_s = decision_timeout_s
        self._clock = clock
        self.current: str | None = None
        self.history: list[str] = []
        self._last_apply_at = 0.0
        self._pending: str | None = None
        self._pending_timer: Any = None
        self._hold_timer: Any = None
        self._decision_timer: Any = None

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Subscribe and force the idle state (the device remembers!)."""
        self._bus.subscribe(topics.TASK_PROGRESS, self._on_task_progress)
        self._bus.subscribe(topics.TASK_RESULT, self._on_task_result)
        self._bus.subscribe(topics.TASK_CANCEL, self._on_task_cancel)
        self._bus.subscribe(topics.TASK_PERMISSION, self._on_task_permission)
        self._bus.subscribe(topics.TASK_APPROVE, self._on_task_approve)
        self._bus.subscribe(topics.RENDER_COMMAND, self._on_render_command)
        self._bus.subscribe(topics.MODE_SWITCH, self._on_mode_switch)
        self._apply(IDLE)  # boot correction (old device state is meaningless)

    def stop(self, reset: bool = True) -> None:
        """Cancel timers; optionally reset the device to its factory state."""
        for timer in (self._pending_timer, self._hold_timer,
                      self._decision_timer):
            if timer is not None:
                timer.cancel()
        self._pending_timer = self._hold_timer = self._decision_timer = None
        self._pending = None
        if reset:
            try:
                self._sink.reset_mood_light()
            except Exception:  # noqa: BLE001 -- teardown must never raise
                logger.exception("mood light reset on stop failed")

    # --- event handlers (fixed mapping, rule 2) ------------------------------

    async def _on_task_progress(self, topic: str, payload: dict) -> None:
        # dispatched / running / tool all mean "working"; repeated progress
        # events map to the same state and therefore write nothing.
        self.request(RUNNING)

    async def _on_task_result(self, topic: str, payload: dict) -> None:
        failed = payload.get("ok") is False or bool(payload.get("error"))
        self.request(FAILED if failed else DONE)

    async def _on_task_cancel(self, topic: str, payload: dict) -> None:
        self.request(FAILED)

    async def _on_task_permission(self, topic: str, payload: dict) -> None:
        self.request(DECISION)

    async def _on_task_approve(self, topic: str, payload: dict) -> None:
        # Permission resolved: leave the "needs you" light immediately.
        self.request(IDLE if payload.get("allow") is False else RUNNING)

    async def _on_render_command(self, topic: str, payload: dict) -> None:
        if payload.get("cmd") == "error":  # rule 6 degradation
            self.request(FAILED)

    async def _on_mode_switch(self, topic: str, payload: dict) -> None:
        # game mode keeps the default breathing look (user ruling, D39)
        if payload.get("mode") in ("game", "normal"):
            self.request(IDLE)

    # --- state machine -------------------------------------------------------

    def request(self, state: str) -> None:
        """Ask for a state; honours the minimum dwell time."""
        if state not in self._states:
            logger.warning("unknown mood state %r ignored", state)
            return
        if state == self.current:
            return  # no write: progress bursts cost nothing
        wait = self._min_dwell_s - (self._clock() - self._last_apply_at)
        if wait <= 0:
            self._apply(state)
            return
        self._pending = state
        if not self._arm(self._pending_timer, wait, self._flush_pending,
                         "_pending_timer"):
            self._apply(state)  # no event loop: fall back to immediate

    def _flush_pending(self) -> None:
        self._pending_timer = None
        state, self._pending = self._pending, None
        if state is not None and state != self.current:
            self._apply(state)

    def _apply(self, state: str) -> None:
        spec = self._states[state]
        self._cancel(self._pending_timer)
        self._pending_timer = None
        self._pending = None
        try:
            ok = self._write(spec)
        except Exception:  # noqa: BLE001 -- a bad sink must not kill render
            logger.exception("mood write failed for state %s", state)
            ok = False
        if not ok:
            logger.warning("mood light write not acknowledged (state %s)", state)
        self.current = state
        self.history.append(state)
        self._last_apply_at = self._clock()

        self._cancel(self._hold_timer)
        self._hold_timer = None
        if spec.hold_s is not None:
            self._arm(self._hold_timer, spec.hold_s,
                      lambda: self.request(IDLE), "_hold_timer")

        self._cancel(self._decision_timer)
        self._decision_timer = None
        if state == DECISION:
            self._arm(self._decision_timer, self._decision_timeout_s,
                      lambda: self.request(IDLE), "_decision_timer")

    def _write(self, spec: MoodStateSpec) -> bool:
        if self._sink is None:
            return True
        if spec.color is not None:
            return bool(self._sink.set_mood_light(
                *spec.color, brightness=spec.brightness,
                speed=spec.speed, index=spec.zone))
        effect = spec.effect or "breathing"
        return bool(self._sink.set_mood_effect(
            effect, brightness=spec.brightness, speed=spec.speed))

    # --- timer helpers -------------------------------------------------------

    def _arm(self, current: Any, delay: float,
             callback: Callable[[], None], slot: str) -> bool:
        """Schedule `callback` after `delay`; False when there is no loop."""
        import asyncio

        self._cancel(current)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            setattr(self, slot, None)
            return False
        setattr(self, slot, loop.call_later(delay, callback))
        return True

    @staticmethod
    def _cancel(timer: Any) -> None:
        if timer is not None:
            timer.cancel()
