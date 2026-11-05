"""KWS bench: real-voice calibration + candidate-model comparison (TASK.KWS.01).

Why threshold sweeps instead of raw scores: sherpa-onnx's Python KWS API
exposes only the hit keyword (no continuous score), so we approximate the
score by sweeping `keywords_threshold` -- for each wav we record the LOWEST
threshold that still hits ("min_hit_threshold"). Lower value = stronger
match; no hit even at the lowest threshold = miss.

Usage (run from repo root):
    # 1) collect wavs anywhere (16k mono wav recommended), split by group:
    #    positives = contains the wake word; negatives = ordinary speech
    python scripts/kws_bench.py \
        --pos temp_script/kws_pos --neg temp_script/kws_neg \
        --model models/wakeword/sherpa-kws-wenetspeech \
        --out logs/kws_bench

    # 2) compare engines (same keywords file per model, so each model
    #    needs its own keywords.txt -- see PIXELBAR_GUIDE / make_keywords):
    python scripts/kws_bench.py --pos ... --neg ... \
        --model models/wakeword/sherpa-kws-wenetspeech \
        --model models/wakeword/sherpa-kws-zh-en-3m-2025

Outputs a markdown table + JSON to logs/kws_bench_<ts>.{md,json}:
    per model x threshold: recall (positives) / false-accept (negatives)
    per model: per-file min_hit_threshold distribution + recommended
    threshold (FAR=0, then max recall) + CPU time per wav.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_THRESHOLDS = "0.05,0.10,0.15,0.20,0.25,0.30"


def wav_to_pcm16(path: Path) -> bytes:
    """Mono 16-bit wav -> s16le bytes (engine expects int16 PCM)."""
    with wave.open(str(path), "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise ValueError(f"{path}: need mono 16-bit wav "
                             f"(got {wf.getnchannels()}ch/"
                             f"{8 * wf.getsampwidth()}bit)")
        frames = wf.readframes(wf.getnframes())
        if wf.getframerate() != 16000:
            raise ValueError(f"{path}: need 16 kHz (got {wf.getframerate()})")
        return frames


def collect_wavs(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(p for p in folder.rglob("*.wav"))


def sweep_file(engines: dict[float, object], pcm: bytes) -> int:
    """Return the index of the first threshold that hits, or len(engines).

    Thresholds are fed in ascending order; KWS hits more easily at lower
    thresholds, so the first hit index approximates the match strength.
    """
    for idx, (_thr, engine) in enumerate(sorted(engines.items())):
        if engine.detect(pcm).hit:
            return idx
    return len(engines)


def summarize(files: list[Path], results: dict[str, list[int]],
              thresholds: list[float]) -> dict:
    """Pure stats: recall / false-accept per threshold + strength dist."""
    n = len(files)
    if n == 0:
        return {"files": 0}
    per_thr = []
    for idx, thr in enumerate(thresholds):
        hits = sum(1 for v in results.values() if v <= idx)
        per_thr.append({"threshold": thr, "hits": hits,
                        "rate": round(hits / n, 4)})
    values = list(results.values())
    dist = {
        "hit_at_all": sum(1 for v in values if v < len(thresholds)),
        "miss": sum(1 for v in values if v >= len(thresholds)),
        "min": min(values) if values else None,
        "median": statistics.median(values) if values else None,
        "max": max(values) if values else None,
    }
    return {"files": n, "per_threshold": per_thr, "strength": dist}


def recommend(pos_sum: dict, neg_sum: dict, thresholds: list[float]) -> dict:
    """Pick the lowest threshold where negatives do not fire (FAR=0),
    maximising recall; falls back to the best recall with reported FAR."""
    best = None
    for idx, thr in enumerate(thresholds):
        pos_hits = pos_sum["per_threshold"][idx]["hits"] if pos_sum.get(
            "files") else 0
        neg_hits = neg_sum["per_threshold"][idx]["hits"] if neg_sum.get(
            "files") else 0
        recall = (pos_hits / pos_sum["files"]) if pos_sum.get("files") else 0.0
        far = (neg_hits / neg_sum["files"]) if neg_sum.get("files") else 0.0
        cand = {"threshold": thr, "recall": round(recall, 4),
                "false_accept": round(far, 4)}
        if best is None or (far == 0 and recall > best["recall"]
                            and best["false_accept"] == 0) or (
                best["false_accept"] > 0 and far == 0):
            best = cand
    return best or {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pos", type=Path, required=True,
                    help="directory of wake-word wavs (16k mono)")
    ap.add_argument("--neg", type=Path, default=None,
                    help="directory of non-wake speech wavs")
    ap.add_argument("--model", action="append", default=[],
                    help="KWS model dir (repeatable for comparison); "
                         "default = production model")
    ap.add_argument("--keywords", default=None,
                    help="keywords.txt override (applies to all models)")
    ap.add_argument("--thresholds", default=DEFAULT_THRESHOLDS,
                    help=f"comma list, default {DEFAULT_THRESHOLDS}")
    ap.add_argument("--out", type=Path, default=Path("logs/kws_bench"),
                    help="output prefix (writes .md and .json)")
    ap.add_argument("--limit", type=int, default=0,
                    help="max files per group (0 = all)")
    args = ap.parse_args(argv)

    thresholds = [float(t) for t in args.thresholds.split(",")]
    models = args.model or [str(ROOT / "models/wakeword/"
                                "sherpa-kws-wenetspeech")]
    pos = collect_wavs(args.pos)[: args.limit or None]
    neg = collect_wavs(args.neg)[: args.limit or None] if args.neg else []
    if not pos:
        print(f"no wavs under {args.pos}")
        return 2

    from engines.wakeword import SherpaKwsEngine

    report = {"positives": str(args.pos), "negatives": str(args.neg),
              "thresholds": thresholds, "models": {}}
    lines = ["# KWS bench", "",
             f"- positives: {len(pos)} files  ·  negatives: {len(neg)} files",
             f"- thresholds: {thresholds}", ""]

    for model_dir in models:
        name = Path(model_dir).name
        print(f"== model {name}: loading {len(thresholds)} engines...")
        engines = {}
        for thr in thresholds:
            engines[thr] = SherpaKwsEngine(
                model_dir=Path(model_dir),
                keywords_file=Path(args.keywords) if args.keywords else None,
                keywords_threshold=thr)

        def run_group(files: list[Path],
                      engines: dict = engines) -> dict:
            out: dict[str, int] = {}
            t0 = time.monotonic()
            for i, wav in enumerate(files, 1):
                pcm = wav_to_pcm16(wav)
                out[str(wav)] = sweep_file(engines, pcm)
                if i % 10 == 0:
                    print(f"   {i}/{len(files)}")
            return {"hits": out,
                    "elapsed_s": round(time.monotonic() - t0, 2)}

        pos_run = run_group(pos)
        neg_run = run_group(neg) if neg else {"hits": {}, "elapsed_s": 0}
        pos_sum = summarize(pos, pos_run["hits"], thresholds)
        neg_sum = summarize(neg, neg_run["hits"], thresholds) if neg else {}
        rec = recommend(pos_sum, neg_sum, thresholds) if neg else {}
        report["models"][name] = {
            "model_dir": model_dir,
            "positives": pos_sum,
            "negatives": neg_sum,
            "recommended": rec,
            "cpu_s_per_wav": round(
                (pos_run["elapsed_s"] + neg_run["elapsed_s"])
                / max(1, len(pos) + len(neg)), 4),
            "per_file": pos_run["hits"],
        }
        lines.append(f"## {name}")
        lines.append("")
        lines.append(f"- cpu: {report['models'][name]['cpu_s_per_wav']}s/wav")
        if rec:
            lines.append(f"- **recommended: threshold {rec['threshold']} "
                         f"(recall {rec['recall']}, FAR {rec['false_accept']})**")
        lines.append("")
        lines.append("| threshold | pos hit | pos recall | neg hit | neg FAR |")
        lines.append("|---|---|---|---|---|")
        for idx, thr in enumerate(thresholds):
            ph = pos_sum["per_threshold"][idx]
            nh = (neg_sum["per_threshold"][idx] if neg_sum
                  else {"hits": "-", "rate": "-"})
            neg_files = neg_sum.get("files", 0) if neg_sum else "-"
            lines.append(f"| {thr} | {ph['hits']}/{pos_sum['files']} | "
                         f"{ph['rate']} | {nh['hits']}/{neg_files} | "
                         f"{nh['rate']} |")
        lines.append("")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    md_path = args.out.parent / f"{args.out.name}_{ts}.md"
    json_path = args.out.parent / f"{args.out.name}_{ts}.json"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    print("\n".join(lines))
    print(f"\nwrote {md_path}\n      {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
