"""Gate policy: voiceprint gate decision logic (design doc §4.2, D7, D12).

Pure function module -- zero I/O, zero hardware dependency.
The SpeakerVerifier (engine) produces similarity scores per registered speaker;
this module decides what to do with them (pass / drop / gray).

Decision rules (fail-closed, D7):
  0. Non-finite scores (NaN/±inf) are discarded first (GATE-11: engine
     glitches must NEVER pass -- inf >= t_pass would be true)
  1. Rank candidates by registration priority (master=1 < wife=2 < elder=3)
  2. First candidate with score >= t_pass -> PASS (with speaker_id)
  3. If best score is in [t_gray, t_pass) -> GRAY (drop + LED "?")
  4. All scores < t_gray -> DROP (silent)

Permission check (D12):
  - PASS carries the speaker's role (admin / member)
  - Task intent from member -> TASK_DENIED (deterministic refusal)
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from core.interfaces import GateResult


@dataclass(frozen=True, slots=True)
class SpeakerProfile:
    """One registered speaker's identity and permissions."""

    speaker_id: str
    name: str
    role: str  # "admin" | "member"
    priority: int  # lower = higher priority (master=1, wife=2, elder=3)


@dataclass(frozen=True, slots=True)
class GateConfig:
    """Gate thresholds, configurable via speakers.yaml.

    Validated at construction (GATE-12 / CC-CFG-04): bad thresholds must
    fail fast at startup, never produce garbage verdicts at runtime.
    """

    t_pass: float = 0.62
    t_gray: float = 0.55

    def __post_init__(self) -> None:
        if not (math.isfinite(self.t_pass) and math.isfinite(self.t_gray)):
            raise ValueError(f"gate thresholds must be finite: {self}")
        if not (0.0 <= self.t_gray <= self.t_pass <= 1.0):
            raise ValueError(
                f"gate thresholds invalid (need 0 <= t_gray <= t_pass <= 1): {self}"
            )


def decide(
    matches: dict[str, float],
    profiles: dict[str, SpeakerProfile],
    cfg: GateConfig,
) -> GateResult:
    """Decide gate outcome from similarity scores.

    Args:
        matches: {speaker_id: similarity_score} from the verifier engine.
        profiles: {speaker_id: SpeakerProfile} from speakers.yaml.
        cfg: threshold configuration.

    Returns:
        GateResult with verdict (pass/drop/gray), speaker_id, and best score.
    """
    if not matches:
        return GateResult(verdict="drop", speaker_id=None, score=0.0)

    # GATE-11: fail-closed against engine glitches -- NaN/inf never pass.
    finite: dict[str, float] = {}
    for sid, score in matches.items():
        try:
            value = float(score)
        except (TypeError, ValueError):
            continue  # non-numeric junk -> discard
        if math.isfinite(value):
            finite[sid] = value
    if not finite:
        return GateResult(verdict="drop", speaker_id=None, score=0.0)

    # Rank by registration priority (lower number = higher priority)
    ranked = sorted(finite, key=lambda sid: profiles[sid].priority if sid in profiles else 999)

    # First candidate above t_pass wins
    for sid in ranked:
        if finite[sid] >= cfg.t_pass:
            return GateResult(verdict="pass", speaker_id=sid, score=finite[sid])

    # No pass; check for gray zone
    best_score = max(finite.values())
    if best_score >= cfg.t_gray:
        return GateResult(verdict="gray", speaker_id=None, score=best_score)

    # All below gray threshold
    return GateResult(verdict="drop", speaker_id=None, score=best_score)


def is_admin(profile: SpeakerProfile | None) -> bool:
    """Check if a speaker has admin privileges (full access including opencode)."""
    return profile is not None and profile.role == "admin"
