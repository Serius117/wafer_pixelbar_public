"""Event bus topics and message types for wafer-hub.

These constants are the single source of truth for topic names used by
all processes. The hub itself is topic-agnostic (pure pub/sub relay);
semantic meaning lives with publishers and subscribers.
"""

from __future__ import annotations

# --- Audio & VAD ---------------------------------------------------------
AUDIO_STATE = "audio.state"          # {vad: "start"|"end"}
TTS_AMPLITUDE = "tts.amplitude"      # {rms: 0.0~1.0}  @30Hz throttled

# --- Session & identity ---------------------------------------------------
SESSION_STATE = "session.state"      # {state: "standby"|"listening"|...}
GATE_DECISION = "gate.decision"      # {verdict, speaker_id, score}
GATE_ENABLED = "gate.enabled"        # {enabled: bool}  CONSOLE.11 console toggle
WAKE_WORD = "wake.word"              # {keyword}  KWS hit (T6.4; console/audit feed)
WAKE_STATUS = "wake.status"          # {mode, state, detail?, timeout_s?}  CONSOLE.11 A2

# --- LLM & emotion --------------------------------------------------------
LLM_EMOTION = "llm.emotion"          # {tag: "happy"|...}

# --- Task lifecycle (bridge -> subscribers) --------------------------------
TASK_PROGRESS = "task.progress"      # {task_id, phase, detail}
TASK_PERMISSION = "task.permission"  # {task_id, kind, target}
TASK_RESULT = "task.result"          # {task_id, summary}
TASK_APPROVE = "task.approve"        # {task_id, allow}   (from core/console)
TASK_CANCEL = "task.cancel"          # {task_id, reason}  (from core)
TASK_CLARIFY = "task.clarify"        # {spoken, candidates} alias-not-found (D33)
TASK_REQUEST = "task.request"        # {op, task?, project?} pipeline -> bridge process (W.9)
TASK_MANUAL = "task.manual"          # {phase?, summary?} opencode app -> relay to the robot (T6.14)

# --- Mode & system ----------------------------------------------------------
MODE_SWITCH = "mode.switch"          # {mode: "normal"|"game", tier, source}
SYSTEM_NOTIFY = "system.notify"      # {level: "info"|"warn"|"error", text}
RENDER_COMMAND = "render.command"    # {cmd: "error"|"scene"|...}

# --- Control & misc ----------------------------------------------------------
CTL_HOTKEY = "ctl.hotkey"            # {action: "gate_toggle"|"approve"|...}
BENCH_TICK = "bench.tick"            # {stage, ts}
PROJECT_SWITCH = "project.switch"    # {alias, path}  current project changed (D33)
FACE_SWITCH = "face.switch"          # {style}  face style changed (F group)

# --- All topics (for validation and console subscription) --------------------
ALL_TOPICS = frozenset({
    AUDIO_STATE,
    TTS_AMPLITUDE,
    SESSION_STATE,
    GATE_DECISION,
    WAKE_WORD,
    LLM_EMOTION,
    TASK_PROGRESS,
    TASK_PERMISSION,
    TASK_RESULT,
    TASK_APPROVE,
    TASK_CANCEL,
    TASK_REQUEST,
    TASK_MANUAL,
    MODE_SWITCH,
    SYSTEM_NOTIFY,
    RENDER_COMMAND,
    CTL_HOTKEY,
    BENCH_TICK,
    PROJECT_SWITCH,
    FACE_SWITCH,
})
