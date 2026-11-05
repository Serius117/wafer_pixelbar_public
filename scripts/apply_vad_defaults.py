"""Patch the OLV frontend bundle: looser default VAD + fresh settings key.

Why: the shipped defaults (positive 50 / negative 35 / redemption 35) fire
``error.vadMisfire`` on short/normal utterances. The bundle is minified and
rebuilt on upgrade, so a textual patch is not stable -- this script re-applies
the two targeted replacements to whatever ``assets/main-*.js`` exists.

The localStorage key is renamed (``vadSettings`` -> ``vadSettings2``) so a
browser profile that already stored the strict values picks up the new
defaults instead of silently overriding them.

    .\\.venv\\Scripts\\python.exe scripts/apply_vad_defaults.py
    .\\.venv\\Scripts\\python.exe scripts/apply_vad_defaults.py --revert
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "olv" / "upstream" / "Open-LLM-VTuber" / "frontend" / "assets"

OLD_DEFAULTS = ("{positiveSpeechThreshold:50,negativeSpeechThreshold:35,"
                "redemptionFrames:35}")
NEW_DEFAULTS = ("{positiveSpeechThreshold:30,negativeSpeechThreshold:20,"
                "redemptionFrames:50}")
OLD_KEY = 'useLocalStorage("vadSettings",'
NEW_KEY = 'useLocalStorage("vadSettings2",'
# 2026-10-25: browser AGC + noise-suppression mangled the wake word (real-voice
# dump scored 0.0 for KWS while the same voice recorded via WASAPI scored 1.0;
# peak 1.0 = clipping). Keep echoCancellation (AEC is required for barge-in).
OLD_MIC = "echoCancellation:!0,autoGainControl:!0,noiseSuppression:!0"
# 2026-10-25 v2: noiseSuppression off (it mangled the wake word), AGC back ON
# (with AGC off, quiet speech landed at rms -31 dBFS and KWS scored 0.000).
NEW_MIC = "echoCancellation:!0,autoGainControl:!0,noiseSuppression:!1"
MIC_LEGACY = "echoCancellation:!0,autoGainControl:!1,noiseSuppression:!1"
# Diagnostic switch (R.20d): AEC may suppress near-end speech while TTS is
# playing, which kills barge-in. `--echo-off` turns it off for one experiment.
MIC_EC_OFF = "echoCancellation:!1,autoGainControl:!1,noiseSuppression:!1"
# 2026-10-25: a persisted autoStopMic=true made the UI turn the mic off after
# every utterance and never reopen it -- two turns in a row were impossible and
# barge-in could not work. Rename the stored keys so the safe defaults apply:
#   autoStopMic=false (keep listening) and autoStartMicOnConvEnd stays false.
OLD_AUTOSTOP = 'useLocalStorage("autoStopMic",'
NEW_AUTOSTOP = 'useLocalStorage("autoStopMic2",'
# TASK.KWS.02: OLV's current bundle prepends 20 VAD frames (~0.64s) before
# speech. Real wake phrases lost their onset ("小饼小饼" became one 0.6s
# syllable) and KWS missed; 60 frames gives ~1.9s of pre-roll.
OLD_PREPAD = "preSpeechPadFrames:20"
NEW_PREPAD = "preSpeechPadFrames:60"


def patch_file(path: Path, revert: bool, echo_off: bool = False) -> bool:
    text = path.read_text(encoding="utf-8")
    old, new = (NEW_DEFAULTS, OLD_DEFAULTS) if revert else (OLD_DEFAULTS,
                                                           NEW_DEFAULTS)
    old_k, new_k = (NEW_KEY, OLD_KEY) if revert else (OLD_KEY, NEW_KEY)
    old_m, new_m = (NEW_MIC, OLD_MIC) if revert else (OLD_MIC, NEW_MIC)
    old_s, new_s = (NEW_AUTOSTOP, OLD_AUTOSTOP) if revert else (OLD_AUTOSTOP,
                                                               NEW_AUTOSTOP)
    old_p, new_p = (NEW_PREPAD, OLD_PREPAD) if revert else (OLD_PREPAD,
                                                           NEW_PREPAD)
    changed = False
    for before, after in ((old, new), (old_k, new_k), (old_m, new_m),
                          (old_s, new_s), (old_p, new_p)):
        if before in text:
            text = text.replace(before, after)
            changed = True
    if echo_off:
        for variant in (NEW_MIC, MIC_LEGACY, OLD_MIC):
            if variant in text:
                text = text.replace(variant, MIC_EC_OFF)
                changed = True
    else:
        for variant in (MIC_EC_OFF, MIC_LEGACY, OLD_MIC):
            if variant in text:
                text = text.replace(variant, NEW_MIC)
                changed = True
    if changed:
        path.write_text(text, encoding="utf-8", newline="\n")
    return changed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--echo-off", action="store_true",
                    help="diagnostic: disable echoCancellation too")
    args = ap.parse_args(argv)

    bundles = sorted(FRONTEND.glob("main-*.js"))
    if not bundles:
        print(f"no bundle under {FRONTEND}", flush=True)
        return 1
    for path in bundles:
        changed = patch_file(path, args.revert, args.echo_off)
        print(f"{path.name}: {'patched' if changed else 'already ok'}",
              flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
