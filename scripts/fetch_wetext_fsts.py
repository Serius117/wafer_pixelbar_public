"""Download the wetext text-normalization FSTs into models/wetext/ (R.19).

Why: CosyVoice's text frontend imports `wetext`, whose Normalizer calls
`snapshot_download("pengzhendong/wetext")` -- that needs an authenticated
ModelScope session on this machine ("Authentication token does not exist").
We keep the four FSTs locally and `scripts/cosyvoice_server.py` points wetext
at them (install_offline_wetext), so no token is needed.

    .\\.venv\\Scripts\\python.exe scripts/fetch_wetext_fsts.py
    .\\.venv\\Scripts\\python.exe scripts/fetch_wetext_fsts.py --check
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "models" / "wetext"
BASE = "https://modelscope.cn/models/pengzhendong/wetext/resolve/master"
FILES = [
    ("zh/tn/tagger.fst", 527178),
    ("zh/tn/verbalizer.fst", 1069758),
    ("en/tn/tagger.fst", 5645674),
    ("en/tn/verbalizer.fst", 6398822),
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="only verify the files are present and non-empty")
    args = ap.parse_args(argv)

    status: list[dict] = []
    for rel, expected in FILES:
        target = DEST / rel
        ok = target.exists() and target.stat().st_size == expected
        if ok or args.check:
            status.append({"file": rel, "ok": ok,
                           "bytes": target.stat().st_size
                           if target.exists() else 0})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        url = f"{BASE}/{rel}"
        with httpx.stream("GET", url, timeout=180,
                          follow_redirects=True) as resp:
            resp.raise_for_status()
            with target.open("wb") as handle:
                for chunk in resp.iter_bytes():
                    handle.write(chunk)
        size = target.stat().st_size
        status.append({"file": rel, "ok": size == expected, "bytes": size,
                       "url": url})

    print(json.dumps(status, ensure_ascii=False, indent=2), flush=True)
    return 0 if all(row["ok"] for row in status) else 1


if __name__ == "__main__":
    sys.exit(main())
