"""Keyword config generator for sherpa-onnx KWS (T6.2, D31).

Turns a wake word into the per-token pinyin line the KWS model expects:

    小饼  ->  "x iǎo b ǐng @小饼"

Pipeline: pypinyin (with tone) -> initial/final split -> validated against
the model's tokens.txt (a bad token fails loudly with near-matches).

Usage:
  python scripts/make_keywords.py --word 小饼 --repeat 2
  python scripts/make_keywords.py --word 小饼 --out my_keywords.txt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_TOKENS = ROOT / "models" / "wakeword" / "sherpa-kws-wenetspeech" / "tokens.txt"
DEFAULT_OUT = ROOT / "models" / "wakeword" / "sherpa-kws-wenetspeech" / "keywords.txt"

INITIALS = (
    "zh", "ch", "sh",  # two-letter first (longest match)
    "b", "p", "m", "f", "d", "t", "n", "l",
    "g", "k", "h", "j", "q", "x", "r", "z", "c", "s", "y", "w",
)


def split_syllable(syllable: str) -> tuple[str, str]:
    """Split one toned pinyin syllable into (initial, final)."""
    for ini in INITIALS:
        if syllable.startswith(ini):
            return ini, syllable[len(ini):]
    return "", syllable  # zero-initial syllable (e.g. ài)


def word_to_tokens(word: str) -> list[str]:
    """Chinese word -> flat per-token list (initial/final alternating)."""
    from pypinyin import Style, lazy_pinyin

    tokens: list[str] = []
    for syllable in lazy_pinyin(word, style=Style.TONE):
        ini, final = split_syllable(syllable)
        if ini:
            tokens.append(ini)
        if final:
            tokens.append(final)
    return tokens


def validate_tokens(tokens: list[str], tokens_file: Path) -> list[str]:
    """Every token must exist in the model's token table; report near-misses."""
    table = {
        line.split()[0]
        for line in tokens_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    problems = []
    for tok in tokens:
        if tok not in table:
            near = [t for t in table if t and tok and t[0] == tok[0] and len(t) <= 4][:6]
            problems.append(f"token {tok!r} not in {tokens_file.name}; near: {near}")
    return problems


def build_line(word: str, repeat: int = 1) -> tuple[str, list[str]]:
    tokens = word_to_tokens(word) * max(1, repeat)
    return " ".join(tokens) + f" @{word}", tokens


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KWS keyword line generator")
    parser.add_argument("--word", required=True, help="wake word, e.g. 小饼")
    parser.add_argument("--repeat", type=int, default=2,
                        help="repeat the word N times (default 2 -> 小饼小饼)")
    parser.add_argument("--extra-word", default="",
                        help="optional second wake word, e.g. 小饼干活")
    parser.add_argument("--extra-repeat", type=int, default=1,
                        help="repeat count for --extra-word (default 1)")
    parser.add_argument("--tokens", type=Path, default=DEFAULT_TOKENS)
    parser.add_argument("--out", type=Path, default=None,
                        help="rewrite the keywords file (default: print only)")
    args = parser.parse_args(argv)

    words = [(args.word, args.repeat)]
    if args.extra_word:
        words.append((args.extra_word, args.extra_repeat))
    lines: list[str] = []
    for word, repeat in words:
        line, tokens = build_line(word, repeat)
        print(f"word={word!r} repeat={repeat}")
        print(f"tokens: {tokens}")
        print(f"line:   {line}")
        problems = validate_tokens(tokens, args.tokens)
        if problems:
            print("\nVALIDATION FAILED:")
            for p in problems:
                print(f"  - {p}")
            return 1
        lines.append(line)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nwritten: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
