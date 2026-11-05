"""TEST.WAKE.08~11: KWS reliability group (TASK.KWS.01/03/04, 2026-10-30).

08: kws_bench mechanics (threshold-sweep stats + FAR=0 recommendation,
    pure functions -- no engines).
10: two-level verification -- low-threshold wake requires the transcript
    to confirm the wake word, otherwise SILENT drop.
11: two-stage UX -- bare "小饼小饼" gets a scripted ack (zero LLM).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "kws_bench", ROOT / "scripts" / "kws_bench.py")
kb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kb)

THRESHOLDS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]


class TestBenchMechanics:
    def test_08_summarize_and_recommend(self, tmp_path):
        """TEST.WAKE.08: sweep stats + FAR=0 threshold recommendation."""
        # positives all hit at 0.10 (index 1); negatives only at index 3
        pos_results = {f"p{i}.wav": 1 for i in range(5)}
        neg_results = {f"n{i}.wav": 3 for i in range(4)}
        pos_files = [Path(p) for p in pos_results]
        neg_files = [Path(p) for p in neg_results]

        pos_sum = kb.summarize(pos_files, pos_results, THRESHOLDS)
        neg_sum = kb.summarize(neg_files, neg_results, THRESHOLDS)

        # recall profile: 0 at 0.05, full from 0.10
        assert pos_sum["per_threshold"][0]["hits"] == 0
        assert pos_sum["per_threshold"][1]["hits"] == 5
        assert pos_sum["strength"]["miss"] == 0
        # negatives start firing only at 0.20 (index 3)
        assert neg_sum["per_threshold"][2]["hits"] == 0
        assert neg_sum["per_threshold"][3]["hits"] == 4

        rec = kb.recommend(pos_sum, neg_sum, THRESHOLDS)
        # lowest threshold with FAR=0 that maximises recall -> 0.10
        assert rec == {"threshold": 0.10, "recall": 1.0,
                       "false_accept": 0.0}

    def test_08_collect_and_misses(self, tmp_path):
        """TEST.WAKE.08: wav discovery + all-miss distribution."""
        (tmp_path / "a.wav").write_bytes(b"RIFF")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.wav").write_bytes(b"RIFF")
        (tmp_path / "x.txt").write_text("no")
        files = kb.collect_wavs(tmp_path)
        assert len(files) == 2

        miss_sum = kb.summarize(files, {"a": 6, "b": 6}, THRESHOLDS)
        assert miss_sum["strength"]["hit_at_all"] == 0
        assert miss_sum["strength"]["miss"] == 2


class FakeBridge:
    """Minimal bridge double (mirrors test_dual_wake)."""

    def __init__(self) -> None:
        self._projects = None
        self.submitted: list[str] = []

    def _ensure_projects(self) -> None:
        pass

    def _extract_project(self, text):
        return None, "", text

    def has_work(self) -> bool:
        return False

    async def submit(self, text, speaker_id):
        self.submitted.append(text)
        return "收到，开工！"

    async def amend(self, text):
        return ""

    async def cancel(self):
        return "已取消啦"

    async def _do_switch(self, alias):
        pass


class TestTwoLevelVerification:
    @pytest.fixture
    def agent(self, monkeypatch):
        import olv.wafer_hooks as wh

        monkeypatch.setenv("WAFER_WAKE_MODE", "kws")
        monkeypatch.setattr(wh, "_current_speaker",
                            {"id": "master", "role": "admin"})
        wh._session = wh._SessionMode()
        a = wh.WaferAgent(llm=None)
        a._bridge = FakeBridge()
        a._pump_started = True
        self.llm_calls: list[str] = []

        async def fake_chat_llm(text):
            self.llm_calls.append(text)
            yield ("llm-reply", "llm-reply")

        monkeypatch.setattr(a, "_chat_llm", fake_chat_llm)
        return a

    async def _say(self, agent, text):
        return [pair async for pair in agent.chat(text)]

    async def test_10_low_conf_wake_silent_drop(self, agent):
        """TEST.WAKE.10: hit without the wake word in the transcript ->
        zero output (the low threshold's false accepts die here)."""
        import olv.wafer_hooks as wh

        wh._session.wake_needs_verify = True
        pairs = await self._say(agent, "今天天气怎么样")
        assert pairs == [], "false wake must be silent"
        assert not self.llm_calls
        assert wh._session.wake_needs_verify is False, "flag consumed once"

    async def test_10_verified_wake_proceeds(self, agent):
        """TEST.WAKE.10: transcript containing the wake word passes."""
        import olv.wafer_hooks as wh

        wh._session.wake_needs_verify = True
        pairs = await self._say(agent, "小饼小饼，今天天气怎么样")
        assert pairs and self.llm_calls == ["小饼小饼，今天天气怎么样"]

    async def test_10_work_wake_passes_verify(self, agent):
        """TEST.WAKE.10: 「小饼干活，…」 also confirms the wake."""
        import olv.wafer_hooks as wh

        wh._session.wake_needs_verify = True
        pairs = await self._say(agent, "小饼干活，查一下测试文件")
        assert agent._bridge.submitted == ["查一下测试文件"], \
            "work-wake must route to the task path (wake stripped)"
        assert pairs, "work-wake must not be dropped"

    async def test_10_flag_cleared_on_standby(self, agent):
        """TEST.WAKE.10: session timeout clears a stale verify flag."""
        import olv.wafer_hooks as wh

        s = wh._SessionMode()
        s.state = "listening"
        s.last_active = 0.0  # ancient -> timeout
        s.wake_needs_verify = True
        assert s.is_standby() is True
        assert s.wake_needs_verify is False


class TestTwoStageAck:
    @pytest.fixture
    def agent(self, monkeypatch):
        import olv.wafer_hooks as wh

        monkeypatch.setattr(wh, "_current_speaker",
                            {"id": "master", "role": "admin"})
        wh._session = wh._SessionMode()
        a = wh.WaferAgent(llm=None)
        a._bridge = FakeBridge()
        a._pump_started = True
        self.llm_calls: list[str] = []

        async def fake_chat_llm(text):
            self.llm_calls.append(text)
            yield ("llm-reply", "llm-reply")

        monkeypatch.setattr(a, "_chat_llm", fake_chat_llm)
        return a

    async def _say(self, agent, text):
        return [pair async for pair in agent.chat(text)]

    async def test_11_bare_chat_wake_ack(self, agent):
        """TEST.WAKE.11: bare 「小饼小饼」 -> scripted ack, zero LLM."""
        pairs = await self._say(agent, "小饼小饼")
        assert pairs and "我在" in pairs[0][0]
        assert not self.llm_calls, "ack must not invoke the LLM"

    async def test_11_bare_with_punctuation(self, agent):
        """TEST.WAKE.11: 「小饼小饼。」 also acks."""
        pairs = await self._say(agent, "小饼小饼。")
        assert pairs and "我在" in pairs[0][0]
        assert not self.llm_calls

    async def test_11_wake_with_content_is_not_ack(self, agent):
        """TEST.WAKE.11: wake word + content flows to the normal chain."""
        await self._say(agent, "小饼小饼，今天天气怎么样")
        assert self.llm_calls == ["小饼小饼，今天天气怎么样"]

    async def test_11_bare_work_wake_keeps_own_ack(self, agent):
        """TEST.WAKE.11: bare 「小饼干活」 keeps the original 好，说吧."""
        import olv.wafer_hooks as wh

        pairs = await self._say(agent, "小饼干活")
        assert any("好，说吧" in p[0] for p in pairs)
        assert wh._session.pending_work is True

    async def test_11_pending_permission_announce_wins(self, agent):
        """TEST.WAKE.11: with a pending permission, the announce is the
        reply (no double '我在呢')."""

        agent._pending_permission = {"question": "想改 out.txt"}
        pairs = await self._say(agent, "小饼小饼")
        text = "".join(p[0] for p in pairs)
        assert "先说一句" in text
        assert "我在" not in text
