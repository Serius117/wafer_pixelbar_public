"""Persistent runtime provider choice (local vs online).

Only the modes are stored here; API keys stay in gitignored .env and
providers.local.yaml.  The file lives under gitignored data/ so it survives
stack restarts without leaking secrets.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "data" / "provider_state.json"
DEFAULTS = {"llm": "online", "tts": "online", "asr": "cpu"}
VALID = {
    "llm": {"online", "local"},
    "tts": {"online", "local"},
    "asr": {"cpu", "gpu"},
}


def state_path() -> Path:
    override = os.environ.get("WAFER_PROVIDER_STATE")
    return Path(override) if override else DEFAULT_PATH


def load_state() -> dict[str, str]:
    path = state_path()
    state = dict(DEFAULTS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return state
    if isinstance(raw, dict):
        for key, allowed in VALID.items():
            value = str(raw.get(key, "")).strip().lower()
            if value in allowed:
                state[key] = value
    return state


def save_state(state: dict[str, Any]) -> dict[str, str]:
    current = load_state()
    for key, allowed in VALID.items():
        value = str(state.get(key, current[key])).strip().lower()
        if value not in allowed:
            raise ValueError(f"invalid {key} mode: {value!r}")
        current[key] = value
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    return current


def apply_state(conf: dict[str, Any]) -> dict[str, Any]:
    """Apply persisted modes to a copied inference/provider config."""
    state = load_state()
    out = dict(conf)
    llm = dict(out.get("llm") or {})
    tts = dict(out.get("tts") or {})
    asr = dict(out.get("asr") or {})
    llm["provider"] = "openai_compat" if state["llm"] == "online" else "local"
    tts["provider"] = "qwen_tts" if state["tts"] == "online" else "gpt_sovits"
    asr["device"] = state["asr"]
    out["llm"] = llm
    out["tts"] = tts
    out["asr"] = asr
    return out
