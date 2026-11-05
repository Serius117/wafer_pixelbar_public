"""Generate the OLV conf.yaml used by wafer-pixel (R.13).

Starts from OLV's config template and points the three inference stages at the
local trio (config/inference.yaml): LLM :8080, ASR :8001, TTS :9880.

    .\\.venv\\Scripts\\python.exe scripts/setup_olv.py

Writes olv/upstream/Open-LLM-VTuber/conf.yaml (gitignored; the loader script is
tracked). Re-run after changing ports/models.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # engines.* import for --llm cloud
OLV_DIR = ROOT / "olv" / "upstream" / "Open-LLM-VTuber"
TEMPLATE = OLV_DIR / "config_templates" / "conf.default.yaml"
TARGET = OLV_DIR / "conf.yaml"

ROBOT_NAME = "小饼"
PERSONA = (
    "你是“小饼”，一位住在桌面音箱里的语音陪伴助手。"
    "说话自然、简短、温柔，口语化，不超过两句话；不要使用 Markdown。"
)


def build(conf: dict, hotwords: list[str]) -> dict:
    system = conf.setdefault("system_config", {})
    system["host"] = "127.0.0.1"
    system["port"] = 12393

    char = conf.setdefault("character_config", {})
    char["character_name"] = ROBOT_NAME
    char["human_name"] = "主人"
    char["persona_prompt"] = PERSONA

    # Disable MCP servers (wafer_agent has its own tool: task_to_opencode;
    # default "time"/"ddg-search" time out at startup)
    agent_settings = char.setdefault("agent_config", {}).setdefault(
        "agent_settings", {})
    agent_settings.setdefault("basic_memory_agent", {}).update({
        "use_mcpp": False,
        "mcp_enabled_servers": [],
    })
    agent_settings.setdefault("wafer_agent", {})["use_mcpp"] = False

    agent = char.setdefault("agent_config", {})
    agent["conversation_agent_choice"] = "wafer_agent"
    settings = agent.setdefault("agent_settings", {})
    settings.setdefault("wafer_agent", {})["llm_provider"] = (
        "openai_compatible_llm")
    settings.setdefault("basic_memory_agent", {})["llm_provider"] = (
        "openai_compatible_llm")
    llm_configs = agent.setdefault("llm_configs", {})
    llm_configs["openai_compatible_llm"] = {
        "base_url": "http://127.0.0.1:8080/v1",
        "llm_api_key": "local",
        "organization_id": None,
        "project_id": None,
        "model": "local",
        "temperature": 0.8,
        "interrupt_method": "user",
    }

    asr = char.setdefault("asr_config", {})
    asr["asr_model"] = "wafer_llama_asr"
    asr["wafer_llama_asr"] = {
        "base_url": "http://127.0.0.1:8001",
        "model": "qwen3-asr",
        "hotwords": hotwords,
        "timeout": 60.0,
        "dump": True,
    }

    tts = char.setdefault("tts_config", {})
    tts["tts_model"] = "wafer_http_tts"
    tts["wafer_http_tts"] = {
        "base_url": "http://127.0.0.1:9880",
        "timeout": 300.0,
        "file_extension": "wav",
    }

    vad = char.setdefault("vad_config", {})
    vad["vad_model"] = None  # browser side does VAD; server VAD adds a dependency
    silero = vad.setdefault("silero_vad", {})
    silero.setdefault("orig_sr", 16000)
    silero.setdefault("target_sr", 16000)
    silero.setdefault("prob_threshold", 0.4)
    silero.setdefault("db_threshold", 60)
    silero.setdefault("required_hits", 3)
    silero.setdefault("required_misses", 24)
    silero.setdefault("smoothing_window", 5)
    return conf


def _apply_cloud_llm(conf: dict, mode: str) -> str:
    """Point OLV's OpenAI-compatible client at the configured API.

    Returns a short status line (never prints the key).  `mode=auto` follows
    inference.yaml; `cloud` is the explicit dev/OLVLOOP escape hatch that
    resolves credentials from the local opencode config.
    """
    from engines.provider_config import load_env_file, load_inference, resolve_llm

    load_env_file()
    inf = load_inference()
    provider = str((inf.get("llm") or {}).get("provider") or "local")
    if mode == "local" or (mode == "auto" and provider != "openai_compat"):
        return "local llama-server :8080"

    if mode == "cloud" and provider != "openai_compat":
        from engines.llm_api import resolve_llm_config

        base_url, api_key, model = resolve_llm_config()
    else:
        _, base_url, model = resolve_llm(inf)
        profile = (inf.get("llm") or {}).get("openai_compat") or {}
        env_name = str(profile.get("api_key_env") or "WAFER_LLM_API_KEY")
        api_key = os.environ.get(env_name, "").strip()
        if not api_key:
            raise RuntimeError(
                f"missing {env_name}; put the cloud LLM key in .env or "
                "the process environment")

    llm = conf["character_config"]["agent_config"]["llm_configs"][
        "openai_compatible_llm"]
    llm.update({"base_url": base_url, "llm_api_key": api_key,
                "model": model})
    return f"cloud {model} @ {base_url}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hotwords", default="小饼小饼,小饼干活,小饼",
                    help="comma list passed to Qwen3-ASR")
    ap.add_argument("--llm", choices=("auto", "local", "cloud"),
                    default="auto",
                    help="auto = follow inference.yaml providers.local "
                         "overlay; local = llama-server :8080; cloud = "
                         "OpenAI-compatible stand-in resolved from opencode")
    args = ap.parse_args(argv)

    if not TEMPLATE.exists():
        print(f"template missing: {TEMPLATE}", file=sys.stderr)
        return 2
    conf = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8")) or {}
    hotwords = [w.strip() for w in args.hotwords.split(",") if w.strip()]
    conf = build(conf, hotwords)

    try:
        llm_status = _apply_cloud_llm(conf, args.llm)
    except (RuntimeError, FileNotFoundError) as exc:
        print(f"LLM provider config error: {exc}", file=sys.stderr)
        return 2
    print(f"llm: {llm_status}")

    TARGET.write_text(
        yaml.safe_dump(conf, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
