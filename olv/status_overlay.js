/* wafer status badge (R.21 / D46 / 需求 #14).
 *
 * Injected into the OLV page as /wafer-status.js. Polls the wafer-console
 * status API (same source of truth as the LED) and renders a fixed badge so
 * the user always knows what the robot is doing -- "Thinking..." alone is a
 * failed state per the requirement.
 */
(function () {
  "use strict";

  var API = "http://127.0.0.1:8790/api/state";
  var POLL_MS = 800;

  var dot = document.createElement("span");
  dot.style.cssText =
    "display:inline-block;width:9px;height:9px;border-radius:50%;" +
    "margin-right:7px;background:#8a8f98;vertical-align:middle;";

  var text = document.createElement("span");
  text.textContent = "连接中…";

  var box = document.createElement("div");
  box.id = "wafer-status-badge";
  box.style.cssText =
    "position:fixed;left:14px;bottom:14px;z-index:99999;" +
    "padding:7px 12px;border-radius:999px;font:13px/1.35 system-ui," +
    "'Segoe UI',sans-serif;color:#eef1f6;background:rgba(18,20,26,.82);" +
    "border:1px solid rgba(255,255,255,.12);box-shadow:0 4px 14px " +
    "rgba(0,0,0,.35);backdrop-filter:blur(6px);max-width:min(70vw,620px);" +
    "white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" +
    "pointer-events:none;user-select:none;";
  box.appendChild(dot);
  box.appendChild(text);

  var COLORS = {
    standby: "#8a8f98", listening: "#4aa8ff", thinking: "#ffc24a",
    speaking: "#4ad991", task: "#b18cff", done: "#4ad991",
    failed: "#ff6b6b", decision: "#ff9d5c"
  };

  var booted = false;
  var labelSince = Date.now();
  var prevLabel = "";

  function attach() {
    if (document.body && !document.getElementById("wafer-status-badge")) {
      document.body.appendChild(box);
    }
  }

  function setLabel(label, color) {
    attach();
    if (label !== prevLabel) {
      prevLabel = label;
      labelSince = Date.now();
    }
    dot.style.background = color || COLORS.standby;
    text.textContent = label;
  }

  function seconds(n) {
    n = Math.max(0, Math.floor(n));
    return n + "s";
  }

  function clock(n) {
    n = Math.max(0, Math.floor(n));
    var m = Math.floor(n / 60);
    var s = n % 60;
    return m + ":" + (s < 10 ? "0" + s : s);
  }

  function clip(s, n) {
    s = (s || "").replace(/\s+/g, " ").trim();
    return s.length > n ? s.slice(0, n) + "…" : s;
  }

  function render(state) {
    var now = state.now || Date.now() / 1000;
    var silent = state.last_event_t ? now - state.last_event_t : 0;
    var task = state.task;

    if (state.notify && silent < 15) {
      setLabel("提示：" + clip(state.notify, 46), "#ffc24a");
      return;
    }
    if (task && task.phase === "failed") {
      setLabel("卡住了：" + clip(task.summary || task.detail || "未知原因", 48),
               COLORS.failed);
      return;
    }
    if (task && task.phase === "done") {
      setLabel("搞定：" + clip(task.summary || "完成", 48), COLORS.done);
      return;
    }
    if (task && task.phase === "permission") {
      setLabel("待决策：" + clip(task.detail || "需要确认", 30) +
               " · 说「批准」或「拒绝」", COLORS.decision);
      return;
    }
    if (task && task.phase === "clarify") {
      setLabel("没找到项目：" + clip(task.detail || "", 34) +
               " · 说全名或换说法", COLORS.decision);
      return;
    }
    var running = task && (task.phase === "dispatched" ||
                           task.phase === "running");
    if (running) {
      var elapsed = state.task_started ? now - state.task_started : 0;
      var label = "干活中：" + clip(task.prompt || "任务", 18) + " · 已运行 " +
                  clock(elapsed) + " · " + clip(task.detail || task.phase, 24);
      if (silent > 20) {
        label += " · 静默 " + seconds(silent);
      }
      setLabel(label, COLORS.task);
      return;
    }

    var s = state.session_state || "standby";
    if (s === "thinking") {
      setLabel("小饼在想… " + seconds((Date.now() - labelSince) / 1000),
               COLORS.thinking);
    } else if (s === "speaking") {
      setLabel("小饼在说话 · 可以插话打断", COLORS.speaking);
    } else if (s === "listening") {
      setLabel("在听 · 直接说", COLORS.listening);
    } else {
      setLabel("待机 · 说「小饼小饼」唤醒", COLORS.standby);
    }
  }

  function tick() {
    fetch(API, { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (state) {
        booted = true;
        render(state);
      })
      .catch(function () {
        attach();
        if (!booted) {
          setLabel("状态服务未启动（LED 仍工作）", "#8a8f98");
        } else {
          setLabel("状态服务断开 · 最后：" + prevLabel, "#ff6b6b");
        }
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", attach);
  } else {
    attach();
  }
  setInterval(tick, POLL_MS);
  tick();
})();
