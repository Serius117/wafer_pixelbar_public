"""M5 tests: gamedog (watcher/mode/providers/stack/service) + enroll + calibration."""

from pathlib import Path

import pytest
import yaml

from core.bus import topics
from core.bus.hub import InProcBus
from core.gate.policy import SpeakerProfile
from core.gate.profiles import load_gate_setup
from engines.voiceprint import EnrollmentStore, SeededEmbedder
from gamedog.mode import MODE_GAME_SILENT, MODE_NORMAL, ModeManager
from gamedog.providers import GhostChatProvider, load_providers
from gamedog.service import GameDog
from gamedog.stack import NoopStackController, StackController
from gamedog.watcher import ProcessWatcher


class SpyStack(StackController):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def stop(self) -> None:
        self.calls.append("stop")

    def start(self) -> None:
        self.calls.append("start")


class ScriptedProvider:
    """Process-list provider that replays a scripted sequence of snapshots."""

    def __init__(self, snapshots: list[set[str]]) -> None:
        self._snapshots = list(snapshots)
        self._i = 0

    def __call__(self) -> set[str]:
        snap = self._snapshots[min(self._i, len(self._snapshots) - 1)]
        self._i += 1
        return set(snap)


class TestProcessWatcher:
    def test_enter_and_exit_events(self) -> None:
        watcher = ProcessWatcher(
            whitelist=["game.exe", "other.exe"],
            provider=ScriptedProvider([set(), {"game.exe"}, {"game.exe"}, set()]),
        )
        assert watcher.poll() == []
        assert watcher.poll() == [("enter", "game.exe")]
        assert watcher.poll() == []  # still running, no event
        assert watcher.poll() == [("exit", "game.exe")]

    def test_non_whitelisted_ignored(self) -> None:
        watcher = ProcessWatcher(
            whitelist=["game.exe"],
            provider=ScriptedProvider([{"chrome.exe", "notepad.exe"}]),
        )
        assert watcher.poll() == []
        assert watcher.running == set()

    def test_case_insensitive(self) -> None:
        watcher = ProcessWatcher(
            whitelist=["Game.EXE"], provider=ScriptedProvider([{"game.exe"}])
        )
        assert watcher.poll() == [("enter", "game.exe")]

    def test_multiple_games_enter_exit_together(self) -> None:
        watcher = ProcessWatcher(
            whitelist=["a.exe", "b.exe"],
            provider=ScriptedProvider([{"a.exe", "b.exe"}, set()]),
        )
        assert watcher.poll() == [("enter", "a.exe"), ("enter", "b.exe")]
        assert watcher.poll() == [("exit", "a.exe"), ("exit", "b.exe")]


class TestModeManager:
    def test_normal_to_game_and_back(self) -> None:
        mm = ModeManager()
        sw = mm.on_game_enter("game.exe")
        assert sw is not None and sw.mode == MODE_GAME_SILENT and sw.game == "game.exe"
        assert mm.mode == MODE_GAME_SILENT

        sw = mm.on_game_exit("game.exe")
        assert sw is not None and sw.mode == MODE_NORMAL and sw.game is None
        assert mm.mode == MODE_NORMAL

    def test_double_enter_is_noop(self) -> None:
        mm = ModeManager()
        assert mm.on_game_enter("a.exe") is not None
        assert mm.on_game_enter("b.exe") is None  # already in game mode
        assert mm.active_game == "a.exe"

    def test_exit_untracked_game_is_noop(self) -> None:
        mm = ModeManager()
        assert mm.on_game_exit("game.exe") is None  # never entered

    def test_exit_wrong_game_is_noop(self) -> None:
        mm = ModeManager()
        mm.on_game_enter("a.exe")
        assert mm.on_game_exit("b.exe") is None
        assert mm.mode == MODE_GAME_SILENT
        assert mm.on_game_exit("a.exe") is not None  # the tracked one still works


class TestProviders:
    def test_ghost_unavailable_and_raises(self) -> None:
        ghost = GhostChatProvider()
        assert ghost.available() is False
        with pytest.raises(NotImplementedError):
            import asyncio

            asyncio.run(ghost.chat("hi"))

    def test_load_providers_from_config(self) -> None:
        registry = load_providers({"providers": {"game_chat": {"kind": "ghost"}}})
        assert "game_chat" in registry
        assert isinstance(registry["game_chat"], GhostChatProvider)

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError):
            load_providers({"providers": {"game_chat": {"kind": "cloud_ds_v4"}}})

    def test_empty_config(self) -> None:
        assert load_providers({}) == {}


class TestGameDogService:
    def _make_dog(self, snapshots: list[set[str]], stack: StackController | None = None):
        bus = InProcBus()
        events: list[tuple[str, dict]] = []
        bus.subscribe("*", lambda t, p: events.append((t, dict(p))))
        watcher = ProcessWatcher(
            whitelist=["game.exe"], provider=ScriptedProvider(snapshots)
        )
        dog = GameDog(bus=bus, watcher=watcher, stack=stack or NoopStackController(),
                      exit_debounce_s=0.0)  # immediate exit confirmation (legacy tests)
        return dog, bus, events

    async def test_game_enter_publishes_triple(self) -> None:
        dog, _, events = self._make_dog([{"game.exe"}])
        switches = await dog.poll_once()

        assert len(switches) == 1
        kinds = [t for t, _ in events]
        assert kinds == [topics.MODE_SWITCH, topics.SYSTEM_NOTIFY, topics.RENDER_COMMAND]

        mode_ev, notify_ev, render_ev = (p for _, p in events)
        assert mode_ev == {
            "mode": "game", "tier": "silent", "source": "gamedog", "game": "game.exe",
        }
        assert "游戏" in notify_ev["text"]
        assert render_ev == {"cmd": "scene", "scene": "game_hud"}

    async def test_game_exit_restores(self) -> None:
        dog, _, events = self._make_dog([{"game.exe"}, set()])
        await dog.poll_once()
        await dog.poll_once()

        mode_events = [p for t, p in events if t == topics.MODE_SWITCH]
        assert mode_events[0]["mode"] == "game"
        assert mode_events[1]["mode"] == "normal"
        render_events = [p for t, p in events if t == topics.RENDER_COMMAND]
        assert render_events[-1]["scene"] == "idle"

    async def test_stack_stopped_and_restarted(self) -> None:
        stack = SpyStack()
        dog, _, _ = self._make_dog([{"game.exe"}, {"game.exe"}, set()], stack=stack)
        await dog.poll_once()
        assert stack.calls == ["stop"]
        await dog.poll_once()  # still running: no more calls
        assert stack.calls == ["stop"]
        await dog.poll_once()  # exit
        assert stack.calls == ["stop", "start"]

    async def test_no_game_no_events(self) -> None:
        dog, _, events = self._make_dog([set()])
        switches = await dog.poll_once()
        assert switches == []
        assert events == []


class TestEnroll:
    def test_update_speakers_yaml(self, tmp_path) -> None:
        import sys

        sys.path.insert(0, str(Path(__file__).parent.parent))
        from scripts.enroll import update_speakers_yaml

        cfg = tmp_path / "speakers.yaml"
        update_speakers_yaml(cfg, "master", "主人", "admin", 1, "data/speakers/master.json")
        update_speakers_yaml(cfg, "wife", "老婆", "member", 2, "data/speakers/wife.json")

        setup = load_gate_setup(cfg)
        assert setup.profiles["master"] == SpeakerProfile(
            speaker_id="master", name="主人", role="admin", priority=1
        )
        assert setup.profiles["wife"].role == "member"
        # non-speaker sections must survive rewrites
        raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert raw["speakers"]["master"]["embedding"] == "data/speakers/master.json"

    def test_enroll_samples_averages(self) -> None:
        import sys

        sys.path.insert(0, str(Path(__file__).parent.parent))
        from engines.voiceprint import normalize
        from scripts.enroll import enroll_samples

        embed_fn = lambda pcm: normalize([1.0, 0.0])  # noqa: E731
        emb = enroll_samples(embed_fn, [b"a", b"b", b"c"])
        assert emb == pytest.approx([1.0, 0.0])

    def test_read_wav_pcm_validates(self, tmp_path) -> None:
        import sys
        import wave

        sys.path.insert(0, str(Path(__file__).parent.parent))
        from scripts.enroll import read_wav_pcm

        # valid: 16k mono s16
        good = tmp_path / "good.wav"
        with wave.open(str(good), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x01" * 100)
        pcm = read_wav_pcm(good)
        assert len(pcm) == 200

        # wrong rate
        bad = tmp_path / "bad.wav"
        with wave.open(str(bad), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\x00\x01")
        with pytest.raises(ValueError):
            read_wav_pcm(bad)

    def test_seeded_enrollment_end_to_end(self, tmp_path) -> None:
        """sim-seed enrollment -> store -> scores via SeededVerifier (CI path)."""
        store = EnrollmentStore(tmp_path)
        store.save("master", SeededEmbedder(seed=7).embedding())
        store.save("wife", SeededEmbedder(seed=99).embedding())

        from engines.voiceprint import SeededVerifier

        master_voice = SeededVerifier(store, seed=7)
        scores = master_voice.scores(b"pcm")
        assert scores["master"] > 0.99
        assert scores["wife"] < 0.5

        stranger = SeededVerifier(store, seed=1234)
        stranger_scores = stranger.scores(b"pcm")
        assert all(s < 0.5 for s in stranger_scores.values())


class TestCalibration:
    def test_synthetic_distributions_separated(self) -> None:
        import sys

        sys.path.insert(0, str(Path(__file__).parent.parent))
        from scripts.calibrate_gate import recommend, sweep, synthetic_score_sets

        genuine, imposter = synthetic_score_sets()
        assert min(genuine) > max(imposter)  # clean separation by construction

        rec = recommend(genuine, imposter)
        assert rec["far_at_t_pass"] == 0.0  # 误触=0 honored
        assert rec["distributions_overlap"] is False
        assert rec["t_gray"] < rec["t_pass"]

        rows = sweep(genuine, imposter)
        assert rows[0]["t"] == pytest.approx(0.30)
        # FAR must be monotonically non-increasing in t
        fars = [r["far"] for r in rows]
        assert all(a >= b for a, b in zip(fars, fars[1:], strict=False))

    def test_recommend_overlap_warning(self) -> None:
        import sys

        sys.path.insert(0, str(Path(__file__).parent.parent))
        from scripts.calibrate_gate import recommend

        # overlapping distributions: one imposter beats the worst genuine
        genuine = [0.60, 0.70, 0.80]
        imposter = [0.65]
        rec = recommend(genuine, imposter)
        assert rec["distributions_overlap"] is True
        assert "WARNING" in rec["note"]

    def test_far_frr_boundary(self) -> None:
        import sys

        sys.path.insert(0, str(Path(__file__).parent.parent))
        from scripts.calibrate_gate import far_frr

        genuine = [0.9, 0.7]
        imposter = [0.5]
        far, frr = far_frr(genuine, imposter, 0.75)
        assert far == 0.0
        assert frr == 0.5  # 0.9 passes, 0.7 rejected
