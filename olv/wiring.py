"""Formal OLV wiring: connects wafer-core pipeline to the OLV audio server.

This module is the composition root for the real deployment (design doc §3.6).
It builds a ConversationPipeline with real OLV-backed engines.

Usage in the P2 patch (custom Agent registration):
    from olv.wiring import build_olv_pipeline
    pipeline = build_olv_pipeline(conf)
"""

from __future__ import annotations

import logging
from pathlib import Path

from core.bus.hub import InProcBus
from core.gate.policy import GateConfig, SpeakerProfile
from core.interfaces import AsrEngine, LlmEngine, SpeakerVerifier, TtsEngine
from core.pipeline import ConversationPipeline
from core.router.scorer import RouterRules
from core.session.state_machine import SessionStateMachine

logger = logging.getLogger(__name__)


def load_speakers_yaml(path: Path) -> dict[str, SpeakerProfile]:
    """Load speaker profiles from speakers.yaml."""
    import yaml

    if not path.exists():
        logger.warning("speakers.yaml not found at %s, using default admin", path)
        default = SpeakerProfile(
            speaker_id="master", name="主人", role="admin", priority=1
        )
        return {"master": default}

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    profiles: dict[str, SpeakerProfile] = {}
    for sp in data.get("speakers", []):
        sid = sp.get("voiceprint", "").stem or sp.get("name", "unknown")
        profiles[sid] = SpeakerProfile(
            speaker_id=sid,
            name=sp.get("name", sid),
            role=sp.get("role", "member"),
            priority=sp.get("priority", 99),
        )
    return profiles


def build_olv_pipeline(
    asr: AsrEngine,
    llm: LlmEngine,
    tts: TtsEngine,
    verifier: SpeakerVerifier,
    bus: InProcBus,
    config_dir: Path,
) -> ConversationPipeline:
    """Build the production ConversationPipeline with OLV-backed engines.

    Args:
        asr: ASR engine (from OLV's provider or our engines/)
        llm: LLM engine (from engines/llm.py or cloud API)
        tts: TTS engine (CosyVoice or cloud)
        verifier: Speaker verifier (CAM++ or fixture for dev)
        bus: Event bus (InProcBus for single-process, HubClient for multi)
        config_dir: Path to config/ directory
    """
    profiles = load_speakers_yaml(config_dir / "speakers.yaml")
    gate_config = GateConfig(t_pass=0.62, t_gray=0.55)
    router_rules = RouterRules()
    state_machine = SessionStateMachine(timeout_sec=8.0)

    return ConversationPipeline(
        source=None,  # OLV feeds segments via P1 patch, not via AudioSource
        verifier=verifier,
        asr=asr,
        llm=llm,
        tts=tts,
        bus=bus,
        gate_config=gate_config,
        router_rules=router_rules,
        profiles=profiles,
        state_machine=state_machine,
    )
