"""wafer-console (T6.12/T6.13, D13): read-only monitor + limited control.

A zero-dependency console served by the render process on localhost:
  GET  /                 static page (render/console/index.html)
  GET  /api/state        latest status summary (session/mode/gate/task)
  GET  /api/events?since=N   event log after sequence N (polling)
  POST /api/ctl          limited control: only publishes bus events,
                         never calls any module directly (D13 boundary)

Everything is fire-and-forget on the hub; the console has no authority
beyond what the bus carries.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from core.bus import topics
from core.bus.hub import InProcBus
from engines.provider_state import load_state as load_provider_state
from engines.provider_state import save_state as save_provider_state

logger = logging.getLogger(__name__)

CONSOLE_DIR = Path(__file__).parent / "console"
EVENT_BUFFER = 200

# --- TASK.CONSOLE.01: five-style compare view --------------------------------

STYLE_META: list[tuple[str, str, str]] = [
    ("wafer", "Wafer", "你命名审批的定制风格（默认）——每表情单一几何嘴形"),
    ("mochi", "Mochi", "圆点眼 emoji 风——结构最简，低分辨率可读性最高"),
    ("bubble", "Bubble", "圆眼+高光点（EMO 机器人式）——高光给眼睛生命感"),
    ("mech", "Mech", "WALL-E 式方眼+扫描线——工业质感"),
    ("claw", "Claw", "开源 claw-face 改编（MIT）——每眼 8 自由度+300ms 平滑变形"),
    ("whale", "Whale", "线条鲸（TASK.AVATAR.01）——横向身体适配 8:1 画幅，尾鳍+喷水+气泡"),
]

COMPARE_THRESH = 50  # 1bpp threshold: lit if max(r,g,b) > T (true-device view)


def _frame_to_1bpp_b64(frame) -> str:  # noqa: ANN001 (Frame)
    """RGB888 frame -> 1bpp 256x32 -> base64 (1024B)."""
    px, w, h = frame.pixels, frame.width, frame.height
    out = bytearray()
    for y in range(h):
        row = bytearray(w // 8)
        base = y * w * 3
        for x in range(w):
            i = base + x * 3
            if max(px[i], px[i + 1], px[i + 2]) > COMPARE_THRESH:
                row[x >> 3] |= 1 << (7 - (x & 7))
        out += row
    return base64.b64encode(bytes(out)).decode("ascii")


class StylePreview:
    """Live five-style preview: independent face instances, ticked on poll.

    Lives inside the console process only -- never touches the active
    FaceManager / frame pipeline (console stays a read-only monitor, D13).
    """

    def __init__(self) -> None:
        import render.claw_face as _cl
        import render.faces.bubble as _b
        import render.faces.mech as _me
        import render.faces.mochi as _mo
        import render.faces.wafer as _wa
        import render.faces.whale as _wh

        makers = {"wafer": _wa.WaferFace, "mochi": _mo.MochiFace,
                  "bubble": _b.BubbleFace, "mech": _me.MechFace,
                  "claw": _cl.ClawFace, "whale": _wh.WhaleFace}
        self._faces = {sid: makers[sid]() for sid, _, _ in STYLE_META}
        self._lock = threading.Lock()
        self._last = time.monotonic()
        self._expression = "normal"

    def snapshot(self, expression: str | None = None) -> dict:
        """Tick all faces by elapsed wall time and render them (thread-safe)."""
        with self._lock:
            now = time.monotonic()
            dt = max(0.0, min(0.25, now - self._last))
            self._last = now
            if expression:
                self._expression = expression
            styles = []
            for sid, name, desc in STYLE_META:
                face = self._faces[sid]
                face.set_expression(self._expression)
                ok = str(getattr(face.state, "expression", "")) == \
                    self._expression
                fb64 = None
                if ok:
                    face.tick(dt)
                    fb64 = _frame_to_1bpp_b64(face.render())
                styles.append({"id": sid, "name": name, "desc": desc,
                               "ok": ok, "frame": fb64})
            return {"expression": self._expression, "w": 256, "h": 32,
                    "styles": styles}


# POST /api/ctl action -> (topic, payload). The console ONLY publishes.
CTL_ACTIONS: dict[str, tuple[str, dict[str, Any]]] = {
    "wake": (topics.CTL_HOTKEY, {"action": "wake"}),
    "sleep": (topics.CTL_HOTKEY, {"action": "sleep"}),
    "gate_toggle": (topics.CTL_HOTKEY, {"action": "gate_toggle"}),
    "approve": (topics.CTL_HOTKEY, {"action": "approve"}),
    "deny": (topics.CTL_HOTKEY, {"action": "deny"}),
    "mode_normal": (topics.MODE_SWITCH, {"mode": "normal", "tier": "none",
                                         "source": "console"}),
    "mode_game": (topics.MODE_SWITCH, {"mode": "game", "tier": "silent",
                                       "source": "console"}),
    "task_cancel": (topics.CTL_HOTKEY, {"action": "task_cancel"}),
}

STATUS_TOPICS = {
    topics.SESSION_STATE, topics.GATE_DECISION, topics.MODE_SWITCH,
    topics.TASK_RESULT, topics.TASK_PROGRESS, topics.TASK_CANCEL,
    topics.TASK_REQUEST, topics.TASK_PERMISSION, topics.TASK_CLARIFY,
    topics.WAKE_WORD, topics.SYSTEM_NOTIFY,
    topics.CTL_HOTKEY,  # control actions must show in the event stream too
    topics.PROJECT_SWITCH,  # D33: project changes visible in the console
    topics.LLM_EMOTION,  # TEST.OLVCORE.11 finding: emotion events missing
    # from the stream -- the §4.9 design has an emotion panel; at minimum
    # the rolling log must show them
    topics.WAKE_STATUS,  # CONSOLE.11 A2: standby listening indicator
    topics.GATE_ENABLED,  # CONSOLE.11 rev: console gate toggle state
}

# --- TASK.CONSOLE.07: speaker management API ---------------------------------
#
# GET  /api/speakers         list profiles + enrollment status
# POST /api/speakers         enroll: {speaker_id, name, role, priority,
#                            samples: [base64 16k s16 PCM], overwrite?}
# POST /api/speakers/delete  remove: {speaker_id}
#
# Reuses the enroll.py pipeline (sherpa CAM++ -> average -> JSON +
# speakers.yaml). No auth for now (user ruling 2026-11-07: add admin
# approval later). Enrollment caches live in the OLV process, so a
# restart is required for new speakers to take effect.

SPEAKERS_CONFIG = Path("config/speakers.yaml")
DIALOGUE_DIR = Path("logs/dialogue")  # DIALOG.01 transcript root


def _build_enroll_verifier(setup):  # noqa: ANN001 (GateSetup)
    """Build the sherpa CAM++ verifier used by console enrollment.

    Module-level so tests can monkeypatch it with a fake embedder.
    """
    from engines.voiceprint import SherpaCampplusVerifier

    if setup.voiceprint_model is None:
        raise FileNotFoundError(
            "voiceprint.model not set in speakers.yaml (see docs)")
    model_path = Path(setup.voiceprint_model)
    if not model_path.exists():
        raise FileNotFoundError(f"CAM++ model not found: {model_path}")
    store = _enroll_store(setup)
    return SherpaCampplusVerifier(store, model_path)


def _enroll_store(setup):  # noqa: ANN001 (GateSetup)
    """EnrollmentStore for the given setup (lazy import, engines layer)."""
    from engines.voiceprint import EnrollmentStore

    return EnrollmentStore(setup.embedding_dir)


def _list_speakers() -> dict:
    """Profiles from speakers.yaml + enrollment status from the store."""
    from core.gate.profiles import load_gate_setup

    setup = load_gate_setup(SPEAKERS_CONFIG)
    enrolled = _enroll_store(setup).load()
    speakers = [
        {
            "speaker_id": p.speaker_id,
            "name": p.name,
            "role": p.role,
            "priority": p.priority,
            "enrolled": p.speaker_id in enrolled,
        }
        for p in sorted(setup.profiles.values(),
                        key=lambda p: p.priority)
    ]
    known = set(setup.profiles)
    orphaned = sorted(sid for sid in enrolled if sid not in known)
    return {"speakers": speakers, "orphaned": orphaned}


# CONSOLE.11 rev: log tail API for the console log tab. Whitelist only --
# never serve arbitrary paths.
LIVE2D_MODELS = (
    "Hiyori",
    "Mao",
    "DeepSeek-chan",
    "whale-maid-live2d",
)

LOG_FILES: dict[str, str] = {
    "olv": "logs/olv.log",
    "bridge": "logs/bridge.log",
    "render": "logs/render.log",
    "hub": "logs/hub.log",
    "llm": "logs/infer_llm.log",
    "asr": "logs/infer_asr.log",
    "tts": "logs/infer_tts.log",
}


def _tail_log(name: str, lines: int) -> dict:
    rel = LOG_FILES.get(name)
    if rel is None:
        return {"error": f"未知日志源 {name!r}（可用：{'/'.join(LOG_FILES)}）"}
    p = Path(rel)
    if not p.exists():
        return {"lines": [], "note": f"{rel} 尚未生成（服务未启动过）"}
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"error": f"读取失败：{e}"}
    all_lines = content.strip().splitlines()
    return {"lines": all_lines[-lines:], "total": len(all_lines)}


_SPEAKER_ID_OK = __import__("re").compile(r"^[a-z0-9_-]{1,32}$")

# CONSOLE.11 D3: gate thresholds for the score gauge (mtime-cached so the
# 1s polling does not re-read speakers.yaml every time).
_GATE_CFG_CACHE: tuple[float, dict] | None = None


def _gate_thresholds() -> dict:
    global _GATE_CFG_CACHE

    try:
        mtime = (SPEAKERS_CONFIG.stat().st_mtime
                 if SPEAKERS_CONFIG.exists() else 0.0)
        if _GATE_CFG_CACHE is not None and _GATE_CFG_CACHE[0] == mtime:
            return _GATE_CFG_CACHE[1]
        from core.gate.profiles import load_gate_setup

        setup = load_gate_setup(SPEAKERS_CONFIG)
        cfg = {"t_pass": setup.config.t_pass, "t_gray": setup.config.t_gray}
        _GATE_CFG_CACHE = (mtime, cfg)
        return cfg
    except Exception:  # noqa: BLE001 -- defaults keep the panel alive
        return {"t_pass": 0.52, "t_gray": 0.45}


def _enroll_speaker(body: dict) -> tuple[dict, int]:
    """Enroll one speaker from base64 PCM samples. Returns (json, code)."""
    import base64
    import binascii

    from core.gate.profiles import load_gate_setup
    from scripts.enroll import update_speakers_yaml

    speaker_id = str(body.get("speaker_id", "")).strip()
    name = str(body.get("name", "")).strip()
    role = str(body.get("role", "member"))
    priority = body.get("priority", 99)
    samples_b64 = body.get("samples") or []
    overwrite = bool(body.get("overwrite"))

    if not _SPEAKER_ID_OK.match(speaker_id):
        return {"error": "speaker_id 只允许小写字母/数字/_/-，长度 1-32"}, 400
    if not name:
        return {"error": "name 不能为空"}, 400
    if role not in ("admin", "member"):
        return {"error": "role 只允许 admin 或 member"}, 400
    try:
        priority = int(priority)
    except (TypeError, ValueError):
        return {"error": "priority 必须是整数"}, 400
    if not samples_b64:
        return {"error": "samples 为空：请先录音"}, 400

    # decode PCM samples (16 kHz mono s16)
    samples: list[bytes] = []
    for i, b64 in enumerate(samples_b64):
        try:
            pcm = base64.b64decode(str(b64))
        except (binascii.Error, ValueError):
            return {"error": f"第 {i + 1} 段音频 base64 解码失败"}, 400
        if len(pcm) < 16000:  # < 0.5 s of audio
            return {"error": f"第 {i + 1} 段音频太短（<0.5s）"}, 400
        samples.append(pcm)

    setup = load_gate_setup(SPEAKERS_CONFIG)
    store = _enroll_store(setup)
    if speaker_id in store.load() and not overwrite:
        return {"error": f"说话人 '{speaker_id}' 已注册；重新注册请勾选覆盖"}, 409

    try:
        verifier = _build_enroll_verifier(setup)
        embeddings = [verifier.embed(pcm) for pcm in samples]
        from engines.voiceprint import average_embedding

        embedding = average_embedding(embeddings)
    except FileNotFoundError as e:
        return {"error": f"声纹模型不可用：{e}"}, 503
    except Exception as e:  # noqa: BLE001 -- surface to the panel
        return {"error": f"声纹提取失败：{e}"}, 500
    if not embedding:
        return {"error": "embedding 为空"}, 500

    emb_path = store.save(speaker_id, embedding)
    update_speakers_yaml(SPEAKERS_CONFIG, speaker_id, name, role,
                         priority, emb_path.as_posix())
    logger.info("console enroll: %s (%s/%s, %d samples)",
                speaker_id, name, role, len(samples))
    return {
        "ok": True,
        "speaker_id": speaker_id,
        "dim": len(embedding),
        "note": "注册已保存；重启 OLV 服务后生效（enrollment 缓存在进程内）",
    }, 200


def _delete_speaker(body: dict) -> tuple[dict, int]:
    """Remove one speaker: embedding file + speakers.yaml entry."""
    import yaml

    from core.gate.profiles import load_gate_setup

    speaker_id = str(body.get("speaker_id", "")).strip()
    if not speaker_id:
        return {"error": "speaker_id 不能为空"}, 400

    setup = load_gate_setup(SPEAKERS_CONFIG)
    _enroll_store(setup).delete(speaker_id)

    # drop the yaml entry, keeping the rest of the file intact
    raw: dict = {}
    if SPEAKERS_CONFIG.exists():
        raw = yaml.safe_load(
            SPEAKERS_CONFIG.read_text(encoding="utf-8")) or {}
    speakers = raw.setdefault("speakers", {})
    removed = speakers.pop(speaker_id, None) is not None
    SPEAKERS_CONFIG.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    logger.info("console speaker delete: %s (yaml entry: %s)",
                speaker_id, removed)
    return {"ok": True, "speaker_id": speaker_id,
            "note": "已删除；重启 OLV 服务后生效"}, 200


class ConsoleBridge:
    """Subscribes to the bus, keeps a rolling event log + latest status."""

    def __init__(self, bus: InProcBus, frame_provider=None,
                 text_provider=None) -> None:
        self._bus = bus
        self._events: deque[dict] = deque(maxlen=EVENT_BUFFER)
        self._seq = 0
        self._lock = threading.Lock()
        self._frame_provider = frame_provider  # callable -> Frame | None (LED)
        # D36: callable -> last textface push dict | None (LED text channel)
        self._text_provider = text_provider
        for topic in STATUS_TOPICS:
            bus.subscribe(topic, self._on_event)

    def _on_event(self, topic: str, payload: dict) -> None:
        import time as _time

        with self._lock:
            self._seq += 1
            self._events.append({
                "seq": self._seq,
                "topic": topic,
                "payload": dict(payload),
                "t": _time.time(),
            })

    def events_since(self, since: int) -> list[dict]:
        with self._lock:
            return [e for e in self._events if e["seq"] > since]

    def latest(self) -> dict:
        """Derive the status summary from the newest events per topic."""
        state: dict[str, Any] = {
            "session_state": "standby",
            "timeout_s": None,
            "mode": "normal",
            "gate": None,
            "gate_enabled": None,
            "wake_word": None,
            "wake_status": None,
            "task": None,
            "notify": None,
            "current_project": None,
            "task_started": None,
            "last_event_t": None,
        }
        with self._lock:
            for event in self._events:
                topic, payload = event["topic"], event["payload"]
                state["last_event_t"] = event.get("t")
                if topic == topics.SESSION_STATE:
                    state["session_state"] = payload.get("state")
                    # CONSOLE.11 D4: idle timeout for the countdown
                    if payload.get("timeout_s"):
                        state["timeout_s"] = payload.get("timeout_s")
                elif topic == topics.MODE_SWITCH:
                    state["mode"] = payload.get("mode")
                elif topic == topics.GATE_DECISION:
                    state["gate"] = payload
                elif topic == topics.GATE_ENABLED:
                    # CONSOLE.11 rev: console gate toggle state
                    state["gate_enabled"] = payload.get("enabled")
                elif topic == topics.WAKE_STATUS:
                    # CONSOLE.11 A2: standby/wake indicator payload
                    state["wake_status"] = payload
                elif topic == topics.WAKE_WORD:
                    state["wake_word"] = payload.get("keyword")
                elif topic == topics.TASK_REQUEST:
                    task = payload.get("task") or {}
                    state["task"] = {
                        "phase": "dispatched",
                        "prompt": task.get("original_text") or task.get("prompt"),
                        "task_id": task.get("task_id"),
                    }
                    state["task_started"] = event.get("t")
                elif topic == topics.TASK_PROGRESS:
                    merged = dict(state["task"] or {})
                    merged.update({"phase": payload.get("phase"),
                                   "detail": payload.get("detail")})
                    state["task"] = merged
                elif topic == topics.TASK_RESULT:
                    merged = dict(state["task"] or {})
                    ok = payload.get("ok")
                    merged.update({
                        "phase": "done" if ok else "failed",
                        "summary": payload.get("summary"),
                        "ok": ok,
                    })
                    state["task"] = merged
                elif topic == topics.TASK_PERMISSION:
                    # R.21: awaiting-decision state (approve/deny pending)
                    merged = dict(state["task"] or {})
                    kind = payload.get("kind") or ""
                    target = payload.get("target") or ""
                    merged.update({
                        "phase": "permission",
                        "detail": f"{kind} {target}".strip(),
                        "task_id": payload.get("task_id"),
                    })
                    state["task"] = merged
                elif topic == topics.TASK_CLARIFY:
                    # R.21/SLOT.03: alias-not-found clarification pending
                    merged = dict(state["task"] or {})
                    spoken = payload.get("spoken") or ""
                    candidates = payload.get("candidates") or []
                    merged.update({
                        "phase": "clarify",
                        "detail": " → ".join(
                            [spoken] + ["、".join(candidates)]).strip(" →"),
                        "candidates": candidates,
                    })
                    state["task"] = merged
                elif topic == topics.SYSTEM_NOTIFY:
                    state["notify"] = payload.get("text")
                elif topic == topics.PROJECT_SWITCH:
                    state["current_project"] = payload
        state["now"] = __import__("time").time()
        # R.21 safety net: a cancelled/interrupted chain may never publish the
        # post-speech state; treat a long-silent "speaking" as listening so the
        # badge cannot get stuck forever.
        last_t = state.get("last_event_t")
        if (state["session_state"] == "speaking" and last_t
                and state["now"] - last_t > 20.0):
            state["session_state"] = "listening"
        # D36: current LED text push (real-machine channel)
        if self._text_provider is not None:
            try:
                state["textface"] = self._text_provider()
            except Exception:
                state["textface"] = None
        return state

    async def publish_action(self, action: str, params: dict | None = None) -> bool:
        """Limited control: publish a bus event, nothing more (D13).

        D33: also handles project_switch / project_add / suggest_alias.
        """
        params = params or {}

        if action == "project_switch":
            from core.projects import load_projects, save_default

            cfg = load_projects()
            entry = cfg.resolve(str(params.get("alias", "")))
            if entry is None:
                return False
            save_default(Path("config/projects.yaml"), entry.alias)
            await self._bus.publish(topics.PROJECT_SWITCH, {
                "alias": entry.alias, "path": str(entry.path),
            })
            logger.info("console project_switch -> %s", entry.alias)
            return True

        if action == "project_add":
            from core.projects import add_project

            path = Path(str(params.get("path", "")))
            if not path.exists() or not path.is_dir():
                return False
            alias = str(params.get("alias", "")).strip()
            if not alias:
                return False
            add_project(Path("config/projects.yaml"), alias, path,
                        str(params.get("description", "")))
            logger.info("console project_add: %s -> %s", alias, path)
            return True

        if action == "suggest_alias":
            return True  # handled in _Handler (needs a sync LLM call)

        # Console-only: expression preview (publishes llm.emotion for the
        # RenderEventBridge to pick up -- lets the user verify all 19 faces)
        if action == "set_expression":
            expression = str(params.get("expression", "normal"))
            await self._bus.publish(topics.LLM_EMOTION, {"tag": expression})
            logger.info("console expression preview: %s", expression)
            return True

        # Console-only: face style switch (F group)
        if action == "face_switch":
            style = str(params.get("style", "claw"))
            await self._bus.publish(topics.FACE_SWITCH, {"style": style})
            logger.info("console face_switch: %s", style)
            return True

        entry = CTL_ACTIONS.get(action)
        if entry is None:
            return False
        topic, payload = entry
        await self._bus.publish(topic, payload)
        logger.info("console action -> %s %s", action, topic)
        return True


class _Handler(BaseHTTPRequestHandler):
    bridge: ConsoleBridge = None  # injected by serve()
    index_html: bytes = b""
    compare_html: bytes = b""
    app_html: bytes = b""  # D62 unified shell (tab page)
    style_preview: StylePreview = None  # injected by serve()

    def log_message(self, fmt, *args) -> None:  # quiet
        pass

    _SERVICE_PORTS = {
        "hub": 8765, "render": 8790, "llm": 8080,
        "asr": 8001, "tts": 9880, "olv": 12393,
    }

    def _probe_services(self) -> dict:
        """TASK.CONSOLE.05 C1: TCP health for the six services."""
        import socket

        result = {}
        for name, port in self._SERVICE_PORTS.items():
            try:
                with socket.create_connection(
                        ("127.0.0.1", port), timeout=0.5):
                    result[name] = {"port": port, "up": True}
            except (TimeoutError, OSError):
                result[name] = {"port": port, "up": False}
        return {"services": result, "ts": __import__("time").time()}

    def _probe_vram(self) -> dict:
        """TASK.CONSOLE.05 C2: GPU VRAM via nvidia-smi (if present)."""
        import shutil
        import subprocess

        if not shutil.which("nvidia-smi"):
            return {"vram": None, "reason": "nvidia-smi not found"}
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                timeout=3, text=True)
            used, total = out.strip().split(",")[0].strip(), \
                out.strip().split(",")[1].strip()
            return {"vram": {"used_mb": int(used), "total_mb": int(total)}}
        except Exception:  # noqa: BLE001 -- never break the console
            return {"vram": None, "reason": "nvidia-smi failed"}

    def _html(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # R.21: the OLV page (:12393) polls this from a different origin.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (std naming)
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._html(self.index_html)
        elif parsed.path == "/app":
            # D62 unified shell: tab bar + OLV/console iframes
            self._html(self.app_html)
        elif parsed.path == "/compare":
            self._html(self.compare_html)
        elif parsed.path == "/api/debug/services":
            # TASK.CONSOLE.05 C-group: service health (TCP port probes)
            self._json(self._probe_services())
        elif parsed.path == "/api/debug/vram":
            # TASK.CONSOLE.05 C2: GPU VRAM usage (nvidia-smi)
            self._json(self._probe_vram())
        elif parsed.path == "/api/compare":
            qs = parse_qs(parsed.query)
            expression = qs.get("expression", [None])[0]
            self._json(self.style_preview.snapshot(expression))
        elif parsed.path == "/api/frame":
            frame = self.bridge._frame_provider() if self.bridge._frame_provider else None
            if frame is None:
                self._json({"frame": None, "w": 0, "h": 0})
            else:
                import base64

                self._json({
                    "frame": base64.b64encode(frame.pixels).decode("ascii"),
                    "w": frame.width, "h": frame.height,
                })
        elif parsed.path == "/api/projects":
            from core.projects import load_projects

            cfg = load_projects()
            self._json({
                "projects": [
                    {"alias": e.alias, "path": str(e.path), "description": e.description}
                    for e in cfg.entries.values()
                ],
                "default": cfg.default_alias,
            })
        elif parsed.path == "/api/speakers":
            # TASK.CONSOLE.07: speaker list + enrollment status
            try:
                self._json(_list_speakers())
            except Exception as e:  # noqa: BLE001 -- panel must not 500
                self._json({"error": f"读取说话人失败：{e}"}, 500)
        elif parsed.path == "/api/dialogue":
            # TASK.CONSOLE.08: dialogue transcript (DIALOG.01 jsonl)
            qs = parse_qs(parsed.query)
            date = qs.get("date", [None])[0] or time.strftime("%Y-%m-%d")
            try:
                limit = max(1, min(500, int(qs.get("limit", ["100"])[0])))
            except ValueError:
                limit = 100
            entries: list[dict] = []
            p = DIALOGUE_DIR / f"{date}.jsonl"
            if p.exists():
                for ln in p.read_text(encoding="utf-8").strip().splitlines():
                    if not ln:
                        continue
                    try:
                        entries.append(json.loads(ln))
                    except json.JSONDecodeError:
                        continue
            # CONSOLE.13: TTS first-audio timing is appended by the OLV
            # tts_manager hook after the dialogue record is written.  Merge
            # the sidecar by stable turn id so the JSONL schema stays
            # append-only and old transcripts remain readable.
            sidecar = DIALOGUE_DIR / f"{date}.latency.jsonl"
            fragments: dict[str, dict] = {}
            if sidecar.exists():
                for ln in sidecar.read_text(encoding="utf-8").strip().splitlines():
                    if not ln:
                        continue
                    try:
                        item = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    turn_id = item.get("id")
                    if turn_id:
                        fragments[turn_id] = item.get("latency_ms") or {}
            for entry in entries:
                fragment = fragments.get(entry.get("id"))
                if fragment:
                    latency = entry.setdefault("latency_ms", {})
                    if isinstance(latency, dict):
                        latency.update(fragment)
            self._json({"date": date, "count": len(entries),
                        "entries": entries[-limit:]})
        elif parsed.path == "/api/logs":
            # TASK.CONSOLE.11 rev: log tail (whitelisted sources)
            qs = parse_qs(parsed.query)
            name = qs.get("name", ["olv"])[0]
            try:
                lines = max(1, min(500, int(qs.get("lines", ["150"])[0])))
            except ValueError:
                lines = 150
            result = _tail_log(name, lines)
            result["name"] = name
            self._json(result, 404 if "error" in result else 200)
        elif parsed.path == "/api/providers":
            self._json({"ok": True, "state": load_provider_state()})
        elif parsed.path == "/api/state":
            state = self.bridge.latest()
            state["providers"] = load_provider_state()
            # CONSOLE.11 D3: gate thresholds for the score gauge
            state["gate_thresholds"] = _gate_thresholds()
            self._json(state)
        elif parsed.path == "/api/events":
            qs = parse_qs(parsed.query)
            since = int(qs.get("since", ["0"])[0])
            events = self.bridge.events_since(since)
            self._json({"events": events, "latest_seq": self.bridge._seq})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        path = urlparse(self.path).path
        if path == "/api/providers":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                state = save_provider_state(body)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                self._json({"error": str(exc)}, 400)
                return
            self._json({"ok": True, "state": state})
            return
        if path == "/api/live2d-model":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                model = str(body.get("model") or "").strip()
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._json({"error": "bad json"}, 400)
                return
            if model not in LIVE2D_MODELS:
                self._json({"error": f"unknown Live2D model: {model}"}, 400)
                return
            try:
                import httpx

                r = httpx.post("http://127.0.0.1:12393/switch-model",
                               json={"model_name": model}, timeout=15.0)
                self._json({"ok": r.status_code == 200, "model": model,
                            "detail": r.text[:200]},
                           200 if r.status_code == 200 else 502)
            except Exception as exc:  # noqa: BLE001 -- console never crashes
                self._json({"error": f"OLV switch failed: {exc}"}, 502)
            return
        if path in ("/api/speakers", "/api/speakers/delete"):
            # TASK.CONSOLE.07: speaker enrollment / deletion (file-level
            # operations, same precedent as project_add; no bus round-trip
            # because the caller needs the synchronous result)
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._json({"error": "bad json"}, 400)
                return
            if path == "/api/speakers":
                result, code = _enroll_speaker(body)
            else:
                result, code = _delete_speaker(body)
            self._json(result, code)
            return
        if path != "/api/ctl":
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            action = str(body.get("action", ""))
            params = body.get("params", body)  # flat or nested
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json({"error": "bad json"}, 400)
            return

        # D33: suggest_alias is a synchronous LLM call (no bus round-trip)
        if action == "suggest_alias":
            path_name = Path(str(params.get("path", ""))).name
            # simple heuristic fallback: use the directory name as-is
            suggested = [path_name]
            self._json({"ok": True, "suggestions": suggested})
            return

        import asyncio

        loop = getattr(self.bridge, "_loop", None)
        if loop is None or loop.is_closed():
            self._json({"error": "bus loop unavailable"}, 503)
            return
        future = asyncio.run_coroutine_threadsafe(
            self.bridge.publish_action(action, params), loop
        )
        ok = future.result(timeout=3.0)
        self._json({"ok": ok}, 200 if ok else 400)


def serve(bus: InProcBus, host: str = "127.0.0.1", port: int = 8790,
          loop=None, frame_provider=None,
          text_provider=None) -> tuple[ThreadingHTTPServer, ConsoleBridge]:
    """Start the console HTTP server in a daemon thread; returns (server, bridge)."""
    bridge = ConsoleBridge(bus, frame_provider=frame_provider,
                            text_provider=text_provider)
    bridge._loop = loop  # noqa: SLF001 -- injection point for POST actions

    index_path = CONSOLE_DIR / "index.html"
    html = index_path.read_bytes() if index_path.exists() else b"<h1>console</h1>"
    compare_path = CONSOLE_DIR / "compare.html"
    compare_html = (compare_path.read_bytes() if compare_path.exists()
                    else b"<h1>compare</h1>")
    # D62: unified shell (tab bar + OLV iframe + console iframe)
    app_path = CONSOLE_DIR / "app.html"
    app_html = (app_path.read_bytes() if app_path.exists()
                else b"<h1>app</h1>")

    handler = type("BoundHandler", (_Handler,), {
        "bridge": bridge,
        "index_html": html,
        "compare_html": compare_html,
        "app_html": app_html,
        "style_preview": StylePreview(),
    })
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="wafer-console")
    thread.start()
    logger.info("wafer-console listening on http://%s:%d", host, port)
    return server, bridge
