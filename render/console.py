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

        makers = {"wafer": _wa.WaferFace, "mochi": _mo.MochiFace,
                  "bubble": _b.BubbleFace, "mech": _me.MechFace,
                  "claw": _cl.ClawFace}
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
}


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
            "mode": "normal",
            "gate": None,
            "wake_word": None,
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
                elif topic == topics.MODE_SWITCH:
                    state["mode"] = payload.get("mode")
                elif topic == topics.GATE_DECISION:
                    state["gate"] = payload
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
    style_preview: StylePreview = None  # injected by serve()

    def log_message(self, fmt, *args) -> None:  # quiet
        pass

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
        elif parsed.path == "/compare":
            self._html(self.compare_html)
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
        elif parsed.path == "/api/state":
            self._json(self.bridge.latest())
        elif parsed.path == "/api/events":
            qs = parse_qs(parsed.query)
            since = int(qs.get("since", ["0"])[0])
            events = self.bridge.events_since(since)
            self._json({"events": events, "latest_seq": self.bridge._seq})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        if urlparse(self.path).path != "/api/ctl":
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

    handler = type("BoundHandler", (_Handler,), {
        "bridge": bridge,
        "index_html": html,
        "compare_html": compare_html,
        "style_preview": StylePreview(),
    })
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="wafer-console")
    thread.start()
    logger.info("wafer-console listening on http://%s:%d", host, port)
    return server, bridge
