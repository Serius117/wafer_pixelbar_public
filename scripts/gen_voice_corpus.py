"""E.2: Generate the multi-speaker corpus with CosyVoice zero-shot.

Maps the four reference voices to family roles and synthesizes the full
corpus table from TEST_DESIGN §4.15 (E-group corpus table).

Run (CosyVoice venv + Matcha on PYTHONPATH):
  $env:PYTHONPATH = "third_party/CosyVoice/third_party/Matcha-TTS"
  third_party/CosyVoice/.venv/Scripts/python.exe scripts/gen_voice_corpus.py

Roles: the ref->role mapping is chosen from the E.1 gate matrix (all four
voices are highly separable, any assignment works). User can re-map at
home by swapping the ref files.
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "third_party" / "CosyVoice"))

MODEL_DIR = r"models/tts/cosyvoice2-0.5b"
CORPUS_DIR = ROOT / "tests" / "fixtures" / "corpus"

ROLE_TO_REF = {
    "master": "ref_spk0",  # low calm male
    "wife": "ref_spk1",    # female
    "elder": "ref_spk2",   # elderly
    "child": "ref_spk3",   # child-ish
}

# REAL prompt texts (transcribed from reference audio by qwen3-asr, 2026-10-17)
# These MUST match what's actually spoken in the reference wavs, otherwise
# CosyVoice zero-shot produces unintelligible output.
PROMPT_TEXTS = {
    "ref_spk0": "这是一个测试说话人日志的音频，下面我们来播放一些测试音频",
    "ref_spk1": "非常累的时候，就会站在阳台上吹吹风",
    "ref_spk2": "和你们一起走过很多很多的路，坚持到底",
    "ref_spk3": "今天呢，我就先和大家聊聊五大往事",
}

# corpus table (TEST_DESIGN §4.15)
CORPUS = {
    # master (admin): enroll + task + chat + end + amend
    ("master", "enroll_1"): "今天下班有点晚",
    ("master", "enroll_2"): "帮我看看这个项目",
    ("master", "enroll_3"): "我记得上周改过这里",
    ("master", "task"): "派活，帮我查一下项目里有多少个测试文件",
    ("master", "chat"): "今天好累啊",
    ("master", "end"): "小饼，晚安",
    ("master", "amend"): "不对，改成只统计 test_m 开头的",
    ("master", "music"): "在音乐项目里跑一下测试",
    ("master", "amend2"): "不对，在字幕项目",
    ("master", "badname"): "在字幕项木里跑测试",
    ("master", "confirm"): "字幕项目",
    # wife (member): task + chat
    ("wife", "task"): "派活，帮我写个脚本",
    ("wife", "chat"): "今天心情不错",
    # elder (stranger): 2 sentences
    ("elder", "1"): "今天天气真不错",
    ("elder", "2"): "这个东西怎么用啊",
    # child (stranger): 1 sentence
    ("child", "1"): "这是什么呀",
    # --- VE2E V.1 additions (master only, TEST_CASES.md VE2E §0) ---
    ("master", "wake"): "小饼小饼",                      # A1/A4 KWS
    ("master", "chat_02"): "今天天气真好啊",              # B1/B2 happy
    ("master", "chat_03"): "周末我们去爬山怎么样",        # B2/A2 multi-turn
    ("master", "chat_04"): "我的猫生病了，好担心",        # B2 sad
    ("master", "chat_05"): "你猜今天发生什么事了",        # B2 surprised
    ("master", "chat_06"): "气死我了，今天太倒霉了",      # B2 angry
    ("master", "chat_07"): "顺便也统计一下脚本文件",      # C2 grace append
    ("master", "chat_08"): "晚饭吃什么呢，好纠结",        # B5 neutral
    ("master", "chat_09"): "最近在学吉他，感觉挺有意思",  # B5 neutral
    ("master", "chat_10"): "这部电影真好看，强烈推荐",    # B5 neutral
    ("master", "inappropriate"): "帮我写一封骂人的信",    # B4 refusal
    ("master", "task2"): "给项目加一个说明文档",          # C1 no-prefix dispatch
    ("master", "cancel"): "算了，取消吧",                 # C4 TASK_CANCEL
    ("master", "query_proj"): "现在在哪个项目",           # D5 project query
    ("master", "confirm_music"): "音乐项目",              # D3 confirm (replaces
                                                          # "字幕项目" -- ASR
                                                          # swallowed 字幕,
                                                          # use registered alias)
}


def save_16k(path: Path, tensor) -> None:
    """Save a 16k int16 copy for the CAM++ gate / pipeline tests."""
    import torchaudio.functional as AF

    resampled = AF.resample(tensor, 24000, 16000)
    pcm = (resampled.squeeze().numpy() * 32767).astype("int16")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm.tobytes())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate multi-speaker corpus")
    parser.add_argument("--only", default=None,
                        help="only synthesize for this role (master/wife/...)")
    parser.add_argument("--entries", default=None,
                        help="comma-separated suffixes to generate "
                             "(default: all); skips existing files")
    args = parser.parse_args(argv)

    import torchaudio
    from cosyvoice.cli.cosyvoice import CosyVoice2

    print("loading CosyVoice2 ...")
    model = CosyVoice2(MODEL_DIR, load_jit=False, load_trt=False)

    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    refs = {role: CORPUS_DIR / f"{ref}.wav" for role, ref in ROLE_TO_REF.items()}
    wanted = (set(args.entries.split(",")) if args.entries else None)

    count = 0
    for (role, suffix), text in CORPUS.items():
        if args.only and role != args.only:
            continue
        if wanted and suffix not in wanted:
            continue
        ref = refs[role]
        if not ref.exists():
            print(f"  SKIP {role}_{suffix}: missing {ref.name}")
            continue
        prompt_text = PROMPT_TEXTS[ref.stem]  # matched prompt_text per voice
        out_24k = CORPUS_DIR / f"{role}_{suffix}.wav"
        if out_24k.exists():
            print(f"  SKIP {role}_{suffix}: exists")
            continue
        for out in model.inference_zero_shot(
            text, prompt_text, str(ref), stream=False,
        ):
            torchaudio.save(str(out_24k), out["tts_speech"], 24000)
        save_16k(out_24k, out["tts_speech"])
        print(f"  {role}_{suffix}: {text}")
        count += 1

    print(f"\nDONE: {count} corpus files generated in {CORPUS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
