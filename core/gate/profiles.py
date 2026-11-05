"""Gate setup loading: speakers.yaml -> profiles + thresholds + engine paths.

The yaml is the single source of truth for who is enrolled, their roles,
gate thresholds, and where the CAM++ model lives. Format (config/speakers.yaml):

    voiceprint:
      model: models/campplus_cn_common.bin   # absent -> sim verifier
      embedding_dir: data/speakers
    speakers:
      master:
        name: 主人
        role: admin
        priority: 1
        embedding: data/speakers/master.json
    gate:
      t_pass: 0.62
      t_gray: 0.55
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from core.gate.policy import GateConfig, SpeakerProfile

DEFAULT_CONFIG_PATH = Path("config/speakers.yaml")


@dataclass(frozen=True, slots=True)
class GateSetup:
    """Everything the gate chain needs, loaded from one yaml file."""

    profiles: dict[str, SpeakerProfile] = field(default_factory=dict)
    config: GateConfig = field(default_factory=GateConfig)
    voiceprint_model: Path | None = None
    embedding_dir: Path = Path("data/speakers")


def load_gate_setup(config_path: Path = DEFAULT_CONFIG_PATH) -> GateSetup:
    """Parse speakers.yaml into a GateSetup (missing file -> all defaults)."""
    config_path = Path(config_path)
    if not config_path.exists():
        return GateSetup()

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    vp = raw.get("voiceprint") or {}
    model = vp.get("model")
    model_path = Path(model) if model else None
    embed_dir = Path(vp.get("embedding_dir", "data/speakers"))

    profiles: dict[str, SpeakerProfile] = {}
    for speaker_id, spec in (raw.get("speakers") or {}).items():
        profiles[str(speaker_id)] = SpeakerProfile(
            speaker_id=str(speaker_id),
            name=str(spec.get("name", speaker_id)),
            role=str(spec.get("role", "member")),
            priority=int(spec.get("priority", 99)),
        )

    gate_raw = raw.get("gate") or {}
    config = GateConfig(
        t_pass=float(gate_raw.get("t_pass", 0.62)),
        t_gray=float(gate_raw.get("t_gray", 0.55)),
    )

    return GateSetup(
        profiles=profiles,
        config=config,
        voiceprint_model=model_path,
        embedding_dir=embed_dir,
    )
