"""TEST.CONSOLE.10~13: speaker management API (TASK.CONSOLE.07, 2026-11-07).

GET  /api/speakers         list profiles + enrollment status
POST /api/speakers         enroll from base64 PCM samples
POST /api/speakers/delete  remove embedding + yaml entry

Uses a fake verifier (monkeypatched) so CI never needs the CAM++ model.
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest
import yaml

from core.bus.hub import InProcBus


def _fake_pcm(seconds: float = 2.0) -> str:
    """Loud-enough fake PCM (16k s16) as base64; content is irrelevant
    because the verifier is faked."""
    return base64.b64encode(b"\x10\x00" * int(16000 * seconds)).decode()


class _FakeVerifier:
    def embed(self, pcm: bytes) -> list[float]:
        return [0.1, 0.2, 0.3]


@pytest.fixture
async def speaker_console(tmp_path, monkeypatch):
    import render.console as rc

    emb_dir = tmp_path / "emb"
    cfg = tmp_path / "speakers.yaml"
    cfg.write_text(yaml.safe_dump({
        "voiceprint": {
            "model": str(tmp_path / "model.onnx"),
            "embedding_dir": str(emb_dir),
        },
        "speakers": {
            "master": {"name": "主人", "role": "admin", "priority": 1},
        },
    }, allow_unicode=True), encoding="utf-8")
    (tmp_path / "model.onnx").write_bytes(b"fake-model")

    monkeypatch.setattr(rc, "SPEAKERS_CONFIG", cfg)
    monkeypatch.setattr(rc, "_build_enroll_verifier",
                        lambda setup: _FakeVerifier())

    bus = InProcBus()
    server, bridge = rc.serve(bus, port=0,
                              loop=asyncio.get_running_loop())
    port = server.server_address[1]
    yield rc, cfg, emb_dir, port
    server.shutdown()


class TestSpeakerList:
    async def test_10_list_profiles(self, speaker_console):
        """TEST.CONSOLE.10: GET /api/speakers lists yaml profiles with
        enrollment status (master has no embedding -> enrolled=False)."""
        _, _, _, port = speaker_console
        async with httpx.AsyncClient() as client:
            r = await client.get(f"http://127.0.0.1:{port}/api/speakers")
        assert r.status_code == 200
        data = r.json()
        assert len(data["speakers"]) == 1
        s = data["speakers"][0]
        assert s["speaker_id"] == "master"
        assert s["name"] == "主人"
        assert s["role"] == "admin"
        assert s["priority"] == 1
        assert s["enrolled"] is False


class TestSpeakerEnroll:
    async def test_11_enroll_success(self, speaker_console):
        """TEST.CONSOLE.11: POST /api/speakers enrolls a new speaker:
        embedding file + yaml entry + list shows enrolled=True."""
        _, cfg, emb_dir, port = speaker_console
        async with httpx.AsyncClient() as client:
            r = await client.post(f"http://127.0.0.1:{port}/api/speakers", json={
                "speaker_id": "wife", "name": "妻子",
                "role": "member", "priority": 2,
                "samples": [_fake_pcm(), _fake_pcm(), _fake_pcm()],
            })
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True
        assert data["dim"] == 3
        assert "重启" in data["note"]

        # embedding file written
        emb_file = emb_dir / "wife.json"
        assert emb_file.exists()
        saved = json.loads(emb_file.read_text(encoding="utf-8"))
        assert saved["speaker_id"] == "wife"

        # yaml updated
        raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert raw["speakers"]["wife"]["role"] == "member"
        assert raw["speakers"]["master"]["role"] == "admin"  # intact

        # list reflects it
        async with httpx.AsyncClient() as client:
            r2 = await client.get(f"http://127.0.0.1:{port}/api/speakers")
        by_id = {s["speaker_id"]: s for s in r2.json()["speakers"]}
        assert by_id["wife"]["enrolled"] is True
        assert by_id["master"]["enrolled"] is False

    async def test_12_duplicate_rejected(self, speaker_console):
        """TEST.CONSOLE.12: duplicate enroll without overwrite -> 409;
        with overwrite -> 200."""
        _, _, _, port = speaker_console
        payload = {
            "speaker_id": "wife", "name": "妻子",
            "role": "member", "priority": 2,
            "samples": [_fake_pcm()],
        }
        async with httpx.AsyncClient() as client:
            r1 = await client.post(
                f"http://127.0.0.1:{port}/api/speakers", json=payload)
            assert r1.status_code == 200
            r2 = await client.post(
                f"http://127.0.0.1:{port}/api/speakers", json=payload)
            assert r2.status_code == 409
            assert "已注册" in r2.json()["error"]
            payload["overwrite"] = True
            r3 = await client.post(
                f"http://127.0.0.1:{port}/api/speakers", json=payload)
            assert r3.status_code == 200

    async def test_13_validation_errors(self, speaker_console):
        """TEST.CONSOLE.13: bad input is rejected with 400."""
        _, _, _, port = speaker_console
        cases = [
            {"speaker_id": "Master!", "name": "x",
             "samples": [_fake_pcm()]},          # bad id charset
            {"speaker_id": "ok", "name": "",
             "samples": [_fake_pcm()]},          # empty name
            {"speaker_id": "ok", "name": "x", "role": "boss",
             "samples": [_fake_pcm()]},          # bad role
            {"speaker_id": "ok", "name": "x"},   # no samples
            {"speaker_id": "ok", "name": "x",
             "samples": [base64.b64encode(b"short").decode()]},  # <0.5s
        ]
        async with httpx.AsyncClient() as client:
            for body in cases:
                r = await client.post(
                    f"http://127.0.0.1:{port}/api/speakers", json=body)
                assert r.status_code == 400, (body, r.status_code, r.text)


class TestSpeakerDelete:
    async def test_delete_removes_everything(self, speaker_console):
        """Delete removes the embedding file and the yaml entry."""
        _, cfg, emb_dir, port = speaker_console
        async with httpx.AsyncClient() as client:
            await client.post(f"http://127.0.0.1:{port}/api/speakers", json={
                "speaker_id": "wife", "name": "妻子",
                "role": "member", "priority": 2,
                "samples": [_fake_pcm()],
            })
            r = await client.post(
                f"http://127.0.0.1:{port}/api/speakers/delete",
                json={"speaker_id": "wife"})
        assert r.status_code == 200
        assert r.json()["ok"] is True

        assert not (emb_dir / "wife.json").exists()
        raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        assert "wife" not in raw["speakers"]
        assert "master" in raw["speakers"]  # untouched


class TestDialogueTranscriptApi:
    """TEST.CONSOLE.14~15: GET /api/dialogue (TASK.CONSOLE.08).

    Reads the DIALOG.01 jsonl transcripts; powers the left column of the
    split-layout console.
    """

    @pytest.fixture
    async def dialogue_console(self, tmp_path, monkeypatch):
        # two days of transcripts, today has 2 turns
        import time as _time

        import render.console as rc

        today = _time.strftime("%Y-%m-%d")
        dlg_dir = tmp_path / "dialogue"
        dlg_dir.mkdir()
        (dlg_dir / f"{today}.jsonl").write_text(
            json.dumps({"id": "t1", "ts": f"{today}T10:00:00",
                        "speaker": "master",
                        "asr_text": "小饼小饼，今天天气怎么样", "route": "chat",
                        "emotion": "normal", "reply": "今天天气不错呢",
                        "latency_ms": {"total": 1200, "llm": 300}},
                       ensure_ascii=False) + "\n"
            + json.dumps({"id": "t2", "ts": f"{today}T10:01:00",
                          "speaker": "master",
                          "asr_text": "小饼干活，查一下测试文件", "route": "task_direct",
                          "emotion": "", "reply": "收到，这就开工",
                          "latency_ms": {"total": 150, "llm": 0}},
                         ensure_ascii=False) + "\n",
            encoding="utf-8")
        (dlg_dir / "2020-01-01.jsonl").write_text(
            json.dumps({"ts": "2020-01-01T00:00:00", "asr_text": "old",
                        "reply": "gone"}, ensure_ascii=False) + "\n",
            encoding="utf-8")

        monkeypatch.setattr(rc, "DIALOGUE_DIR", dlg_dir)
        bus = InProcBus()
        server, _bridge = rc.serve(bus, port=0,
                                   loop=asyncio.get_running_loop())
        port = server.server_address[1]
        yield port, today, dlg_dir
        server.shutdown()

    async def test_14_dialogue_today(self, dialogue_console):
        """TEST.CONSOLE.14: default (no date) returns today's turns with
        route/emotion/latency fields intact."""
        port, today, _ = dialogue_console
        async with httpx.AsyncClient() as client:
            r = await client.get(f"http://127.0.0.1:{port}/api/dialogue")
        assert r.status_code == 200
        data = r.json()
        assert data["date"] == today
        assert data["count"] == 2
        assert len(data["entries"]) == 2
        first = data["entries"][0]
        assert first["route"] == "chat"
        assert first["emotion"] == "normal"
        assert first["latency_ms"]["total"] == 1200
        assert "天气" in first["asr_text"]

    async def test_15_dialogue_by_date_and_limit(self, dialogue_console):
        """TEST.CONSOLE.15: explicit date selects that file; limit caps
        the tail; bad limit falls back to the default."""
        port, _, _ = dialogue_console
        async with httpx.AsyncClient() as client:
            r_old = await client.get(
                f"http://127.0.0.1:{port}/api/dialogue?date=2020-01-01")
            assert r_old.json()["count"] == 1
            assert r_old.json()["entries"][0]["asr_text"] == "old"

            r_lim = await client.get(
                f"http://127.0.0.1:{port}/api/dialogue?limit=1")
            assert r_lim.json()["count"] == 2
            assert len(r_lim.json()["entries"]) == 1
            # tail = newest
            assert r_lim.json()["entries"][0]["route"] == "task_direct"

            r_bad = await client.get(
                f"http://127.0.0.1:{port}/api/dialogue?limit=abc")
            assert r_bad.status_code == 200  # falls back, not 500

            r_none = await client.get(
                f"http://127.0.0.1:{port}/api/dialogue?date=1999-09-09")
            assert r_none.json()["count"] == 0
            assert r_none.json()["entries"] == []

    async def test_23_tts_sidecar_merge(self, dialogue_console):
        """TEST.CONSOLE.23: TTS first-audio sidecar merges by turn id."""
        port, today, dlg_dir = dialogue_console
        (dlg_dir / f"{today}.latency.jsonl").write_text(
            json.dumps({"id": "t1", "ts": f"{today}T10:00:01",
                        "latency_ms": {"tts": 420, "total": 1620}},
                       ensure_ascii=False) + "\n",
            encoding="utf-8")
        async with httpx.AsyncClient() as client:
            r = await client.get(f"http://127.0.0.1:{port}/api/dialogue")
        first = r.json()["entries"][0]
        assert first["latency_ms"]["tts"] == 420
        assert first["latency_ms"]["total"] == 1620


class TestGateThresholdsApi:
    @pytest.fixture
    async def gate_console(self, tmp_path, monkeypatch):
        import render.console as rc

        cfg = tmp_path / "speakers.yaml"
        cfg.write_text(yaml.safe_dump({
            "voiceprint": {"model": str(tmp_path / "m.onnx"),
                           "embedding_dir": str(tmp_path / "e")},
            "speakers": {"master": {"name": "主人", "role": "admin",
                                    "priority": 1}},
            "gate": {"t_pass": 0.61, "t_gray": 0.5},
        }, allow_unicode=True), encoding="utf-8")
        monkeypatch.setattr(rc, "SPEAKERS_CONFIG", cfg)
        monkeypatch.setattr(rc, "_GATE_CFG_CACHE", None)
        bus = InProcBus()
        server, _ = rc.serve(bus, port=0, loop=asyncio.get_running_loop())
        port = server.server_address[1]
        yield port
        server.shutdown()

    async def test_17_state_carries_gate_thresholds(self, gate_console):
        """TEST.CONSOLE.17 (D3): /api/state carries gate thresholds so the
        score gauge can draw the gray/pass bands."""
        port = gate_console
        async with httpx.AsyncClient() as client:
            r = await client.get(f"http://127.0.0.1:{port}/api/state")
        assert r.status_code == 200
        data = r.json()
        assert data["gate_thresholds"]["t_pass"] == 0.61
        assert data["gate_thresholds"]["t_gray"] == 0.5

    async def test_17b_thresholds_cache_invalidation(self, gate_console,
                                                     tmp_path, monkeypatch):
        """Editing speakers.yaml is reflected (mtime cache invalidation)."""
        import render.console as rc
        port = gate_console
        async with httpx.AsyncClient() as client:
            r1 = await client.get(f"http://127.0.0.1:{port}/api/state")
        assert r1.json()["gate_thresholds"]["t_pass"] == 0.61
        # rewrite the config with a new threshold (mtime changes)
        cfg = rc.SPEAKERS_CONFIG
        cfg.write_text(yaml.safe_dump({
            "voiceprint": {"model": str(tmp_path / "m.onnx"),
                           "embedding_dir": str(tmp_path / "e")},
            "speakers": {"master": {"name": "主人", "role": "admin",
                                    "priority": 1}},
            "gate": {"t_pass": 0.7, "t_gray": 0.6},
        }, allow_unicode=True), encoding="utf-8")
        async with httpx.AsyncClient() as client:
            r2 = await client.get(f"http://127.0.0.1:{port}/api/state")
        assert r2.json()["gate_thresholds"]["t_pass"] == 0.7


class TestLogTailApi:
    @pytest.fixture
    async def log_console(self, tmp_path, monkeypatch):
        import render.console as rc

        (tmp_path / "olv.log").write_text(
            "\n".join(f"line-{i}" for i in range(300)) + "\n", encoding="utf-8")
        monkeypatch.setitem(rc.LOG_FILES, "olv", str(tmp_path / "olv.log"))
        monkeypatch.setitem(rc.LOG_FILES, "missing", str(tmp_path / "nope.log"))
        bus = InProcBus()
        server, _ = rc.serve(bus, port=0, loop=asyncio.get_running_loop())
        port = server.server_address[1]
        yield port
        server.shutdown()

    async def test_21_log_tail(self, log_console):
        """TEST.CONSOLE.21: /api/logs tails whitelisted files; rejects
        unknown names; missing files degrade to a note."""
        port = log_console
        async with httpx.AsyncClient() as client:
            r = await client.get(f"http://127.0.0.1:{port}/api/logs?name=olv")
            assert r.status_code == 200
            d = r.json()
            assert d["total"] == 300
            assert d["lines"][0] == "line-150"
            assert d["lines"][-1] == "line-299"

            # lines cap
            r2 = await client.get(
                f"http://127.0.0.1:{port}/api/logs?name=olv&lines=5")
            assert len(r2.json()["lines"]) == 5
            assert r2.json()["lines"][-1] == "line-299"

            # unknown source -> 404 (whitelist guard)
            r3 = await client.get(
                f"http://127.0.0.1:{port}/api/logs?name=../../etc/passwd")
            assert r3.status_code == 404

            # not-yet-created log degrades gracefully
            r4 = await client.get(f"http://127.0.0.1:{port}/api/logs?name=missing")
            assert r4.status_code == 200
            assert "尚未生成" in r4.json()["note"]


class TestLatencyAndGaugePage:
    async def test_19_20_debug_panel_structure(self):
        """TEST.CONSOLE.19/20: latency tab + D3 gauge + D4 countdown + A2
        wake status elements present and wired."""
        html = (Path(__file__).parent.parent
                / "render" / "console" / "index.html").read_text(
                    encoding="utf-8")
        # B1/B2 latency tab (fourth tab, view, spark svg, dialogue source)
        assert 'data-rt="latency"' in html
        assert 'id="rt-latency"' in html
        assert 'id="lat_spark"' in html
        assert '/api/dialogue?limit=200' in html
        assert 'P95' in html
        # ruler scales (user feedback rev): thermometer turn bar (fixed
        # span + 2s red line + exceeded-part tint), y-axis sparkline
        assert 'id="lat_thermo"' in html
        assert 'id="btn_asr_device"' in html
        assert "toggleProvider('asr')" in html
        assert 'id="lat_first"' in html
        assert 'firstAudioMs' in html
        assert '近 20 轮首帧延迟' in html
        assert '首帧 ${first}ms' in html
        assert 'SPAN_DEFAULT' in html
        assert 'niceStep' in html
        assert 'text-anchor="end"' in html
        assert '2s 目标' in html
        assert 'tick red' in html
        assert 'id="lat_asr"' in html
        assert 'id="lat_llmfirst"' in html
        assert 'id="lat_llmrest"' in html
        assert 'id="lat_tts"' in html
        assert '>ASR</span>' in html
        assert '>LLM 首字</span>' in html
        assert '>LLM 生成</span>' in html
        assert '>TTS 首音</span>' in html
        assert '<svg id="lat_spark" width="100%" height="96"></svg>' in html, \
            "svg must be attribute-free (viewBox stretching deforms text)"
        assert 'text-anchor="middle">${hhmm}</text>' in html, \
            "x-axis time labels"
        assert 'clientWidth' in html, "pixel-exact svg width"
        # log tab (CONSOLE.11 rev): fifth tab + source picker + tail view
        assert 'data-rt="logs"' in html
        assert 'id="rt-logs"' in html
        assert 'id="log_source"' in html
        assert '/api/logs?name=' in html
        assert 'logSourceChanged' in html
        # quick-control feedback + state highlight (CONSOLE.11 rev)
        assert 'id="ctl_toast"' in html
        assert 'updateCtlButtons' in html
        assert "classList.toggle('ctl-on'" in html
        assert 'id="btn_gate"' in html
        assert '门禁 开' in html
        # Chinese labels for machine-facing values (A11y rev)
        assert "ZH = {" in html
        assert "'会话状态'" in html
        assert "'门禁判定'" in html
        assert "zh('verdict'" in html
        assert '服务健康' in html
        assert '显存占用' in html
        # titles fully localized + human-readable (user report rev3:
        # "会话" is machine-speak; also 14px headers)
        for zh_title in ('<h2>当前会话</h2>', '<h2>当前模式</h2>',
                         '<h2>门禁最近判定</h2>', '<h2>任务状态</h2>',
                         '<h2>当前项目</h2>', '<h2>快捷控制</h2>',
                         '<h2>LED 屏幕预览', '最近一轮耗时', '唤醒状态：'):
            assert zh_title in html, zh_title
        assert '.card h2 { font-size: 14px' in html
        # D3 gate gauge
        assert 'id="gate_gauge"' in html
        assert 'gate_thresholds' in html
        # D4 countdown
        assert 'id="sleep_countdown"' in html
        assert 'tickCountdown' in html
        # A2 wake status
        assert 'id="wake-status"' in html
        assert 'wake_status' in html
        # tab order: events < latency < speakers < debug
        assert html.index('data-rt="events"') < html.index('data-rt="latency"')
        assert (html.index('data-rt="latency"')
                < html.index('data-rt="speakers"'))


class TestSplitLayoutPage:
    async def test_16_split_layout_structure(self):
        """TEST.CONSOLE.16 (D64 update): console page is embed-only stack
        -- LED strip on top, cards/controls/tabbed panels below; the
        transcript panel moved out (OLV chat column shows live chat)."""
        html = (Path(__file__).parent.parent
                / "render" / "console" / "index.html").read_text(
                    encoding="utf-8")
        # layout containers in order: led-strip ... col-main ... meters ...
        # grid ... right-tabs
        i_led = html.index('class="led-strip"')
        i_main = html.index('class="col-main"')
        i_meters = html.index('id="audio-meters"')
        i_grid = html.index('class="grid"')
        i_tabs = html.index('id="right-tabs"')
        assert i_led < i_main < i_meters < i_grid < i_tabs
        # mic meter is browser-API only (arch iron rule 0)
        assert "getUserMedia" in html
        assert "sounddevice" not in html
        # transcript panel is gone (was TASK.CONSOLE.08's left column)
        assert 'id="dialogue-panel"' not in html
        assert "fetchDialogue" not in html
        assert "dlg_list" not in html
        # right column tabs: events default / speakers / debug
        assert html.index('id="rt-events"') < html.index('id="rt-speakers"')
        assert html.index('id="rt-debug"') < html.index("</div><!-- /right-tabs -->")
        assert "rtSwitch('speakers')" in html
        # closing tags balance
        assert html.count("</div><!-- /col-main -->") == 1
        assert html.count("</div><!-- /right-tabs -->") == 1


class TestProviderSwitchApi:
    async def test_24_provider_state_api_and_buttons(self, tmp_path,
                                                     monkeypatch):
        """TEST.PROVIDER.09: GET/POST /api/providers + control buttons."""
        import render.console as rc

        monkeypatch.setenv("WAFER_PROVIDER_STATE",
                           str(tmp_path / "provider_state.json"))
        bus = InProcBus()
        server, _bridge = rc.serve(bus, port=0,
                                   loop=asyncio.get_running_loop())
        port = server.server_address[1]
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"http://127.0.0.1:{port}/api/providers")
                assert r.json()["state"] == {
                    "llm": "online", "tts": "online", "asr": "cpu"}
                r = await client.post(
                    f"http://127.0.0.1:{port}/api/providers",
                    json={"llm": "local", "asr": "gpu"})
                assert r.json()["state"] == {
                    "llm": "local", "tts": "online", "asr": "gpu"}
                r = await client.get(
                    f"http://127.0.0.1:{port}/api/state")
                assert r.json()["providers"]["llm"] == "local"
                assert r.json()["providers"]["asr"] == "gpu"
        finally:
            server.shutdown()
        html = (Path(__file__).parent.parent
                / "render" / "console" / "index.html").read_text(
                    encoding="utf-8")
        assert "btn_llm_provider" in html
        assert "btn_tts_provider" in html
        assert "btn_asr_device" in html
        assert "toggleProvider('llm')" in html
        assert "toggleProvider('asr')" in html
        assert 'id="live2d_model"' in html
        assert "switchLive2DModel" in html
        assert 'value="DeepSeek-chan"' in html

    async def test_25_live2d_model_api(self, tmp_path, monkeypatch):
        """TEST.CONSOLE.25: console proxies model switch to OLV."""
        from types import SimpleNamespace

        import render.console as rc

        calls: list[dict] = []

        def fake_post(url, json, timeout):  # noqa: ANN001
            calls.append({"url": url, "json": json})
            return SimpleNamespace(status_code=200, text='{"ok":true}')

        monkeypatch.setattr(httpx, "post", fake_post)
        bus = InProcBus()
        server, _bridge = rc.serve(bus, port=0,
                                   loop=asyncio.get_running_loop())
        port = server.server_address[1]
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"http://127.0.0.1:{port}/api/live2d-model",
                    json={"model": "Hiyori"})
                assert r.status_code == 200
                assert r.json()["ok"] is True
                r_bad = await client.post(
                    f"http://127.0.0.1:{port}/api/live2d-model",
                    json={"model": "bogus"})
                assert r_bad.status_code == 400
        finally:
            server.shutdown()
        assert calls == [{"url": "http://127.0.0.1:12393/switch-model",
                          "json": {"model_name": "Hiyori"}}]

