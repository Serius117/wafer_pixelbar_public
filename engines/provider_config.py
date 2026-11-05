"""Runtime provider configuration (TASK.PROVIDER.01).

Layering (lowest -> highest):
    config/inference.yaml
    config/providers.local.yaml   (gitignored; user-specific endpoints/voices)
    environment variables         (keys and explicit overrides)

The tracked inference.yaml contains local defaults and no secrets.  The
local overlay lets a user point LLM/TTS at arbitrary APIs without dirtying
a tracked file; real keys live only in the gitignored repo-root .env.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INFERENCE = ROOT / "config" / "inference.yaml"
DEFAULT_LOCAL = ROOT / "config" / "providers.local.yaml"


def load_env_file(path: Path | str | None = None) -> dict[str, str]:
    """Load simple KEY=VALUE pairs without overwriting the real environment."""
    env_path = Path(path) if path is not None else ROOT / ".env"
    loaded: dict[str, str] = {}
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return loaded
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip().strip('"').strip("'")
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `override` into `base` (new dict, no mutation)."""
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        old = merged.get(key)
        if isinstance(old, dict) and isinstance(value, dict):
            merged[key] = _merge(old, value)
        else:
            merged[key] = value
    return merged


def load_inference(
    path: Path | str | None = None,
    local_path: Path | str | None = None,
) -> dict[str, Any]:
    """Load tracked inference config plus the optional gitignored overlay.

    Env overrides:
        WAFER_INFERENCE_CONFIG  base file
        WAFER_PROVIDER_CONFIG   local overlay file
        WAFER_PROVIDER_CONFIG= (empty) disables the overlay
    """
    env_base = os.environ.get("WAFER_INFERENCE_CONFIG")
    explicit_base = path is not None or bool(env_base)
    base_path = Path(path or env_base or DEFAULT_INFERENCE)
    data = yaml.safe_load(base_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"inference config is not a mapping: {base_path}")

    if local_path is None:
        env_local = os.environ.get("WAFER_PROVIDER_CONFIG")
        if env_local:
            overlay_path = Path(env_local)
        elif explicit_base:
            # Smoke/CI configs must stay self-contained: an explicit
            # --config never picks up the developer's cloud overlay.
            overlay_path = None
        else:
            overlay_path = DEFAULT_LOCAL
    else:
        overlay_path = Path(local_path)
    if overlay_path is not None and overlay_path.exists():
        overlay = yaml.safe_load(overlay_path.read_text(encoding="utf-8")) or {}
        if not isinstance(overlay, dict):
            raise ValueError(f"provider overlay is not a mapping: {overlay_path}")
        data = _merge(data, overlay)
    return data


def resolve_llm(conf: dict[str, Any]) -> tuple[str, str, str]:
    """Resolve (provider, base_url, model/key) for the configured LLM.

    Returns `(provider, base_url, model)`; the caller resolves the key from
    env so it is never returned/logged by config helpers.
    """
    llm = conf.get("llm") or {}
    provider = str(llm.get("provider") or "local").strip()
    if provider != "openai_compat":
        return provider, "", ""
    profile = llm.get("openai_compat") or {}
    base_url = (os.environ.get(str(profile.get("base_url_env")
                                  or "WAFER_LLM_BASE_URL"), "")
                or str(profile.get("base_url") or "").strip())
    model = (os.environ.get(str(profile.get("model_env")
                                or "WAFER_LLM_MODEL"), "")
             or str(profile.get("model") or "").strip())
    if not base_url:
        raise RuntimeError(
            "llm.provider=openai_compat needs a base_url "
            "(WAFER_LLM_BASE_URL or llm.openai_compat.base_url)")
    if not model:
        raise RuntimeError(
            "llm.provider=openai_compat needs a model "
            "(WAFER_LLM_MODEL or llm.openai_compat.model)")
    return provider, base_url, model


def tts_profile(conf: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Merge the selected TTS provider's nested profile over flat fields."""
    tts = conf.get("tts") or {}
    provider = str(tts.get("provider") or "gpt_sovits").strip() or "gpt_sovits"
    profile = dict(tts)
    nested = tts.get(provider)
    if isinstance(nested, dict):
        profile.update(nested)
    return provider, profile


def provider_env(profile: dict[str, Any], key: str, default_env: str) -> str:
    """Read a profile value with an env-var name convention."""
    env_name = str(profile.get(f"{key}_env") or default_env)
    return os.environ.get(env_name, "").strip()
