"""Launch the OLV audio host with wafer's inference trio (R.13).

- verifies the trio (:8080/:8001/:9880) is healthy (warns, does not block)
- makes `ffmpeg` available on PATH (pydub in OLV needs it; provided by the
  imageio-ffmpeg wheel inside OLV's venv, copied to data/bin/ffmpeg.exe)
- starts `run_server.py` from the OLV fork, logs to logs/olv.log
- prints the Edge command for the audio page

    .\\.venv\\Scripts\\python.exe scripts/run_olv.py [--open-edge]

Stop it with Ctrl+C (or close the console); `scripts/start_all` stays unaware
until R.13 is finished.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
OLV_DIR = ROOT / "olv" / "upstream" / "Open-LLM-VTuber"
OLV_PY = OLV_DIR / ".venv" / "Scripts" / "python.exe"
FFMPEG_BIN = ROOT / "data" / "bin" / "ffmpeg.exe"
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
AUDIO_PAGE = "http://127.0.0.1:12393/"


def ensure_ffmpeg() -> Path | None:
    found = shutil.which("ffmpeg")
    if found:
        return Path(found)
    try:
        out = subprocess.run(
            [str(OLV_PY), "-c",
             "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"],
            capture_output=True, text=True, timeout=60, check=True)
    except Exception as exc:  # noqa: BLE001
        print(f"ffmpeg unavailable: {exc}", file=sys.stderr)
        return None
    source = Path(out.stdout.strip())
    if not source.exists():
        return None
    FFMPEG_BIN.parent.mkdir(parents=True, exist_ok=True)
    if not FFMPEG_BIN.exists() or FFMPEG_BIN.stat().st_size != source.stat().st_size:
        shutil.copy2(source, FFMPEG_BIN)
    return FFMPEG_BIN


def trio_status() -> list[str]:
    from engines.provider_config import load_inference

    conf = load_inference()
    llm_provider = str((conf.get("llm") or {}).get("provider") or "local")
    rows = []
    for name, url in (("llm", "http://127.0.0.1:8080/health"),
                      ("asr", "http://127.0.0.1:8001/health"),
                      ("tts", "http://127.0.0.1:9880/health")):
        if name == "llm" and llm_provider == "openai_compat":
            rows.append("llm=cloud")
            continue
        try:
            ok = httpx.get(url, timeout=2.0).status_code == 200
        except Exception:
            ok = False
        rows.append(f"{name}={'ok' if ok else 'DOWN'}")
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--open-edge", action="store_true",
                    help="open the audio page in an Edge app window first")
    args = ap.parse_args(argv)

    print("trio:", " ".join(trio_status()), flush=True)
    if not (OLV_DIR / "conf.yaml").exists():
        print("conf.yaml missing: run scripts/setup_olv.py first",
              file=sys.stderr)
        return 2

    ffmpeg = ensure_ffmpeg()
    env = os.environ.copy()
    if ffmpeg:
        env["PATH"] = str(ffmpeg.parent) + os.pathsep + env.get("PATH", "")
        print(f"ffmpeg: {ffmpeg.relative_to(ROOT)}", flush=True)

    if args.open_edge:
        if EDGE.exists():
            subprocess.Popen([str(EDGE), f"--app={AUDIO_PAGE}"])
            print(f"Edge app window -> {AUDIO_PAGE}", flush=True)
        else:
            print(f"Edge not found at {EDGE}", file=sys.stderr)

    log = (ROOT / "logs" / "olv.log").open("w", encoding="utf-8")
    proc = subprocess.Popen([str(OLV_PY), "run_server.py"], cwd=OLV_DIR,
                            env=env, stdout=log, stderr=subprocess.STDOUT)
    print(f"OLV pid {proc.pid}; page: {AUDIO_PAGE}", flush=True)
    try:
        while proc.poll() is None:
            time.sleep(1)
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    finally:
        log.close()
    return proc.returncode or 0


if __name__ == "__main__":
    sys.exit(main())
