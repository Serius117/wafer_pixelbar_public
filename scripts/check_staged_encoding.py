"""Reject staged text files whose UTF-8 contains U+FFFD (TASK guard).

This is the mechanical backstop for the AGENTS.md rule that forbids
PowerShell file cmdlets / shell pipelines from rewriting project files:
those commands silently corrupt Chinese text, and the corruption is visible
as the Unicode replacement character (U+FFFD).

Run by .githooks/pre-commit.  Binary files (NUL bytes) are skipped.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def staged_files() -> list[str]:
    raw = subprocess.check_output(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
        cwd=ROOT,
    )
    return [p.decode("utf-8", "surrogateescape")
            for p in raw.split(b"\0") if p]


def check(path: str) -> str | None:
    try:
        raw = subprocess.check_output(["git", "show", f":{path}"], cwd=ROOT)
    except subprocess.CalledProcessError:
        return None
    if b"\0" in raw:
        return None  # binary asset
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "not valid UTF-8"
    if "\ufffd" in text:
        return "contains U+FFFD replacement character"
    return None


def main() -> int:
    bad: list[tuple[str, str]] = []
    for path in staged_files():
        reason = check(path)
        if reason:
            bad.append((path, reason))
    if not bad:
        return 0
    print("pre-commit: encoding corruption detected", file=sys.stderr)
    for path, reason in bad:
        print(f"  {path}: {reason}", file=sys.stderr)
    print("Fix with Edit/Write, then stage the file again.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
