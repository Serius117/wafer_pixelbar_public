"""E-group implementation tests: multi-project (D33), voiceprint (E.3),
and project utilities.

Covers TEST_DESIGN PROJECT-01~06 + DEV-L3-02 (SherpaCampplusVerifier unit).
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from bridge.composer import TaskComposer
from core.bus import topics
from core.bus.hub import InProcBus
from core.gate.policy import SpeakerProfile
from core.interfaces import Route
from core.pipeline import ConversationPipeline
from core.projects import (
    ProjectConfig,
    ProjectEntry,
    add_project,
    load_projects,
    save_default,
    suggest_alias_candidates,
)
from core.sim import CaptureTts, FixtureAsr, ScriptedLlm, ScriptedVerifier

ROOT = Path(__file__).parent.parent
ALIASES = ["本项目", "字幕项目", "音乐项目", "工作台"]


class TestComposerProjectGrammar:
    """PROJECT-01: deterministic alias grammar, position-independent."""

    def _composer(self) -> TaskComposer:
        c = TaskComposer()
        c.set_project_aliases(ALIASES)
        return c

    def test_prefix_switch(self) -> None:
        alias, spoken, rest = self._composer().extract_project("在字幕项目里跑测试")
        assert alias == "字幕项目"
        assert rest == "跑测试"

    def test_suffix_switch(self) -> None:
        alias, spoken, rest = self._composer().extract_project("跑下测试在音乐项目")
        assert alias == "音乐项目"
        assert "跑下测试" in rest

    def test_go_switch(self) -> None:
        alias, spoken, rest = self._composer().extract_project("去音乐项目跑一下")
        assert alias == "音乐项目"
        assert rest == "跑一下"

    def test_return_switch(self) -> None:
        alias, spoken, rest = self._composer().extract_project("回本项目修个bug")
        assert alias == "本项目"
        assert rest == "修个bug"

    def test_no_project_mention(self) -> None:
        alias, spoken, rest = self._composer().extract_project("跑一下测试")
        assert alias is None
        assert rest == "跑一下测试"

    def test_unknown_alias_returns_not_found(self) -> None:
        """PROJECT-02: unregistered name -> __NOT_FOUND__ + the spoken text."""
        alias, spoken, rest = self._composer().extract_project("在字幕项木里跑测试")
        assert alias == "__NOT_FOUND__"
        assert "字幕项木" in rest or rest  # the unmatched alias is reported

    def test_with_punctuation(self) -> None:
        alias, spoken, rest = self._composer().extract_project("在字幕项目里，帮我查一下")
        assert alias == "字幕项目"
        assert rest == "，帮我查一下"


class TestProjectConfig:
    """PROJECT-08: yaml load/save/add."""

    def test_load(self) -> None:
        cfg = load_projects(ROOT / "config" / "projects.yaml")
        assert len(cfg.entries) >= 2
        # 2026-10-25: the home machine runs this repo, so 本项目 is the default.
        assert cfg.default_alias == "本项目"
        # machine-agnostic: just check it resolves to an existing dir
        assert cfg.resolve("本项目").path.is_dir()

    def test_add_and_save_default(self, tmp_path) -> None:
        yaml_path = tmp_path / "projects.yaml"
        yaml_path.write_text(
            "default: 工作台\nprojects:\n  工作台:\n    path: D:\\\\wd\n",
            encoding="utf-8",
        )
        add_project(yaml_path, "新项目", Path("D:\\new_proj"))
        save_default(yaml_path, "新项目")
        cfg = load_projects(yaml_path)
        assert cfg.default_alias == "新项目"
        assert "新项目" in cfg.aliases()

    def test_missing_file_empty(self, tmp_path) -> None:
        cfg = load_projects(tmp_path / "nope.yaml")
        assert cfg.entries == {}
        assert cfg.default_alias == ""


class TestSuggestAliasCandidates:
    """PROJECT-02 helper: closest-first ranking."""

    def test_closest_first(self) -> None:
        ranked = suggest_alias_candidates("字幕项木", ["字幕项目", "音乐项目", "本项目"])
        assert ranked[0] == "字幕项目"  # most character overlap

    def test_top_limit(self) -> None:
        ranked = suggest_alias_candidates("x", ["a", "b", "c", "d", "e"], top=3)
        assert len(ranked) == 3


class TestProjectSwitchPipeline:
    """PROJECT-05/06: switching clears _recent + is global."""

    def _make(self, tmp_path, projects: ProjectConfig):
        import json

        (tmp_path / "transcripts.json").write_text(
            json.dumps(["在字幕项目里跑测试", "在音乐项目里跑一下", "跑一下测试"]),
            encoding="utf-8",
        )
        (tmp_path / "replies.json").write_text('["[emotion:happy] 好的"]', encoding="utf-8")
        bus = InProcBus()
        from bridge.server import Bridge

        bridge = Bridge(bus=bus, session_book_path=tmp_path / "sb.json",
                        grace_seconds=999.0)
        composer = bridge._composer  # noqa: SLF001
        composer.set_project_aliases(projects.aliases())
        tts = CaptureTts()
        pipeline = ConversationPipeline(
            source=None,
            verifier=ScriptedVerifier([{"master": 0.75}] * 3),
            asr=FixtureAsr(tmp_path),
            llm=ScriptedLlm(tmp_path),
            tts=tts,
            bus=bus,
            bridge=bridge,
            profiles={"master": SpeakerProfile(
                speaker_id="master", name="主人", role="admin", priority=1)},
            projects=projects,
        )
        return pipeline, bridge, bus, tts

    def _projects(self) -> ProjectConfig:
        return ProjectConfig(
            entries={
                "字幕项目": ProjectEntry("字幕项目", Path("D:/subtitle")),
                "音乐项目": ProjectEntry("音乐项目", Path("D:/music")),
                "本项目": ProjectEntry("本项目", Path("D:/self_agent")),
            },
            default_alias="本项目",
        )

    async def test_switch_clears_recent(self, tmp_path) -> None:
        """PROJECT-05 (red line): old project context never bleeds into
        the new project's task prompt."""
        import json

        (tmp_path / "transcripts.json").write_text(
            json.dumps(["在字幕项目里跑测试", "在音乐项目里跑一下"]),
            encoding="utf-8")
        (tmp_path / "replies.json").write_text('["ok"]', encoding="utf-8")
        bus = InProcBus()
        from bridge.server import Bridge

        bridge = Bridge(bus=bus, session_book_path=tmp_path / "sb.json",
                        grace_seconds=0.0)  # immediate dispatch
        bridge._composer.set_project_aliases(self._projects().aliases())
        tts = CaptureTts()
        pipeline = ConversationPipeline(
            source=None,
            verifier=ScriptedVerifier([{"master": 0.75}] * 2),
            asr=FixtureAsr(tmp_path),
            llm=ScriptedLlm(tmp_path),
            tts=tts,
            bus=bus,
            bridge=bridge,
            profiles={"master": SpeakerProfile(
                speaker_id="master", name="主人", role="admin", priority=1)},
            projects=self._projects(),
        )
        events = []
        bus.subscribe("*", lambda t, p: events.append((t, dict(p))))

        # turn 1: switch to 字幕项目 with a task mentioning 解码报错
        pipeline._recent.append("我们刚才聊了解码报错的问题")
        r1 = await pipeline.run_turn(b"\x00" * 100)
        assert r1 == Route.TASK_DIRECT
        assert pipeline._recent == []  # cleared!
        assert bridge._default_project == "D:/subtitle"

        import asyncio
        await asyncio.sleep(0.1)  # let grace=0 flush complete (BRIDGE-26)

        # turn 2: switch to 音乐项目 — task prompt must NOT contain 解码/报错
        pipeline._recent.append("刚才聊了音乐播放器的事")
        r2 = await pipeline.run_turn(b"\x00" * 100)
        assert r2 == Route.TASK_DIRECT
        assert bridge._default_project == "D:/music"

        # verify the second task's prompt has no old-project contamination
        import asyncio
        await asyncio.sleep(0.2)  # let grace=0 flush complete
        dispatched = [p for t, p in events if t == topics.TASK_PROGRESS
                      and p.get("phase") == "dispatched"]
        assert len(dispatched) >= 2  # two tasks dispatched
        music_prompt = dispatched[-1].get("detail", "")
        assert "解码" not in music_prompt, f"contamination: {music_prompt}"
        assert "报错" not in music_prompt, f"contamination: {music_prompt}"
        await bridge.close()

    async def test_switch_is_global(self, tmp_path) -> None:
        """PROJECT-06: after switching, subsequent tasks WITHOUT a project
        mention stay on the new project (switching is global, not one-shot)."""
        import json

        (tmp_path / "transcripts.json").write_text(
            json.dumps(["在字幕项目里跑测试", "跑一下测试"]), encoding="utf-8")
        (tmp_path / "replies.json").write_text('["ok"]', encoding="utf-8")
        bus = InProcBus()
        from bridge.server import Bridge

        bridge = Bridge(bus=bus, session_book_path=tmp_path / "sb.json",
                        grace_seconds=999.0)
        bridge._composer.set_project_aliases(self._projects().aliases())
        tts = CaptureTts()
        pipeline = ConversationPipeline(
            source=None,
            verifier=ScriptedVerifier([{"master": 0.75}] * 2),
            asr=FixtureAsr(tmp_path),
            llm=ScriptedLlm(tmp_path),
            tts=tts,
            bus=bus,
            bridge=bridge,
            profiles={"master": SpeakerProfile(
                speaker_id="master", name="主人", role="admin", priority=1)},
            projects=self._projects(),
        )

        await pipeline.run_turn(b"\x00" * 100)  # "在字幕项目里跑测试" -> switch
        assert bridge._default_project == "D:/subtitle"

        # second task has NO project mention -> should stay on 字幕项目
        await pipeline.run_turn(b"\x00" * 100)  # "跑一下测试"
        assert bridge._default_project == "D:/subtitle"  # still there ✓
        await bridge.close()

    async def test_project_switch_event_published(self, tmp_path) -> None:
        """PROJECT_SWITCH event on the bus for console/audit."""
        pipeline, bridge, bus, _ = self._make(tmp_path, self._projects())
        switch_events = []
        bus.subscribe(topics.PROJECT_SWITCH, lambda t, p: switch_events.append(p))

        await pipeline.run_turn(b"\x00" * 100)  # switch to 字幕项目
        assert len(switch_events) == 1
        assert switch_events[0]["alias"] == "字幕项目"
        assert switch_events[0]["path"] == "D:/subtitle"
        await bridge.close()

    async def test_unknown_alias_triggers_clarification(self, tmp_path) -> None:
        """PROJECT-02/03: not-found -> spoken feedback with candidates;
        next turn confirming the alias resumes the original task."""
        import json

        (tmp_path / "transcripts.json").write_text(
            json.dumps(["在字幕项木里跑测试", "字幕项目"]), encoding="utf-8")
        (tmp_path / "replies.json").write_text('["ok"]', encoding="utf-8")
        bus = InProcBus()
        from bridge.server import Bridge

        bridge = Bridge(bus=bus, session_book_path=tmp_path / "sb.json",
                        grace_seconds=999.0)
        bridge._composer.set_project_aliases(self._projects().aliases())
        tts = CaptureTts()
        pipeline = ConversationPipeline(
            source=None,
            verifier=ScriptedVerifier([{"master": 0.75}] * 2),
            asr=FixtureAsr(tmp_path),
            llm=ScriptedLlm(tmp_path),
            tts=tts,
            bus=bus,
            bridge=bridge,
            profiles={"master": SpeakerProfile(
                speaker_id="master", name="主人", role="admin", priority=1)},
            projects=self._projects(),
        )

        # turn 1: unknown alias -> clarification, no dispatch
        r1 = await pipeline.run_turn(b"\x00" * 100)
        assert r1 == Route.TASK_DIRECT  # routed but consumed by clarification
        assert bridge._default_project != "D:/subtitle"  # NOT switched yet
        assert bridge.has_work() is False  # no task dispatched
        assert any("没找到" in t for t in tts.captured)  # feedback spoken
        assert any("字幕项目" in t for t in tts.captured)  # candidate listed

        # turn 2: user says "字幕项目" -> confirm + original task dispatched
        await pipeline.run_turn(b"\x00" * 100)
        assert bridge._default_project == "D:/subtitle"  # NOW switched
        assert bridge.has_work() is True  # original task dispatched
        await bridge.close()


class TestBridgeProjectSwitchEvent:
    """Bridge subscribes to PROJECT_SWITCH from the bus (console entry)."""

    async def test_bridge_receives_switch(self, tmp_path) -> None:
        from bridge.server import Bridge

        bus = InProcBus()
        bridge = Bridge(bus=bus, session_book_path=tmp_path / "sb.json")
        assert bridge._default_project == "."
        await bus.publish(topics.PROJECT_SWITCH, {
            "alias": "字幕项目", "path": "D:/subtitle",
        })
        assert bridge._default_project == "D:/subtitle"
        await bridge.close()


class TestSherpaCampplusVerifier:
    """DEV-L3-02: SherpaCampplusVerifier unit (skipif model absent)."""

    MODEL = Path(r"models\speaker"
                 r"\3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx")

    def test_construction(self, tmp_path) -> None:
        if not self.MODEL.exists():
            pytest.skip("CAM++ model not present")
        from engines.voiceprint import SherpaCampplusVerifier

        store = tmp_path / "speakers"
        verifier = SherpaCampplusVerifier.__new__(SherpaCampplusVerifier)
        EmbeddingVerifier = __import__(
            "engines.voiceprint", fromlist=["EmbeddingVerifier"]).EmbeddingVerifier
        EmbeddingVerifier.__init__(verifier, store)
        verifier._model_path = self.MODEL
        verifier._extractor = None
        assert verifier is not None

    def test_embed_returns_192_dim_normalized(self, tmp_path) -> None:
        if not self.MODEL.exists():
            pytest.skip("CAM++ model not present")
        from engines.voiceprint import EnrollmentStore, SherpaCampplusVerifier

        store = EnrollmentStore(tmp_path)
        verifier = SherpaCampplusVerifier(store, self.MODEL)
        # 1 second of silence
        import numpy as np

        pcm = np.zeros(16000, dtype=np.int16).tobytes()
        emb = verifier.embed(pcm)
        assert len(emb) == 192

    def test_same_voice_higher_than_cross(self, tmp_path) -> None:
        """CAM++ on the synthesized corpus: intra > inter (uses the E.1 gate wavs)."""
        gate_dir = ROOT / "temp_script" / "e1_gate"
        if not self.MODEL.exists() or not gate_dir.exists():
            pytest.skip("model or gate wavs not present")


        from engines.voiceprint import EnrollmentStore, SherpaCampplusVerifier

        store = EnrollmentStore(tmp_path)
        verifier = SherpaCampplusVerifier(store, self.MODEL)

        def read(p):
            with wave.open(str(p), "rb") as w:
                return w.readframes(w.getnframes())

        # register spk0 with one sample
        emb0 = verifier.embed(read(gate_dir / "ref_spk0_0_16k.wav"))
        store.save("spk0", emb0)

        # same voice different sentence
        scores_same = verifier.scores(read(gate_dir / "ref_spk0_1_16k.wav"))
        # different voice
        scores_diff = verifier.scores(read(gate_dir / "ref_spk2_0_16k.wav"))

        assert scores_same["spk0"] > 0.5  # same voice, high
        assert scores_diff["spk0"] < scores_same["spk0"]  # lower for different
