"""Gate threshold calibration (T5.4): FAR/FRR sweep + threshold recommendation.

Requirement §12#2: 误响应容忍 = 0 (zero false-accept), so the recommended
t_pass is the smallest threshold with FAR == 0, i.e. above the best imposter
score. t_gray keeps the default 0.07 gap below t_pass (configurable).

Input modes:
  --sim                synthetic speakers (deterministic, CI / demo)
  --enrolled-dir D     dirs of per-speaker wavs: D/<speaker_id>/*.wav (real)

Output: JSON table of (t, FAR, FRR) + recommendation, to stdout or --out file.

Examples:
  .venv\\Scripts\\python.exe scripts\\calibrate_gate.py --sim
  .venv\\Scripts\\python.exe scripts\\calibrate_gate.py --enrolled-dir data/enroll_check ^
      --model models/campplus_cn_common.bin --out temp_script\\calib.json
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.gate.policy import GateConfig
from engines.voiceprint import (
    DEFAULT_DIM,
    SAMPLE_RATE,
    CampplusVerifier,
    EnrollmentStore,
    cosine_similarity,
    normalize,
)

logger = logging.getLogger(__name__)


def synthetic_score_sets(
    n_speakers: int = 4,
    samples_per_speaker: int = 6,
    noise: float = 0.04,
    seed: int = 42,
) -> tuple[list[float], list[float]]:
    """Deterministic synthetic genuine/imposter score distributions.

    Per-dim noise std 0.04 over 192 dims gives noise norm ~0.55 vs unit
    base (genuine cosine ~0.9); random 192-dim unit vectors are near
    orthogonal (imposter cosine ~0 +/- 0.07) -- clean separation.
    """
    rng = random.Random(seed)

    # per-speaker base unit vectors
    bases = []
    for _ in range(n_speakers):
        bases.append(normalize([rng.gauss(0.0, 1.0) for _ in range(DEFAULT_DIM)]))

    # per-sample embeddings = base + noise, renormalized
    per_speaker: list[list[list[float]]] = []
    for base in bases:
        samples = []
        for _ in range(samples_per_speaker):
            noisy = [b + noise * rng.gauss(0.0, 1.0) for b in base]
            samples.append(normalize(noisy))
        per_speaker.append(samples)

    genuine: list[float] = []
    imposter: list[float] = []
    for i in range(n_speakers):
        for j in range(n_speakers):
            for a in per_speaker[i]:
                for b in per_speaker[j]:
                    s = cosine_similarity(a, b)
                    if i == j:
                        genuine.append(s)
                    else:
                        imposter.append(s)
    return genuine, imposter


def real_score_sets(enrolled_dir: Path, model_path: Path) -> tuple[list[float], list[float]]:
    """Genuine/imposter scores from per-speaker wav dirs + CAM++ model."""
    verifier = CampplusVerifier(EnrollmentStore(Path("data/speakers")), model_path)

    speaker_files: dict[str, list[bytes]] = {}
    for speaker_dir in sorted(enrolled_dir.iterdir()):
        if not speaker_dir.is_dir():
            continue
        pcms = []
        for wav_path in sorted(speaker_dir.glob("*.wav")):
            import wave

            with wave.open(str(wav_path), "rb") as w:
                if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1:
                    raise ValueError(f"{wav_path}: need 16k mono s16 wav")
                pcms.append(w.readframes(w.getnframes()))
        if pcms:
            speaker_files[speaker_dir.name] = pcms

    if len(speaker_files) < 2:
        raise SystemExit("need at least 2 speaker dirs under --enrolled-dir")

    embs = {
        sid: [verifier.embed(pcm) for pcm in pcms]
        for sid, pcms in speaker_files.items()
    }
    ids = sorted(embs)
    genuine: list[float] = []
    imposter: list[float] = []
    for i, a in enumerate(ids):
        for b in ids[i:]:
            for ea in embs[a]:
                for eb in embs[b]:
                    s = cosine_similarity(ea, eb)
                    (genuine if a == b else imposter).append(s)
    return genuine, imposter


def far_frr(genuine: list[float], imposter: list[float], t: float) -> tuple[float, float]:
    """FAR/FRR at threshold t (FAR = imposter accepted rate)."""
    far = sum(1 for s in imposter if s >= t) / max(len(imposter), 1)
    frr = sum(1 for s in genuine if s < t) / max(len(genuine), 1)
    return far, frr


def sweep(
    genuine: list[float],
    imposter: list[float],
    t_min: float = 0.30,
    t_max: float = 0.95,
    step: float = 0.01,
) -> list[dict]:
    rows = []
    t = t_min
    while t <= t_max + 1e-9:
        far, frr = far_frr(genuine, imposter, t)
        rows.append({"t": round(t, 4), "far": round(far, 4), "frr": round(frr, 4)})
        t += step
    return rows


def recommend(
    genuine: list[float],
    imposter: list[float],
    margin: float = 0.02,
    gray_gap: float = 0.07,
) -> dict:
    """Pick thresholds honoring 误触=0: t_pass above the best imposter score.

    margin: safety margin above max(imposter); gray_gap: t_pass - t_gray.
    """
    if not genuine:
        raise ValueError("no genuine scores")
    best_imposter = max(imposter) if imposter else 0.0
    worst_genuine = min(genuine)

    t_pass = round(best_imposter + margin, 4)
    if t_pass >= worst_genuine:
        # distributions overlap: cannot guarantee FAR=0 without sacrificing
        # every genuine attempt -- report honestly, clamp below worst_genuine
        t_pass = round(worst_genuine - 0.001, 4)
        overlap = True
    else:
        overlap = False

    t_gray = round(max(t_pass - gray_gap, best_imposter), 4)
    far, frr = far_frr(genuine, imposter, t_pass)
    return {
        "t_pass": t_pass,
        "t_gray": t_gray,
        "far_at_t_pass": round(far, 4),
        "frr_at_t_pass": round(frr, 4),
        "best_imposter": round(best_imposter, 4),
        "worst_genuine": round(worst_genuine, 4),
        "distributions_overlap": overlap,
        "note": (
            "FAR=0 achievable" if not overlap else
            "WARNING: genuine/imposter overlap -- collect more samples or a stronger model"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Calibrate voiceprint gate thresholds")
    parser.add_argument("--sim", action="store_true", help="synthetic distributions")
    parser.add_argument("--enrolled-dir", type=Path, default=None,
                        help="dir containing per-speaker subdirs of wavs")
    parser.add_argument("--model", type=Path, default=None, help="CAM++ model path")
    parser.add_argument("--margin", type=float, default=0.02)
    parser.add_argument("--gray-gap", type=float, default=0.07)
    parser.add_argument("--out", type=Path, default=None, help="write JSON here (else stdout)")
    parser.add_argument("--rows", action="store_true", help="include the full sweep table")
    args = parser.parse_args(argv)

    if args.sim:
        genuine, imposter = synthetic_score_sets()
    elif args.enrolled_dir and args.model:
        genuine, imposter = real_score_sets(args.enrolled_dir, args.model)
    else:
        raise SystemExit("choose --sim, or --enrolled-dir + --model")

    if not genuine:
        raise SystemExit("no genuine scores computed")

    report = {
        "n_genuine": len(genuine),
        "n_imposter": len(imposter),
        "recommendation": recommend(genuine, imposter, args.margin, args.gray_gap),
        "current_defaults": {"t_pass": GateConfig().t_pass, "t_gray": GateConfig().t_gray},
    }
    if args.rows:
        report["sweep"] = sweep(genuine, imposter)

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"written: {args.out}")
    print(text)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
