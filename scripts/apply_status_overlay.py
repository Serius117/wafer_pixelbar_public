"""Inject the wafer status badge into the OLV page (R.21 / D46).

Copies `olv/status_overlay.js` into the OLV frontend and adds a `<script>`
tag to `index.html`. Idempotent; `--revert` removes both.

Why an overlay instead of editing the React bundle: the bundle is minified
and rebuilt on OLV upgrades; a standalone script keeps the badge independent
and reviewable.

    .\\.venv\\Scripts\\python.exe scripts/apply_status_overlay.py
    .\\.venv\\Scripts\\python.exe scripts/apply_status_overlay.py --revert
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "olv" / "status_overlay.js"
FRONTEND = (ROOT / "olv" / "upstream" / "Open-LLM-VTuber" / "frontend")
DEST = FRONTEND / "wafer-status.js"
INDEX = FRONTEND / "index.html"
TAG = '    <script src="/wafer-status.js" defer></script>\n'


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--revert", action="store_true")
    args = ap.parse_args(argv)

    if not INDEX.exists():
        print(f"missing {INDEX}", flush=True)
        return 1

    html = INDEX.read_text(encoding="utf-8")
    if args.revert:
        if TAG in html:
            INDEX.write_text(html.replace(TAG, ""), encoding="utf-8",
                             newline="\n")
        DEST.unlink(missing_ok=True)
        print("overlay removed", flush=True)
        return 0

    shutil.copyfile(SRC, DEST)
    if TAG in html:
        print("script tag already present", flush=True)
        return 0
    if "</body>" not in html:
        print("index.html has no </body>; cannot inject", flush=True)
        return 1
    html = html.replace("</body>", TAG + "</body>", 1)
    INDEX.write_text(html, encoding="utf-8", newline="\n")
    print("overlay injected (refresh the page with Ctrl+Shift+R)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
