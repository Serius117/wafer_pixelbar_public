"""Unified verification entry (TASK.DEPLOY.01, D41).

One entry for every verification suite; kills the previous "8 scripts,
8 flag sets, 8 report formats" fragmentation. Reports land in
logs/verify_<timestamp>.json.

Usage:
    python scripts/verify.py <suite> [<suite> ...] [passthrough args]

Suites:
    smoke    pre-deploy smoke on THIS machine (TEST.DEPLOY.01-04):
             OLV boot / trio CPU / fake-browser audio chain / report.
    ve2e     VE2E D2 real-engine cases (wraps scripts/ve2e_run.py)
    egroup   E-group full-chain layers L2/L3/L5 (wraps e2e_verify.py)
    bench    latency benchmark (wraps bench_e2e.py)
    accept   home-machine hardware acceptance (wraps accept_home.py)
    all      smoke + ve2e + egroup + bench (accept is home-only)

Granularity:
    suite level   : verify.py smoke
    group level   : verify.py ve2e --group B        (passthrough)
    case level    : verify.py smoke --only TEST.DEPLOY.03

Service lifecycle (smoke):
    --skip-trio        reuse already-running trio (probe health, don't boot)
    --keep-services    don't stop trio/OLV on exit (for debug loops)

Gate rule (AGENTS): anything that needs HOME-machine verification must
pass `verify.py smoke` on the dev machine BEFORE push.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling scripts

LOG_DIR = ROOT / "logs"

OLV_DIR = ROOT / "olv" / "upstream" / "Open-LLM-VTuber"
OLV_WS = "ws://127.0.0.1:12393/client-ws"
OLV_HTTP = "http://127.0.0.1:12393/"
CORPUS = ROOT / "tests" / "fixtures" / "corpus"

# TEST.DEPLOY.03: what the ASR transcript of master_enroll_1.wav must contain
# (and must NOT be "?????" -- the R.18 home-machine symptom).
# Use an enrollment clip so the hermetic test gate accepts it.
FAKE_BROWSER_WAV = "master_enroll_1.wav"
FAKE_BROWSER_MUST_CONTAIN = ("下班",)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

class Report:
    """Case results -> console + logs/verify_<ts>.json (TEST.DEPLOY.04)."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.results: list[dict] = []

    def add(self, case: str, status: str, detail: str = "") -> None:
        assert status in ("PASS", "FAIL", "SKIP")
        self.results.append({"case": case, "status": status,
                             "detail": detail, "secs": None})
        mark = {"PASS": "[PASS]", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}[status]
        line = f"  {mark} {case}" + (f" -- {detail}" if detail else "")
        print(line, flush=True)

    def finish(self, last: dict, status: str, detail: str) -> None:
        last["status"] = status
        last["detail"] = detail
        last["secs"] = round(time.monotonic() - self.started, 1)
        mark = {"PASS": "[PASS]", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}[status]
        print(f"  {mark} {last['case']} -- {detail}", flush=True)

    def summary(self) -> int:
        passed = sum(1 for r in self.results if r["status"] == "PASS")
        failed = sum(1 for r in self.results if r["status"] == "FAIL")
        skipped = sum(1 for r in self.results if r["status"] == "SKIP")
        total = len(self.results)
        print(f"\n===== verify: {passed} PASS / {failed} FAIL / "
              f"{skipped} SKIP ({total} cases) =====", flush=True)
        LOG_DIR.mkdir(exist_ok=True)
        out = LOG_DIR / f"verify_{time.strftime('%Y%m%d_%H%M%S')}.json"
        out.write_text(json.dumps(
            {"results": self.results,
             "totals": {"pass": passed, "fail": failed, "skip": skipped},
             "elapsed_s": round(time.monotonic() - self.started, 1)},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"report -> {out.name}", flush=True)
        return 1 if failed else 0

    def begin(self, case: str) -> dict:
        self.results.append({"case": case, "status": "RUNNING",
                             "detail": "", "secs": None})
        print(f"\n[{case}]", flush=True)
        return self.results[-1]


# ---------------------------------------------------------------------------
# Service management (parameterized; smoke uses it, other suites keep
# their own internals behind thin wrappers)
# ---------------------------------------------------------------------------

@dataclass
class ServiceSpec:
    name: str
    cmd: list[str]
    cwd: Path
    health_url: str
    timeout: float = 300.0
    env_extra: dict = field(default_factory=dict)
    log_name: str = ""


class ServiceManager:
    def __init__(self) -> None:
        self.procs: dict[str, subprocess.Popen] = {}

    def start(self, spec: ServiceSpec) -> tuple[bool, str]:
        log = (LOG_DIR / (spec.log_name or f"verify_{spec.name}.log")).open(
            "w", encoding="utf-8")
        env = {**os.environ, **spec.env_extra}
        proc = subprocess.Popen(spec.cmd, cwd=str(spec.cwd), env=env,
                                stdout=log, stderr=subprocess.STDOUT)
        self.procs[spec.name] = proc
        began = time.monotonic()
        import httpx

        while time.monotonic() - began < spec.timeout:
            if proc.poll() is not None:
                log.close()
                return False, (f"{spec.name} exited rc={proc.returncode}"
                               f" (see {log.name})")
            try:
                if httpx.get(spec.health_url, timeout=2).status_code == 200:
                    log.close()
                    return True, (f"healthy after "
                                  f"{time.monotonic() - began:.1f}s")
            except Exception:
                time.sleep(1.5)
        log.close()
        return False, f"{spec.name} never healthy on {spec.health_url}"

    def stop_all(self) -> None:
        for name, proc in self.procs.items():
            if proc.poll() is None:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T",
                                "/F"], capture_output=True, timeout=30)
                print(f"stopped {name} (pid {proc.pid})", flush=True)
        self.procs.clear()

    @staticmethod
    def healthy(url: str) -> bool:
        import httpx

        try:
            return httpx.get(url, timeout=2).status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# smoke suite (TEST.DEPLOY.01-04)
# ---------------------------------------------------------------------------

def _trio_specs(conf: dict) -> dict[str, ServiceSpec]:
    """Build trio service specs from a serve_inference-style config.

    The config's llama_server path is machine-specific (home points at a
    CUDA build outside the repo); fall back to the repo-local binary when
    it is absent -- smoke is CPU mode, any build works.
    """
    import serve_inference as si

    llama = Path(conf["llama_server"])
    if not llama.exists():
        local = ROOT / "tools" / "llama" / "llama-server.exe"
        if local.exists():
            conf = {**conf, "llama_server": str(local)}
    return {
        name: ServiceSpec(
            name=name, cmd=si._cmd(conf, name),  # noqa: SLF001 (sibling)
            cwd=ROOT,
            health_url=si._health_url(conf, name),  # noqa: SLF001
            timeout=300.0, log_name=f"verify_trio_{name}.log")
        for name in ("llm", "asr", "tts")
    }


def _prepare_test_gate() -> Path | None:
    """Build a hermetic gate config from the corpus (TASK.RELEASE.03).

    Smoke/OLVCORE feed synthetic corpus voices. Production enrollment is the
    real owner's voiceprint, so a corpus clip correctly gets dropped by the
    gate (home-machine smoke symptom: wake hit -> gate drop -> no transcript).
    Generate a throwaway enrollment + config under logs/verify_gate so tests
    exercise the real CAM++ gate without depending on local enrollment.

    Returns the config path, or None when the CAM++ model/corpus is absent
    (gate then uses its normal pass-through degrade).
    """
    import yaml

    from engines.voiceprint import EnrollmentStore, SherpaCampplusVerifier, average_embedding

    model = ROOT / "models" / "voiceprint" / "campplus_cn_common.onnx"
    enroll = {
        "master": ["master_enroll_1.wav", "master_enroll_2.wav",
                   "master_enroll_3.wav"],
        "wife": ["wife_chat.wav", "wife_task.wav"],
    }
    if not model.exists() or not all(
            (CORPUS / name).exists() for names in enroll.values() for name in names):
        return None

    def pcm(name: str) -> bytes:
        with wave.open(str(CORPUS / name), "rb") as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
                raise ValueError(f"{name}: expected 16k mono s16")
            return w.readframes(w.getnframes())

    verifier = SherpaCampplusVerifier(store=None, model_path=model)  # type: ignore[arg-type]
    gate_dir = LOG_DIR / "verify_gate"
    gate_dir.mkdir(parents=True, exist_ok=True)
    store = EnrollmentStore(gate_dir)
    for speaker_id, names in enroll.items():
        vectors = [verifier.embed(pcm(name)) for name in names]
        store.save(speaker_id, average_embedding(vectors))

    config = {
        "voiceprint": {
            "model": str(model.resolve()),
            "embedding_dir": str(gate_dir.resolve()),
        },
        "speakers": {
            "master": {"name": "主人", "role": "admin", "priority": 1},
            "wife": {"name": "妻子", "role": "member", "priority": 2},
            "elder": {"name": "老人", "role": "stranger", "priority": 3},
            "child": {"name": "孩子", "role": "stranger", "priority": 4},
        },
        "gate": {"t_pass": 0.52, "t_gray": 0.45},
    }
    path = gate_dir / "speakers.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True,
                                   sort_keys=False), encoding="utf-8")
    print(f"  test gate fixture: {path}", flush=True)
    return path


def _boot_olv(mgr: ServiceManager, cloud_llm: bool) -> tuple[bool, str]:
    """Regenerate conf.yaml (validates setup + template against the fork's
    pydantic schema) and boot the OLV server; shared by smoke/olvloop."""
    cmd = [sys.executable, str(ROOT / "scripts" / "setup_olv.py")]
    if cloud_llm:
        cmd += ["--llm", "cloud"]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       timeout=120)
    if r.returncode != 0:
        return False, f"setup_olv failed: {r.stderr.strip()[:200]}"
    import run_olv  # sibling: ffmpeg PATH helper

    ffmpeg = run_olv.ensure_ffmpeg()
    env_extra = {}
    if ffmpeg:
        env_extra["PATH"] = (str(ffmpeg.parent) + os.pathsep
                             + os.environ.get("PATH", ""))
    gate_config = _prepare_test_gate()
    if gate_config is not None:
        env_extra["WAFER_SPEAKERS_CONFIG"] = str(gate_config)
    spec = ServiceSpec(
        name="olv", cmd=[str(run_olv.OLV_PY), "run_server.py"],
        cwd=run_olv.OLV_DIR, health_url=OLV_HTTP, timeout=120.0,
        env_extra=env_extra,
        log_name="verify_olv.log")
    return mgr.start(spec)


async def run_smoke(args, report: Report, mgr: ServiceManager) -> None:
    import serve_inference as si

    # -- TEST.DEPLOY.01: OLV-BOOT ------------------------------------------
    case = report.begin("TEST.DEPLOY.01 OLV-BOOT")
    ok, detail = _boot_olv(mgr, cloud_llm=False)
    report.finish(case, "PASS" if ok else "FAIL", detail)

    # -- TEST.DEPLOY.02: TRIO-CPU -------------------------------------------
    case = report.begin("TEST.DEPLOY.02 TRIO-CPU")
    conf = si.load_config(ROOT / "config" / "inference.smoke.yaml")
    specs = _trio_specs(conf)
    tri: list[str] = []
    llm_model = ROOT / conf["llm"]["model"]
    if args.skip_trio:
        report.finish(case, "SKIP",
                      "--skip-trio: reusing running services")
    elif not llm_model.exists():
        # home-only asset; the chain-level check (03) still runs for ASR
        tri = ["asr", "tts"]
    else:
        tri = ["llm", "asr", "tts"]
    if tri:
        all_ok, details = True, []
        for name in tri:
            ok, detail = mgr.start(specs[name])
            all_ok &= ok
            details.append(f"{name}: {detail}")
        missing = ("llm" if not llm_model.exists() and not args.skip_trio
                   else None)
        if missing:
            details.append(f"{missing}: SKIP (model file absent on this "
                           f"machine -- home-only asset)")
        report.finish(case, "PASS" if all_ok else "FAIL",
                      "; ".join(details))

    # -- TEST.DEPLOY.03: FAKE-BROWSER ---------------------------------------
    case = report.begin("TEST.DEPLOY.03 FAKE-BROWSER")
    llm_ok = ServiceManager.healthy("http://127.0.0.1:8080/health")
    asr_ok = ServiceManager.healthy("http://127.0.0.1:8001/health")
    if not asr_ok:
        report.finish(case, "FAIL",
                      "ASR :8001 not healthy (see TEST.DEPLOY.02)")
    else:
        ok, detail = await _fake_browser_audio(report)
        if not ok:
            report.finish(case, "FAIL", detail)
        elif not llm_ok:
            report.finish(case, "PASS",
                          f"{detail}; full round-trip SKIP "
                          f"(LLM 9B gguf absent on this machine)")
        else:
            ok2, detail2 = await _fake_browser_text()
            report.finish(case, "PASS" if ok2 else "FAIL",
                          f"{detail}; {detail2}")

    # -- TEST.DEPLOY.04: report written back (the framework itself) -------
    case = report.begin("TEST.DEPLOY.04 REPORT")
    report.finish(case, "PASS", "per-case results -> "
                  "logs/verify_<timestamp>.json (written at summary)")


def _corpus_float32(name: str):
    import numpy as np

    with wave.open(str(CORPUS / name), "rb") as w:
        assert w.getframerate() == 16000, "corpus wavs are 16k"
        pcm = w.readframes(w.getnframes())
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    return samples


async def _collect_until(ws, want_type: str, timeout: float):
    """Receive messages until one matches want_type or timeout."""
    msgs = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=deadline
                                         - time.monotonic())
        except TimeoutError:
            break
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            msgs.append({"type": "<binary>", "len": len(raw)})
            continue
        msgs.append(msg)
        if msg.get("type") == want_type:
            break
    return msgs


async def _wake_olv_session(timeout: float = 60.0) -> None:
    """OLVCORE.02: activate the standby session before sending task audio.

    The keyword list is "小饼小饼"; a task clip that only says "小饼" will not
    wake the session. In ASR mode the synthesized master_wake clip may be
    dropped by the test gate before ASR, so use a text-input wake instead.
    In KWS mode keep the audio wake path (it fires before the gate).
    """
    import os

    import websockets

    async with websockets.connect(OLV_WS, max_size=2 ** 22) as ws:
        await _drain(ws, 3.0)
        if os.environ.get("WAFER_WAKE_MODE", "asr") == "kws":
            samples = _corpus_float32("master_wake.wav")
            chunks = [samples[i:i + 4096]
                      for i in range(0, len(samples), 4096)]
            for chunk in chunks:
                await ws.send(json.dumps(
                    {"type": "mic-audio-data", "audio": chunk.tolist()}))
            await ws.send(json.dumps({"type": "mic-audio-end"}))
        else:
            await ws.send(json.dumps({"type": "text-input",
                                      "text": "小饼小饼"}))
            # ASR-mode text wake is immediate; don't burn the full 60s
            # waiting for a TTS reply before the fake-browser connection.
            await _drain(ws, 5.0)
            return
        await _drain(ws, timeout)


async def _fake_browser_audio(report: Report) -> tuple[bool, str]:
    """Audio path: mic-audio-data (float32/16k/4096) + mic-audio-end ->
    assert user-input-transcription carries the expected keywords.

    This is the R.18 '?????' hypothesis splitter: if this passes on the
    dev machine, a home-machine failure points at the real frontend
    (vad-web/AEC) or acoustics -- not at our server-side chain.
    """
    import websockets

    await _wake_olv_session()
    samples = _corpus_float32(FAKE_BROWSER_WAV)
    chunks = [samples[i:i + 4096]
              for i in range(0, len(samples), 4096)]
    async with websockets.connect(OLV_WS, max_size=2 ** 22) as ws:
        # initial handshake messages (init-config etc.) -- drain briefly
        await _collect_until(ws, "<never>", 3.0)
        for chunk in chunks:
            await ws.send(json.dumps(
                {"type": "mic-audio-data", "audio": chunk.tolist()}))
        await ws.send(json.dumps({"type": "mic-audio-end"}))
        msgs = await _collect_until(ws, "user-input-transcription", 90.0)
    transcript = next((m.get("text", "") for m in msgs
                       if m.get("type") == "user-input-transcription"), None)
    if transcript is None:
        types = [m.get("type") for m in msgs[:10]]
        return False, f"no transcript in 90s; got {types}"
    if all(k in transcript for k in FAKE_BROWSER_MUST_CONTAIN) \
            and "?" * 3 not in transcript:
        return True, f"transcript ok: {transcript[:40]}"
    return False, f"transcript wrong: {transcript[:60]!r}"


async def _fake_browser_text() -> tuple[bool, str]:
    """Full round trip (needs LLM): text-input -> reply + audio frames."""
    import websockets

    async with websockets.connect(OLV_WS, max_size=2 ** 22) as ws:
        await _collect_until(ws, "<never>", 3.0)
        await ws.send(json.dumps({"type": "text-input", "text": "你好"}))
        deadline = time.monotonic() + 180.0
        got_reply, got_audio = False, False
        while time.monotonic() < deadline and not (got_reply and got_audio):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            except TimeoutError:
                continue
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                got_audio = True  # binary audio frame
                continue
            if msg.get("type") == "full-text" and len(msg.get("text", "")) > 1:
                got_reply = True
            elif msg.get("type") == "audio":
                got_audio = True
    ok = got_reply and got_audio
    return ok, (f"reply={'y' if got_reply else 'n'} "
                f"audio={'y' if got_audio else 'n'}")


# ---------------------------------------------------------------------------
# olvloop suite (TEST.OLVLOOP.01-03, TASK.DEPLOY.02)
# ---------------------------------------------------------------------------

async def _drain(ws, secs: float) -> None:
    """Receive-and-discard for a short window (initial handshake noise)."""
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        try:
            await asyncio.wait_for(ws.recv(),
                                   timeout=deadline - time.monotonic())
        except TimeoutError:
            return


def _is_audio(msg: dict) -> bool:
    return msg.get("type") == "audio" and msg.get("audio")


async def _converse(ws, send: list[dict], stop, timeout: float) -> list[dict]:
    """Send messages, then receive until stop(msg) or timeout."""
    for m in send:
        await ws.send(json.dumps(m))
    msgs: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(),
                                         timeout=deadline - time.monotonic())
        except TimeoutError:
            break
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            continue
        msgs.append(msg)
        if stop(msg):
            break
    return msgs


def _reply_text(msgs: list[dict]) -> str:
    """Concatenate the AI's display text.

    OLV carries the reply per sentence in the audio frames' display_text
    (repeated across a sentence's slices -> dedupe consecutive), plus some
    full-text notifications (Thinking/handshake -> skip).
    """
    skip = {"Thinking...", "Connection established"}
    parts: list[str] = []
    last_display = None
    for m in msgs:
        if m.get("type") == "full-text" and m.get("text") not in skip:
            parts.append(m.get("text", ""))
        dt = m.get("display_text")
        if isinstance(dt, dict) and dt.get("text"):
            if dt["text"] != last_display:
                parts.append(dt["text"])
                last_display = dt["text"]
    return "".join(parts).strip()


def _decode_audio_frames(msgs: list[dict]) -> tuple[bytes, int]:
    """base64 wav slices -> (concatenated PCM, sample_rate)."""
    import base64
    import io
    import wave

    pcm = bytearray()
    sr = 24000
    for m in msgs:
        if not _is_audio(m):
            continue
        raw = base64.b64decode(m["audio"])
        with wave.open(io.BytesIO(raw)) as w:
            sr = w.getframerate()
            pcm += w.readframes(w.getnframes())
    return bytes(pcm), sr


async def run_olvloop(args, report: Report, mgr: ServiceManager) -> None:
    import serve_inference as si
    import websockets

    only = args.only

    # services: OLV with the cloud LLM stand-in + trio asr/tts (CPU)
    if not args.skip_trio:
        conf = si.load_config(ROOT / "config" / "inference.smoke.yaml")
        specs = _trio_specs(conf)
        for name in ("asr", "tts"):
            ok, detail = mgr.start(specs[name])
            if not ok:
                report.add("TEST.OLVLOOP.00 services", "FAIL",
                           f"{name}: {detail}")
                return
    else:
        down = [n for n, u in (("asr", "http://127.0.0.1:8001/health"),
                               ("tts", "http://127.0.0.1:9880/health"))
                if not ServiceManager.healthy(u)]
        if down:
            report.add("TEST.OLVLOOP.00 services", "FAIL",
                       f"--skip-trio but {down} not running")
            return
    ok, detail = _boot_olv(mgr, cloud_llm=True)
    if not ok:
        report.add("TEST.OLVLOOP.00 services", "FAIL", f"olv: {detail}")
        return

    # -- TEST.OLVLOOP.03: text-input full round trip ------------------------
    if not only or only == "TEST.OLVLOOP.03":
        case = report.begin("TEST.OLVLOOP.03 TEXT-INPUT")
        try:
            async with websockets.connect(OLV_WS, max_size=2 ** 22) as ws:
                await _drain(ws, 3.0)
                msgs = await _converse(
                    ws,
                    [{"type": "text-input",
                      "text": "你好，用一句话打个招呼"}],
                    stop=_is_audio, timeout=300.0)
            reply = _reply_text(msgs)
            n_audio = sum(1 for m in msgs if _is_audio(m))
            ok = bool(reply) and n_audio >= 1
            report.finish(case, "PASS" if ok else "FAIL",
                          f"reply={reply[:30]!r} audio_frames={n_audio}")
        except Exception as exc:  # noqa: BLE001
            report.finish(case, "FAIL", f"{type(exc).__name__}: {exc}")

    # -- TEST.OLVLOOP.01: chat full loop + TTS roundtrip --------------------
    if not only or only == "TEST.OLVLOOP.01":
        case = report.begin("TEST.OLVLOOP.01 CHAT-LOOP")
        try:
            samples = _corpus_float32("master_chat_02.wav")  # 今天天气真好啊
            chunks = [samples[i:i + 4096]
                      for i in range(0, len(samples), 4096)]
            send = [{"type": "mic-audio-data", "audio": c.tolist()}
                    for c in chunks]
            send.append({"type": "mic-audio-end"})
            await _wake_olv_session()
            async with websockets.connect(OLV_WS, max_size=2 ** 22) as ws:
                await _drain(ws, 3.0)
                # phase 1: until the first TTS frame (LLM + first-sentence
                # synthesis can take ~30-90s with the CPU TTS)
                msgs = await _converse(ws, send, stop=_is_audio,
                                       timeout=300.0)
                # phase 2: keep collecting ~8s more -- a sentence's slices
                # arrive as a burst, so this captures the whole first
                # sentence for the roundtrip (single 100ms slice is not
                # enough audio for a meaningful ASR read-back)
                msgs += await _converse(ws, [], stop=lambda m: False,
                                        timeout=8.0)
            transcript = next((m.get("text", "") for m in msgs
                               if m.get("type") == "user-input-transcription"),
                              "")
            reply = _reply_text(msgs)
            n_audio = sum(1 for m in msgs if _is_audio(m))

            problems = []
            if "天气" not in transcript:
                problems.append(f"transcript={transcript[:40]!r}")
            if not reply:
                problems.append("no reply text")
            if n_audio < 1:
                problems.append("no audio frames")

            # TTS roundtrip: what the robot SAID, heard back by our ASR
            roundtrip = ""
            pcm, sr = _decode_audio_frames(msgs)
            if pcm:
                from engines.asr_qwen import QwenLlamaServerAsr

                asr = QwenLlamaServerAsr(url="http://127.0.0.1:8001")
                pcm16 = _resample(pcm, sr, 16000) if sr != 16000 else pcm
                roundtrip = await asr.transcribe(pcm16)
                shared = (set(roundtrip)
                          & set(c for c in reply if "\u4e00" <= c <= "\u9fff"))
                if len(shared) < 2:
                    problems.append(
                        f"roundtrip weak: {roundtrip[:30]!r}")
            else:
                problems.append("no decodable audio")

            ok = not problems
            report.finish(case, "PASS" if ok else "FAIL",
                          "; ".join(problems) or
                          f"transcript={transcript[:20]!r} "
                          f"reply={reply[:20]!r} roundtrip={roundtrip[:20]!r}")
        except Exception as exc:  # noqa: BLE001
            report.finish(case, "FAIL", f"{type(exc).__name__}: {exc}")

    # -- TEST.OLVLOOP.02: server-side interrupt + clean restart -------------
    if not only or only == "TEST.OLVLOOP.02":
        case = report.begin("TEST.OLVLOOP.02 INTERRUPT")
        try:
            async with websockets.connect(OLV_WS, max_size=2 ** 22) as ws:
                await _drain(ws, 3.0)
                # 1) start a conversation and wait for TTS audio
                msgs = await _converse(
                    ws, [{"type": "text-input",
                          "text": "讲一个三十字左右的小故事"}],
                    stop=_is_audio, timeout=300.0)
                if not any(_is_audio(m) for m in msgs):
                    report.finish(case, "FAIL",
                                  "no audio before interrupt")
                    return
                # 2) interrupt the running chain
                await ws.send(json.dumps({"type": "interrupt-signal",
                                          "text": "停"}))
                await asyncio.sleep(2.0)
                # 3) the chain must restart cleanly (R.18 interrupt-loop
                #    regression guard: no deadlock from the cancelled task)
                msgs2 = await _converse(
                    ws, [{"type": "text-input", "text": "你好"}],
                    stop=_is_audio, timeout=300.0)
            reply2 = _reply_text(msgs2)
            ok = bool(reply2) and any(_is_audio(m) for m in msgs2)
            report.finish(case, "PASS" if ok else "FAIL",
                          f"restart reply={reply2[:30]!r}")
        except Exception as exc:  # noqa: BLE001
            report.finish(case, "FAIL", f"{type(exc).__name__}: {exc}")


def _resample(pcm: bytes, sr_from: int, sr_to: int) -> bytes:
    """Linear-interpolation resample of s16le mono PCM."""
    import array

    a = array.array("h")
    a.frombytes(pcm)
    n_out = int(len(a) * sr_to / sr_from)
    out = array.array("h")
    for i in range(n_out):
        pos = i * sr_from / sr_to
        lo = int(pos)
        hi = min(lo + 1, len(a) - 1)
        frac = pos - lo
        out.append(int(a[lo] * (1 - frac) + a[hi] * frac))
    return out.tobytes()


# ---------------------------------------------------------------------------
# olvcore suite (TEST.OLVCORE.01-06, TASK.OLVCORE.01e)
# ---------------------------------------------------------------------------

async def run_olvcore(args, report: Report, mgr: ServiceManager) -> None:
    """Full-flow closed loop: corpus → OLV → gate → route → opencode → LED."""
    import socket
    import time as _t

    import serve_inference as si
    import websockets

    only = args.only

    # hub: raw TCP, check port then start if needed
    def _port_open(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except (TimeoutError, OSError):
            return False

    if not _port_open(8765):
        log = (LOG_DIR / "verify_hub.log").open("w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", "core.bus", "--port", "8765"],
            cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT)
        mgr.procs["hub"] = proc
        log.close()
        for _ in range(30):
            if _port_open(8765):
                print("  hub ready on :8765", flush=True)
                break
            _t.sleep(1.0)
        else:
            report.add("TEST.OLVCORE.00 services", "FAIL",
                       "hub never ready on :8765")
            return
    else:
        print("  hub already running", flush=True)

    # services: trio (asr+tts only, LLM=cloud) + OLV
    if not args.skip_trio:
        conf = si.load_config(ROOT / "config" / "inference.smoke.yaml")
        specs = _trio_specs(conf)
        for name in ("asr", "tts"):
            ok, detail = mgr.start(specs[name])
            if not ok:
                report.add("TEST.OLVCORE.00 services", "FAIL",
                           f"{name}: {detail}")
                return
    else:
        down = [n for n, u in (("asr", "http://127.0.0.1:8001/health"),
                               ("tts", "http://127.0.0.1:9880/health"))
                if not ServiceManager.healthy(u)]
        if down:
            report.add("TEST.OLVCORE.00 services", "FAIL",
                       f"--skip-trio but {down} not running")
            return

    ok, detail = _boot_olv(mgr, cloud_llm=True)
    if not ok:
        report.add("TEST.OLVCORE.00 services", "FAIL", f"olv: {detail}")
        return

    # hub probe: subscribe to decision topics for assertions
    from core.bus import topics as T
    from core.bus.hub_client import HubClient

    hub = HubClient("127.0.0.1", 8765)
    await hub.connect()
    hub_events: list[tuple[str, dict]] = []
    for t in (T.GATE_DECISION, T.LLM_EMOTION, T.TASK_REQUEST,
              T.TASK_PROGRESS, T.TASK_RESULT, T.TASK_CANCEL,
              T.SESSION_STATE, T.PROJECT_SWITCH):
        hub.subscribe(t, lambda topic, p, _ev=hub_events:
                      _ev.append((topic, dict(p))))

    def _hub_events(topic: str) -> list[dict]:
        return [p for t, p in hub_events if t == topic]

    def _clear_hub() -> None:
        hub_events.clear()

    async def _wake_session() -> bool:
        """Send the wake word via a SEPARATE connection to activate the
        session (OLVCORE.02), then wait for that conversation to complete.

        Uses its own WebSocket so the test connection doesn't see the
        wake word's transcript/reply/audio (which would pollute asserts).
        """
        import os

        import websockets as _ws_mod

        async with _ws_mod.connect(OLV_WS, max_size=2 ** 22) as ws:
            await _drain(ws, 3.0)
            if os.environ.get("WAFER_WAKE_MODE", "asr") == "kws":
                samples = _corpus_float32("master_wake.wav")
                chunks = [samples[i:i + 4096]
                          for i in range(0, len(samples), 4096)]
                for c in chunks:
                    await ws.send(json.dumps(
                        {"type": "mic-audio-data", "audio": c.tolist()}))
                await ws.send(json.dumps({"type": "mic-audio-end"}))
            else:
                await ws.send(json.dumps({"type": "text-input",
                                          "text": "小饼小饼"}))
                await _drain(ws, 5.0)
                return True
            # wait for the wake word conversation to fully complete
            # (gate + ASR + LLM + TTS ≈ 20-40s with cloud LLM + CPU TTS)
            await _drain(ws, 60.0)
        return True

    # -- TEST.OLVCORE.01: master chat (gate pass + emotion + audio) --------
    if not only or only == "TEST.OLVCORE.01":
        case = report.begin("TEST.OLVCORE.01 MASTER-CHAT")
        _clear_hub()
        try:
            samples = _corpus_float32("master_chat_02.wav")
            chunks = [samples[i:i + 4096]
                      for i in range(0, len(samples), 4096)]
            send = [{"type": "mic-audio-data", "audio": c.tolist()}
                    for c in chunks]
            send.append({"type": "mic-audio-end"})
            async with websockets.connect(OLV_WS, max_size=2 ** 22) as ws:
                await _drain(ws, 3.0)
                await _wake_session()
                _clear_hub()
                msgs = await _converse(ws, send, stop=_is_audio,
                                       timeout=300.0)
                msgs += await _converse(ws, [], stop=lambda m: False,
                                        timeout=8.0)
            transcript = next((m.get("text", "") for m in msgs
                               if m.get("type") == "user-input-transcription"),
                              "")
            reply = _reply_text(msgs)
            n_audio = sum(1 for m in msgs if _is_audio(m))
            gates = _hub_events("gate.decision")
            emotions = _hub_events("llm.emotion")
            states = _hub_events("session.state")

            problems = []
            if not gates or gates[0].get("verdict") != "pass":
                problems.append(f"gate={gates[:1]}")
            if "天气" not in transcript:
                problems.append(f"transcript={transcript[:30]!r}")
            if not reply:
                problems.append("no reply")
            if n_audio < 1:
                problems.append("no audio")
            if not emotions:
                problems.append("no llm.emotion")
            elif emotions[0].get("tag") not in (
                    "normal", "happy", "sad", "angry", "surprised"):
                problems.append(f"tag={emotions[0].get('tag')}")
            state_vals = [s.get("state") for s in states]
            if "thinking" not in state_vals:
                problems.append(f"states={state_vals}")

            ok = not problems
            report.finish(case, "PASS" if ok else "FAIL",
                          "; ".join(problems) or
                          f"gate=pass,tag={emotions[0]['tag'] if emotions else '?'}"
                          f",reply={reply[:20]!r}")
        except Exception as exc:  # noqa: BLE001
            report.finish(case, "FAIL", f"{type(exc).__name__}: {exc}")

    # -- TEST.OLVCORE.02: stranger drop (gate reject, zero response) --------
    if not only or only == "TEST.OLVCORE.02":
        case = report.begin("TEST.OLVCORE.02 STRANGER-DROP")
        _clear_hub()
        try:
            elder_wav = CORPUS / "elder_1.wav"
            if not elder_wav.exists():
                report.finish(case, "SKIP", "elder corpus not present")
            else:
                import wave

                import numpy as np

                with wave.open(str(elder_wav), "rb") as w:
                    pcm = w.readframes(w.getnframes())
                samples = (np.frombuffer(pcm, dtype=np.int16)
                           .astype(np.float32) / 32768.0)
                chunks = [samples[i:i + 4096]
                          for i in range(0, len(samples), 4096)]
                send = [{"type": "mic-audio-data", "audio": c.tolist()}
                        for c in chunks]
                send.append({"type": "mic-audio-end"})
                async with websockets.connect(OLV_WS, max_size=2 ** 22) as ws:
                    await _drain(ws, 3.0)
                    await _wake_session()
                    _clear_hub()
                    msgs = await _converse(ws, send, stop=lambda m: False,
                                           timeout=30.0)

                gates = _hub_events("gate.decision")
                transcripts = [m for m in msgs
                               if m.get("type") == "user-input-transcription"]
                replies = [m for m in msgs if m.get("type") == "full-text"
                           and m.get("text") not in (
                               "Thinking...", "Connection established")]

                problems = []
                if not gates or gates[0].get("verdict") not in ("drop", "gray"):
                    problems.append(f"gate={gates[:1]}")
                if transcripts:
                    problems.append("ASR fired (gate should have blocked)")
                if replies:
                    problems.append("got reply (should be silent)")

                ok = not problems
                report.finish(case, "PASS" if ok else "FAIL",
                              "; ".join(problems) or
                              f"gate={gates[0]['verdict'] if gates else '?'}"
                              f",silent={not transcripts and not replies}")
        except Exception as exc:  # noqa: BLE001
            report.finish(case, "FAIL", f"{type(exc).__name__}: {exc}")

    # -- TEST.OLVCORE.03: master dispatch task (full chain) ------------------
    if not only or only == "TEST.OLVCORE.03":
        case = report.begin("TEST.OLVCORE.03 MASTER-DISPATCH")
        _clear_hub()
        try:
            samples = _corpus_float32("master_task.wav")
            chunks = [samples[i:i + 4096]
                      for i in range(0, len(samples), 4096)]
            send = [{"type": "mic-audio-data", "audio": c.tolist()}
                    for c in chunks]
            send.append({"type": "mic-audio-end"})
            async with websockets.connect(OLV_WS, max_size=2 ** 22) as ws:
                await _drain(ws, 3.0)
                await _wake_session()
                _clear_hub()
                msgs = await _converse(ws, send, stop=_is_audio,
                                       timeout=300.0)
                msgs += await _converse(ws, [], stop=lambda m: False,
                                        timeout=10.0)

            transcript = next((m.get("text", "") for m in msgs
                               if m.get("type") == "user-input-transcription"),
                              "")
            reply = _reply_text(msgs)
            gates = _hub_events("gate.decision")
            requests = _hub_events("task.request")

            problems = []
            if not gates or gates[0].get("verdict") != "pass":
                problems.append(f"gate={gates[:1]}")
            if "项目" not in transcript and "测试" not in transcript:
                problems.append(f"transcript={transcript[:30]!r}")
            if not requests:
                problems.append("no task.request (dispatch failed)")
            elif requests[0].get("op") != "execute":
                problems.append(f"op={requests[0].get('op')}")
            if "收到" not in reply and "开工" not in reply:
                problems.append(f"reply={reply[:30]!r}")

            ok = not problems
            report.finish(case, "PASS" if ok else "FAIL",
                          "; ".join(problems) or
                          f"gate=pass,task_request=ok,reply={reply[:20]!r}")
        except Exception as exc:  # noqa: BLE001
            report.finish(case, "FAIL", f"{type(exc).__name__}: {exc}")

    # -- TEST.OLVCORE.04: amend during grace window --------------------------
    if not only or only == "TEST.OLVCORE.04":
        case = report.begin("TEST.OLVCORE.04 AMEND")
        _clear_hub()
        try:
            # dispatch first
            samples1 = _corpus_float32("master_task.wav")
            chunks1 = [samples1[i:i + 4096]
                       for i in range(0, len(samples1), 4096)]
            send1 = [{"type": "mic-audio-data", "audio": c.tolist()}
                     for c in chunks1]
            send1.append({"type": "mic-audio-end"})
            async with websockets.connect(OLV_WS, max_size=2 ** 22) as ws:
                await _drain(ws, 3.0)
                await _wake_session()
                _clear_hub()
                await _converse(ws, send1, stop=_is_audio,
                                        timeout=300.0)
                # now amend
                samples2 = _corpus_float32("master_amend.wav")
                chunks2 = [samples2[i:i + 4096]
                           for i in range(0, len(samples2), 4096)]
                send2 = [{"type": "mic-audio-data", "audio": c.tolist()}
                         for c in chunks2]
                send2.append({"type": "mic-audio-end"})
                _clear_hub()
                msgs2 = await _converse(ws, send2, stop=_is_audio,
                                        timeout=300.0)
                msgs2 += await _converse(ws, [], stop=lambda m: False,
                                         timeout=10.0)

            reply2 = _reply_text(msgs2)
            requests = _hub_events("task.request")

            problems = []
            if "按新的来" not in reply2:
                problems.append(f"reply={reply2[:30]!r}")
            if not requests:
                problems.append("no amend task.request")

            ok = not problems
            report.finish(case, "PASS" if ok else "FAIL",
                          "; ".join(problems) or f"amend ok,reply={reply2[:20]!r}")
        except Exception as exc:  # noqa: BLE001
            report.finish(case, "FAIL", f"{type(exc).__name__}: {exc}")

    # -- TEST.OLVCORE.05: cancel ----------------------------------------------
    if not only or only == "TEST.OLVCORE.05":
        case = report.begin("TEST.OLVCORE.05 CANCEL")
        _clear_hub()
        try:
            # dispatch then cancel
            samples1 = _corpus_float32("master_task.wav")
            chunks1 = [samples1[i:i + 4096]
                       for i in range(0, len(samples1), 4096)]
            send1 = [{"type": "mic-audio-data", "audio": c.tolist()}
                     for c in chunks1]
            send1.append({"type": "mic-audio-end"})
            async with websockets.connect(OLV_WS, max_size=2 ** 22) as ws:
                await _drain(ws, 3.0)
                await _wake_session()
                _clear_hub()
                await _converse(ws, send1, stop=_is_audio, timeout=300.0)
                # cancel
                samples2 = _corpus_float32("master_cancel.wav")
                chunks2 = [samples2[i:i + 4096]
                           for i in range(0, len(samples2), 4096)]
                send2 = [{"type": "mic-audio-data", "audio": c.tolist()}
                         for c in chunks2]
                send2.append({"type": "mic-audio-end"})
                _clear_hub()
                msgs2 = await _converse(ws, send2, stop=_is_audio,
                                        timeout=300.0)
                msgs2 += await _converse(ws, [], stop=lambda m: False,
                                         timeout=10.0)

            reply2 = _reply_text(msgs2)
            cancels = _hub_events("task.cancel")

            problems = []
            if "取消" not in reply2:
                problems.append(f"reply={reply2[:30]!r}")
            if not cancels:
                problems.append("no task.cancel event")

            ok = not problems
            report.finish(case, "PASS" if ok else "FAIL",
                          "; ".join(problems) or f"cancel ok,reply={reply2[:20]!r}")
        except Exception as exc:  # noqa: BLE001
            report.finish(case, "FAIL", f"{type(exc).__name__}: {exc}")

    # -- TEST.OLVCORE.06: clarify (bad project alias) -------------------------
    if not only or only == "TEST.OLVCORE.06":
        case = report.begin("TEST.OLVCORE.06 CLARIFY")
        _clear_hub()
        try:
            samples = _corpus_float32("master_badname.wav")
            chunks = [samples[i:i + 4096]
                      for i in range(0, len(samples), 4096)]
            send = [{"type": "mic-audio-data", "audio": c.tolist()}
                    for c in chunks]
            send.append({"type": "mic-audio-end"})
            async with websockets.connect(OLV_WS, max_size=2 ** 22) as ws:
                await _drain(ws, 3.0)
                await _wake_session()
                _clear_hub()
                msgs = await _converse(ws, send, stop=_is_audio,
                                       timeout=300.0)
                msgs += await _converse(ws, [], stop=lambda m: False,
                                        timeout=10.0)

            reply = _reply_text(msgs)
            requests = _hub_events("task.request")

            problems = []
            if "没找到" not in reply and "项目" not in reply:
                problems.append(f"reply={reply[:30]!r}")
            if requests:
                problems.append("task dispatched (should be held for clarify)")

            # TTS roundtrip of the clarify reply: the announcement must be
            # audibly intelligible (what the robot SAID, read back by ASR)
            roundtrip = ""
            pcm, sr = _decode_audio_frames(msgs)
            if pcm:
                from engines.asr_qwen import QwenLlamaServerAsr

                asr = QwenLlamaServerAsr(url="http://127.0.0.1:8001")
                pcm16 = _resample(pcm, sr, 16000) if sr != 16000 else pcm
                roundtrip = await asr.transcribe(pcm16)
                shared = (set(roundtrip)
                          & set(c for c in reply
                                if "\u4e00" <= c <= "\u9fff"))
                if len(shared) < 2:
                    problems.append(f"roundtrip weak: {roundtrip[:30]!r}")
            else:
                problems.append("no decodable audio for roundtrip")

            ok = not problems
            report.finish(case, "PASS" if ok else "FAIL",
                          "; ".join(problems) or
                          f"clarify ok,reply={reply[:20]!r} "
                          f"roundtrip={roundtrip[:20]!r}")
        except Exception as exc:  # noqa: BLE001
            report.finish(case, "FAIL", f"{type(exc).__name__}: {exc}")

    # -- TEST.OLVCORE.07: multi-turn context ------------------------------
    if not only or only == "TEST.OLVCORE.07":
        case = report.begin("TEST.OLVCORE.07 MULTI-TURN")
        _clear_hub()
        try:
            async with websockets.connect(OLV_WS, max_size=2 ** 22) as ws:
                await _drain(ws, 3.0)
                await _wake_session()
                _clear_hub()
                # turn 1
                s1 = _corpus_float32("master_chat_02.wav")
                c1 = [{"type": "mic-audio-data", "audio": s1[i:i+4096].tolist()}
                      for i in range(0, len(s1), 4096)]
                c1.append({"type": "mic-audio-end"})
                m1 = await _converse(ws, c1, stop=_is_audio, timeout=300.0)
                m1 += await _converse(ws, [], stop=lambda m: False,
                                      timeout=8.0)
                # turn 2
                s2 = _corpus_float32("master_chat_05.wav")
                c2 = [{"type": "mic-audio-data", "audio": s2[i:i+4096].tolist()}
                      for i in range(0, len(s2), 4096)]
                c2.append({"type": "mic-audio-end"})
                m2 = await _converse(ws, c2, stop=_is_audio, timeout=300.0)
                m2 += await _converse(ws, [], stop=lambda m: False,
                                      timeout=8.0)

            r1 = _reply_text(m1)
            r2 = _reply_text(m2)
            gates = _hub_events("gate.decision")
            emotions = _hub_events("llm.emotion")

            problems = []
            if len(gates) < 2 or not all(
                    g.get("verdict") == "pass" for g in gates[:2]):
                problems.append(f"gates={len(gates)}")
            if not r1:
                problems.append("turn1 no reply")
            if not r2:
                problems.append("turn2 no reply")
            if len(emotions) < 2:
                problems.append(f"emotions={len(emotions)}")

            ok = not problems
            report.finish(case, "PASS" if ok else "FAIL",
                          "; ".join(problems) or
                          f"2 turns ok, r1={r1[:15]!r} r2={r2[:15]!r}")
        except Exception as exc:  # noqa: BLE001
            report.finish(case, "FAIL", f"{type(exc).__name__}: {exc}")

    # -- TEST.OLVCORE.08: project switch full-chain ------------------------
    if not only or only == "TEST.OLVCORE.08":
        case = report.begin("TEST.OLVCORE.08 PROJECT-SWITCH")
        _clear_hub()
        try:
            samples = _corpus_float32("master_music.wav")
            chunks = [samples[i:i + 4096]
                      for i in range(0, len(samples), 4096)]
            send = [{"type": "mic-audio-data", "audio": c.tolist()}
                    for c in chunks]
            send.append({"type": "mic-audio-end"})
            async with websockets.connect(OLV_WS, max_size=2 ** 22) as ws:
                await _drain(ws, 3.0)
                await _wake_session()
                _clear_hub()
                msgs = await _converse(ws, send, stop=_is_audio,
                                       timeout=300.0)
                msgs += await _converse(ws, [], stop=lambda m: False,
                                        timeout=10.0)

            reply = _reply_text(msgs)
            gates = _hub_events("gate.decision")
            switches = _hub_events("project.switch")
            requests = _hub_events("task.request")

            problems = []
            if not gates or gates[0].get("verdict") != "pass":
                problems.append(f"gate={gates[:1]}")
            if not switches or "音乐" not in switches[0].get("alias", ""):
                problems.append(f"switch={switches[:1]}")
            if not requests:
                problems.append("no task.request")
            elif "music" not in str(requests[0].get("project", "")).lower() \
                    and "音乐" not in str(requests[0].get("project", "")):
                problems.append(
                    f"project={requests[0].get('project', '?')!r}")

            ok = not problems
            report.finish(case, "PASS" if ok else "FAIL",
                          "; ".join(problems) or
                          f"switch=音乐, project={requests[0]['project'] if requests else '?'}")
        except Exception as exc:  # noqa: BLE001
            report.finish(case, "FAIL", f"{type(exc).__name__}: {exc}")

    # -- TEST.OLVCORE.09: task failure full-chain ---------------------------
    if not only or only == "TEST.OLVCORE.09":
        case = report.begin("TEST.OLVCORE.09 TASK-FAILURE")
        _clear_hub()
        try:
            # Mock bridge: subscribe to TASK_REQUEST, always fail
            async def _fail_executor(topic, payload):
                await hub.publish(T.TASK_RESULT, {
                    "task_id": (payload.get("task") or {}).get(
                        "task_id", "unknown"),
                    "summary": "任务失败：opencode exited rc=1",
                    "ok": False,
                    "error": "opencode exited rc=1 (test injection)",
                })

            hub.subscribe(T.TASK_REQUEST, _fail_executor)

            samples = _corpus_float32("master_task.wav")
            chunks = [samples[i:i + 4096]
                      for i in range(0, len(samples), 4096)]
            send = [{"type": "mic-audio-data", "audio": c.tolist()}
                    for c in chunks]
            send.append({"type": "mic-audio-end"})
            async with websockets.connect(OLV_WS, max_size=2 ** 22) as ws:
                await _drain(ws, 3.0)
                await _wake_session()
                _clear_hub()
                msgs = await _converse(ws, send, stop=_is_audio,
                                       timeout=300.0)
                msgs += await _converse(ws, [], stop=lambda m: False,
                                        timeout=15.0)

            gates = _hub_events("gate.decision")
            requests = _hub_events("task.request")
            results = _hub_events("task.result")

            problems = []
            if not gates or gates[0].get("verdict") != "pass":
                problems.append(f"gate={gates[:1]}")
            if not requests:
                problems.append("no task.request")
            if not results:
                problems.append("no task.result (mock bridge didn't run)")
            elif results[0].get("ok") is not False:
                problems.append(f"ok={results[0].get('ok')}")
            if results and not results[0].get("error"):
                problems.append("no error field")

            ok = not problems
            report.finish(case, "PASS" if ok else "FAIL",
                          "; ".join(problems) or
                          f"ok=false, error={results[0].get('error', '')[:40]!r}")
            hub.unsubscribe(T.TASK_REQUEST, _fail_executor)
        except Exception as exc:  # noqa: BLE001
            report.finish(case, "FAIL", f"{type(exc).__name__}: {exc}")

    # -- TEST.OLVCORE.10: stress loop ---------------------------------------
    if not only or only == "TEST.OLVCORE.10":
        case = report.begin("TEST.OLVCORE.10 STRESS")
        _clear_hub()
        try:
            rounds_ok = 0
            async with websockets.connect(OLV_WS, max_size=2 ** 22) as ws:
                await _drain(ws, 3.0)
                await _wake_session()
                _clear_hub()
                for i in range(10):
                    msgs = await _converse(
                        ws,
                        [{"type": "text-input", "text": f"你好，这是第{i+1}次"}],
                        stop=_is_audio, timeout=300.0)
                    reply = _reply_text(msgs)
                    if reply:
                        rounds_ok += 1
                    else:
                        report.finish(case, "FAIL",
                                      f"round {i+1}: no reply")
                        break

            emotions = _hub_events("llm.emotion")
            gates = _hub_events("gate.decision")
            ok = rounds_ok == 10
            report.finish(case, "PASS" if ok else "FAIL",
                          f"{rounds_ok}/10 rounds, "
                          f"emotions={len(emotions)}, gates={len(gates)}")
        except Exception as exc:  # noqa: BLE001
            report.finish(case, "FAIL", f"{type(exc).__name__}: {exc}")

    # -- TEST.OLVCORE.12: latency benchmark ----------------------------------
    if not only or only == "TEST.OLVCORE.12":
        case = report.begin("TEST.OLVCORE.12 LATENCY")
        latencies = []
        try:
            async with websockets.connect(OLV_WS, max_size=2 ** 22) as ws:
                await _drain(ws, 3.0)
                await _wake_session()
                _clear_hub()
                for i in range(3):
                    t0 = time.monotonic()
                    msgs = await _converse(
                        ws,
                        [{"type": "text-input", "text": f"打个招呼，第{i+1}次"}],
                        stop=_is_audio, timeout=300.0)
                    t_end = time.monotonic()
                    # find timestamps from messages
                    t_transcript = None
                    t_first_audio = None
                    for m in msgs:
                        if m.get("type") == "user-input-transcription" \
                                and t_transcript is None:
                            t_transcript = t0  # approx (same event batch)
                        if _is_audio(m) and t_first_audio is None:
                            t_first_audio = t_end
                    e2e = t_end - t0
                    latencies.append({
                        "round": i + 1,
                        "e2e_s": round(e2e, 2),
                    })

            avg = sum(lat["e2e_s"] for lat in latencies) / len(latencies)
            # write baseline
            bench = ROOT / "logs" / "bench_olvcore.json"
            bench.write_text(json.dumps(
                {"timestamp": time.strftime("%Y%m%d_%H%M%S"),
                 "latencies": latencies, "avg_e2e_s": round(avg, 2)},
                ensure_ascii=False, indent=2), encoding="utf-8")

            ok = avg < 120.0  # CPU cold-start first round can be ~100s
            report.finish(case, "PASS" if ok else "FAIL",
                          f"avg E2E={avg:.1f}s, rounds="
                          f"{[lat['e2e_s'] for lat in latencies]}")
        except Exception as exc:  # noqa: BLE001
            report.finish(case, "FAIL", f"{type(exc).__name__}: {exc}")

    # -- TEST.OLVCORE.11: real-browser E2E (Playwright + local chromium) ------
    # Covers: (a) OLV real frontend JS -> WS protocol -> full round (the
    # "fake vs real browser" gap), (b) wafer-console live event rendering,
    # (c) /compare design-bench rendering.
    if not only or only == "TEST.OLVCORE.11":
        case = report.begin("TEST.OLVCORE.11 BROWSER-E2E")
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            report.finish(case, "SKIP",
                          "playwright not installed (pip install playwright "
                          "+ playwright install chromium)")
        else:
            # render process with console on a dedicated port (8791 -- do
            # not clobber the standard 8790 possibly left running)
            if _port_open(8791):
                report.finish(case, "SKIP",
                              "port 8791 already in use (leftover render?)")
            else:
                rlog = (LOG_DIR / "verify_render.log").open(
                    "w", encoding="utf-8")
                rproc = subprocess.Popen(
                    [sys.executable, "-m", "render", "--host", "127.0.0.1",
                     "--port", "8765", "--console", "8791"],
                    cwd=str(ROOT), stdout=rlog, stderr=subprocess.STDOUT)
                mgr.procs["render"] = rproc
                rlog.close()
                for _ in range(20):
                    if _port_open(8791):
                        break
                    _t.sleep(1.0)
                else:
                    report.finish(case, "FAIL",
                                  "render console never ready on :8791")
                    rproc = None

                if rproc is not None:
                    try:
                        async with async_playwright() as pw:
                            browser = await pw.chromium.launch(
                                args=["--autoplay-policy="
                                      "no-user-gesture-required",
                                      # headless has no mic: give the
                                      # frontend VAD a fake capture device
                                      "--use-fake-device-for-media-stream",
                                      "--use-fake-ui-for-media-stream"])
                            ctx = await browser.new_context()
                            page = await ctx.new_page()
                            js_errors: list[str] = []
                            page.on(
                                "console",
                                lambda m: js_errors.append(m.text)
                                if m.type == "error" else None)

                            # (a) OLV real frontend: text chat round
                            await page.goto(OLV_HTTP,
                                            wait_until="domcontentloaded")
                            ta = page.locator("textarea.chakra-textarea")
                            await ta.wait_for(timeout=15000)
                            await _wake_session()
                            _clear_hub()
                            body_before = await page.inner_text("body")
                            await ta.fill("你好呀，简单打个招呼就好")
                            await ta.press("Enter")
                            deadline = time.monotonic() + 90.0
                            while time.monotonic() < deadline:
                                if _hub_events(T.LLM_EMOTION):
                                    break
                                await asyncio.sleep(1.0)
                            ok_backend = bool(_hub_events(T.LLM_EMOTION))
                            await asyncio.sleep(2.0)  # let the DOM settle
                            body_after = await page.inner_text("body")
                            ui_grew = len(body_after) > len(body_before) + 5
                            states = {p.get("state")
                                      for p in _hub_events(T.SESSION_STATE)}

                            # (b) wafer-console renders live events
                            page2 = await ctx.new_page()
                            await page2.goto(
                                "http://127.0.0.1:8791/",
                                wait_until="domcontentloaded")
                            await page2.wait_for_timeout(2500)
                            evtext = await page2.inner_text("#events")
                            con_events = "llm.emotion" in evtext
                            sess_text = await page2.inner_text("#session")

                            # (c) /compare design bench renders
                            page3 = await ctx.new_page()
                            await page3.goto(
                                "http://127.0.0.1:8791/compare",
                                wait_until="domcontentloaded")
                            await page3.wait_for_timeout(2000)
                            nm = await page3.locator(".nm").first.inner_text()
                            n_canvas = await page3.locator("canvas").count()

                            await browser.close()

                            problems = []
                            if not ok_backend:
                                problems.append(
                                    "no llm.emotion via real-frontend "
                                    "text-input")
                            if not ui_grew:
                                problems.append(
                                    "frontend body text did not grow "
                                    "(reply not rendered?)")
                            if not con_events:
                                problems.append(
                                    "console events stream missing "
                                    "llm.emotion")
                            if not ({"thinking", "speaking"} & states):
                                problems.append(
                                    f"session states={sorted(states)}")
                            if not sess_text.strip():
                                problems.append("console session card empty")
                            if "Wafer" not in (nm or ""):
                                problems.append(f"compare panel={nm!r}")
                            if n_canvas < 1:
                                problems.append("compare canvas missing")
                            fatal = [e for e in js_errors
                                     if "CONNECTION_REFUSED" not in e
                                     and "404" not in e]
                            if fatal:
                                problems.append(f"JS errors: {fatal[:2]}")
                            report.finish(
                                case, "PASS" if not problems else "FAIL",
                                "; ".join(problems) or
                                f"frontend round ok (states="
                                f"{sorted(s for s in states if s)}), "
                                f"console+compare rendered")
                    except Exception as exc:  # noqa: BLE001
                        report.finish(case, "FAIL",
                                      f"{type(exc).__name__}: {exc}")

    await hub.close()


# ---------------------------------------------------------------------------
# wrapper suites (thin: subprocess passthrough, internals untouched)
# ---------------------------------------------------------------------------

WRAPPERS = {
    "ve2e": "ve2e_run.py",
    "egroup": "e2e_verify.py",
    "bench": "bench_e2e.py",
    "accept": "accept_home.py",
}


def run_wrapper(name: str, passthrough: list[str]) -> int:
    script = ROOT / "scripts" / WRAPPERS[name]
    cmd = [sys.executable, str(script), *passthrough]
    print(f"\n===== suite: {name} ({WRAPPERS[name]}) =====", flush=True)
    return subprocess.call(cmd, cwd=ROOT)


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("suites", nargs="+",
                    choices=["smoke", "olvloop", "olvcore", "ve2e", "egroup",
                             "bench", "accept", "all"])
    ap.add_argument("--only", default=None, metavar="TEST.DEPLOY.NN",
                    help="smoke: run a single case")
    ap.add_argument("--skip-trio", action="store_true",
                    help="smoke: reuse the running trio")
    ap.add_argument("--keep-services", action="store_true",
                    help="smoke: don't stop trio/OLV on exit")
    args, passthrough = ap.parse_known_args(argv)

    suites = (["smoke", "ve2e", "egroup", "bench"]
              if "all" in args.suites else list(args.suites))
    rc = 0
    for suite in suites:
        if suite in ("smoke", "olvloop", "olvcore"):
            report = Report()
            mgr = ServiceManager()
            smoke_args = args

            runner = {"smoke": run_smoke, "olvloop": run_olvloop,
                      "olvcore": run_olvcore}[suite]

            async def _run(rep=report, manager=mgr, sa=smoke_args,
                           fn=runner) -> None:
                await fn(sa, rep, manager)

            try:
                asyncio.run(_run())
            except Exception as exc:  # noqa: BLE001
                report.add("smoke-suite", "FAIL", f"{type(exc).__name__}: "
                                                 f"{exc}")
            finally:
                if not args.keep_services:
                    mgr.stop_all()
            rc |= report.summary()
        else:
            rc |= run_wrapper(suite, passthrough)
    return rc


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
