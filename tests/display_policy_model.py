"""Display policy model: executable spec of RENDER_POLICY (§4/§5).

A synchronous pure model of the E-world display state machine, driven by
the same bus events the real controller will consume. Voice-scenario
event streams (proven by TEST.OLVCORE with real corpus audio) are
replayed through this model to validate the DESIGN before TASK.SLOT.02
implements the real ExpressionSlotController.

Encodes the 2026-10-24 rulings (RENDER_POLICY):
  - "speak when you can, text when you can't": normal mode = slot +
    voice (NO text); game mode = slot + text (5s hold, then re-assert)
  - generation HOLDS normal (no thinking slot); task execution returns
    to the native clock (the flowing mood light says "busy")
  - thinking slot is reserved for user participation (clarify /
    permission)
  - degrade script: angry 5s -> text -> clock (other events ignored)
  - idle 10s -> clock; game scene 10s -> clock
  - gate drop (stranger): zero output (silence red line)

Not modelled (recon-dependent): keepalive (unknown #5) and actual
0xEF/0xE8 wire effects.
"""

from __future__ import annotations

SLOT_BY_TAG = {"normal": 1, "happy": 2, "sad": 3, "angry": 4}

# event word -> base text (channel decided by voice availability)
WORDS = {
    "listening": "在听哦",
    "confirm": "收到，开工！",
    "done": "搞定！",
    "fail": "呜，下次努力",
    "cancel": "已取消啦",
    "permission": "等你点头哦",
    "clarify": "没找到项目呀",
    "game": "游戏愉快！",
    "degrade": "歇一下",
}

TEXT_HOLD_S = 5.0
IDLE_TIMEOUT_S = 10.0
GAME_HOLD_S = 10.0
DEGRADE_ANGRY_S = 5.0


class PolicySimulator:
    """Feed bus events + advance time; collect display actions.

    Actions are ``(t, kind, value)`` tuples: kind in
    ``slot|clock|game|text|voice``; value is the slot number (int),
    the word (str) or None.
    """

    def __init__(self) -> None:
        self.t = 0.0
        self.mode = "normal"  # voice availability: "normal" | "game"
        self.actions: list[tuple[float, str, object]] = []
        self._display: tuple[str, object] = ("clock", None)
        self._idle_at: float | None = None
        self._game_until: float | None = None
        self._reassert_at: float | None = None
        self._degrade: tuple[str, float] | None = None  # (phase, deadline)

    # --- helpers -------------------------------------------------------------

    def _emit(self, kind: str, value: object) -> None:
        self.actions.append((self.t, kind, value))

    def _show(self, kind: str, value: object, idle: bool = True) -> None:
        if self._display != (kind, value):
            self._emit(kind, value)
        self._display = (kind, value)
        self._reassert_at = None
        if idle:
            self._idle_at = self.t + IDLE_TIMEOUT_S

    def _word(self, key: str, game_only: bool = False) -> None:
        """Channel split: normal mode = voice; game mode = text + 5s hold."""
        if self._degrade is not None:
            return
        if self.mode == "normal":
            if game_only:
                return  # listening: the wake reply is the pipeline's TTS
            self._emit("voice", WORDS[key])
            self._idle_at = self.t + IDLE_TIMEOUT_S
        else:
            self._emit("text", WORDS[key])
            self._reassert_at = self.t + TEXT_HOLD_S
            self._idle_at = self.t + IDLE_TIMEOUT_S

    # --- event input ----------------------------------------------------------

    def feed(self, topic: str, payload: dict | None = None) -> None:
        if self._degrade is not None:
            return  # degrade script owns the display until it completes
        payload = payload or {}

        if topic == "session.state":
            state = payload.get("state")
            if state == "listening":
                self._show("slot", 1)
                self._word("listening", game_only=True)
            elif state == "thinking":
                pass  # HOLD current face (2026-10-24 ruling)
            elif state == "standby":
                self._show("clock", None, idle=False)
        elif topic == "llm.emotion":
            self._show("slot", SLOT_BY_TAG.get(payload.get("tag"), 1))
        elif topic == "task.progress":
            if payload.get("phase") == "dispatched":
                self._show("slot", 2)
                self._word("confirm")
            else:
                self._show("clock", None)  # execution returns to clock
        elif topic == "task.result":
            failed = payload.get("ok") is False or bool(payload.get("error"))
            self._show("slot", 3 if failed else 7)
            self._word("fail" if failed else "done")
        elif topic == "task.cancel":
            self._show("slot", 1)
            self._word("cancel")
        elif topic == "task.clarify":
            self._show("slot", 6)
            self._word("clarify")
        elif topic == "task.permission":
            self._show("slot", 6)
            self._word("permission")
        elif topic == "task.approve":
            pass  # approval is not a display event; next progress resumes
        elif topic == "mode.switch":
            if payload.get("mode") == "game":
                self.mode = "game"
                self._show("game", None, idle=False)
                self._game_until = self.t + GAME_HOLD_S
                self._word("game")
            else:
                self.mode = "normal"
                self._show("clock", None, idle=False)
                self._game_until = None
        elif topic == "render.command" and payload.get("cmd") == "error":
            # degrade script: angry 5s -> text -> clock (mode-independent)
            self._show("slot", 4, idle=False)
            self._degrade = ("angry", self.t + DEGRADE_ANGRY_S)
        elif topic == "gate.decision":
            if payload.get("verdict") == "drop":
                pass  # silence red line: zero output

    # --- time -----------------------------------------------------------------

    def advance(self, dt: float) -> None:
        end = self.t + dt
        while True:
            deadlines = [d for d in (self._idle_at, self._game_until,
                                     self._reassert_at,
                                     self._degrade[1] if self._degrade else None)
                         if d is not None and d > self.t]
            if not deadlines or min(deadlines) > end:
                self.t = end
                return
            self.t = min(deadlines)
            self._fire()

    def _fire(self) -> None:
        if self._degrade is not None and self._degrade[1] <= self.t:
            phase = self._degrade[0]
            if phase == "angry":
                self._emit("text", WORDS["degrade"])
                self._degrade = ("text", self.t + TEXT_HOLD_S)
            else:
                self._show("clock", None, idle=False)
                self._degrade = None
            return
        if self._game_until is not None and self._game_until <= self.t:
            self._show("clock", None, idle=False)
            self._game_until = None
            return
        if self._reassert_at is not None and self._reassert_at <= self.t:
            self._reassert_at = None
            self._emit(*self._display)  # re-assert after text hold
            return
        if self._idle_at is not None and self._idle_at <= self.t:
            self._idle_at = None
            if self._display[0] != "clock":
                self._show("clock", None, idle=False)
