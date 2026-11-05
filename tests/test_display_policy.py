"""TEST.POLICY.01~12: display state machine validated by voice scenarios.

Each case replays the event stream that a real voice scenario produces
(event shapes proven by TEST.OLVCORE with corpus audio) through the
PolicySimulator -- the executable spec of RENDER_POLICY §4/§5 -- and
asserts the display action sequence. This validates the DESIGN itself
(and becomes the acceptance spec for TASK.SLOT.02).
"""

from __future__ import annotations

from display_policy_model import PolicySimulator


def kinds(sim: PolicySimulator) -> list[tuple[str, object]]:
    return [(k, v) for _, k, v in sim.actions]


class TestVoiceScenarios:
    """TEST.POLICY.01~12 -- voice-driven display policy scenarios."""

    def test_01_chat_turn_holds_normal_during_generation(self) -> None:
        """Voice: master_chat_02.wav (wake + casual chat).
        Ruling under test: LLM generation HOLDS normal -- no thinking slot."""
        sim = PolicySimulator()
        sim.feed("session.state", {"state": "listening"})
        sim.feed("session.state", {"state": "thinking"})  # ASR endpoint
        assert kinds(sim) == [("slot", 1)], "thinking must not switch face"
        sim.feed("llm.emotion", {"tag": "happy"})
        assert kinds(sim) == [("slot", 1), ("slot", 2)]
        assert not any(k == "text" for k, _ in kinds(sim)), \
            "normal mode: no text (voice carries the reply)"
        sim.advance(10.0)
        assert kinds(sim)[-1] == ("clock", None), "idle 10s -> clock"

    def test_02_dispatch_execution_returns_to_clock(self) -> None:
        """Voice: master_task.wav (wake + dispatch a task).
        Ruling under test: execution returns to the native clock."""
        sim = PolicySimulator()
        sim.feed("session.state", {"state": "listening"})
        sim.feed("task.progress", {"phase": "dispatched"})
        assert ("slot", 2) in kinds(sim)
        assert ("voice", "收到，开工！") in kinds(sim), \
            "normal mode: dispatch confirm is VOICED, not texted"
        sim.feed("task.progress", {"phase": "running"})
        assert kinds(sim)[-1] == ("clock", None), \
            "execution must return to clock (light says busy)"
        assert ("slot", 6) not in kinds(sim), "no thinking face at work"

    def test_03_completion_celebrate_no_text(self) -> None:
        """Voice: task finishes ok (task.result).
        Ruling: celebrate is immediate (no text shadow), voice speaks."""
        sim = PolicySimulator()
        sim.feed("task.result", {"ok": True, "summary": "3 files fixed"})
        seq = kinds(sim)
        assert ("slot", 7) in seq
        assert ("voice", "搞定！") in seq
        assert not any(k == "text" for k, _ in seq)
        sim.advance(10.0)
        assert kinds(sim)[-1] == ("clock", None)

    def test_04_failure_sad(self) -> None:
        sim = PolicySimulator()
        sim.feed("task.result", {"ok": False, "error": "rc=1"})
        assert ("slot", 3) in kinds(sim)
        assert ("voice", "呜，下次努力") in kinds(sim)

    def test_05_cancel_dedicated_wording(self) -> None:
        """Voice: master_cancel.wav. Ruling: cancel != failure wording."""
        sim = PolicySimulator()
        sim.feed("task.cancel", {"reason": "user_request"})
        assert ("slot", 1) in kinds(sim)
        assert ("voice", "已取消啦") in kinds(sim)
        assert ("voice", "呜，下次努力") not in kinds(sim)

    def test_06_clarify_thinking_slot(self) -> None:
        """Voice: master_badname.wav (alias not found).
        thinking slot = user participation (clarify)."""
        sim = PolicySimulator()
        sim.feed("task.clarify", {"spoken": "音乐", "candidates": ["本项目"]})
        assert ("slot", 6) in kinds(sim)
        assert ("voice", "没找到项目呀") in kinds(sim)

    def test_07_permission_thinking_and_approve_silent(self) -> None:
        sim = PolicySimulator()
        sim.feed("task.permission", {"kind": "file", "target": "x.py"})
        assert ("slot", 6) in kinds(sim)
        assert ("voice", "等你点头哦") in kinds(sim)
        before = len(sim.actions)
        sim.feed("task.approve", {"allow": True})
        assert len(sim.actions) == before, "approve is not a display event"

    def test_08_game_mode_text_substitute(self) -> None:
        """Ruling: no TTS in game mode -> words become TEXT + 5s re-assert."""
        sim = PolicySimulator()
        sim.feed("mode.switch", {"mode": "game"})
        assert ("game", None) in kinds(sim)
        assert ("text", "游戏愉快！") in kinds(sim), \
            "game mode: text substitutes voice"
        sim.advance(5.0)  # text hold expires -> re-assert game scene
        assert kinds(sim)[-1] == ("game", None)
        sim.advance(5.0)  # game scene 10s -> clock
        assert kinds(sim)[-1] == ("clock", None)
        # task result while in game mode: text, not voice
        sim.feed("task.result", {"ok": True, "summary": "ok"})
        assert ("slot", 7) in kinds(sim)
        assert ("text", "搞定！") in kinds(sim)
        assert not any(k == "voice" for k, _ in kinds(sim))

    def test_09_degrade_angry_script(self) -> None:
        """Ruling: degrade = angry 5s -> text -> clock; events ignored."""
        sim = PolicySimulator()
        sim.feed("render.command", {"cmd": "error"})
        assert kinds(sim) == [("slot", 4)], "angry first, nothing else"
        sim.feed("llm.emotion", {"tag": "happy"})  # ignored during script
        sim.advance(5.0)
        assert ("text", "歇一下") in kinds(sim)
        sim.advance(5.0)
        assert kinds(sim)[-1] == ("clock", None)
        assert len([k for k, _ in kinds(sim) if k == "slot"]) == 1, \
            "no further slot switches during the script"

    def test_10_stranger_silence_red_line(self) -> None:
        sim = PolicySimulator()
        sim.feed("gate.decision", {"verdict": "drop", "speaker_id": "elder_1"})
        sim.advance(20.0)
        assert sim.actions == [], "stranger: zero display output"

    def test_11_idle_decay_single_clock(self) -> None:
        sim = PolicySimulator()
        sim.feed("llm.emotion", {"tag": "normal"})
        sim.advance(10.0)
        sim.advance(30.0)
        clocks = [k for k, _ in kinds(sim) if k == "clock"]
        assert clocks == ["clock"], "decay fires exactly once"

    def test_12_fallback_text_reassert_timing(self) -> None:
        """Game mode: text holds 5s (not 3s), then the face returns."""
        sim = PolicySimulator()
        sim.feed("mode.switch", {"mode": "game"})
        sim.feed("task.cancel", {"reason": "user"})
        n_text = kinds(sim).count(("text", "已取消啦"))
        assert n_text == 1
        sim.advance(4.9)
        assert kinds(sim)[-1] != ("slot", 1), "text still holding at 4.9s"
        sim.advance(0.1)
        assert kinds(sim)[-1] == ("slot", 1), "face back at 5.0s"
