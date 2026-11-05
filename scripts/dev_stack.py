"""One-command start / stop / status for the whole local stack.

Started processes: inference trio (serve_inference), hub (core.bus), render
(LED text + mood), bridge (opencode dispatch) and OLV (voice page).  The
script supervises them; if any child dies -- or you press Ctrl+C -- it calls
``scripts/stop_all.py`` so no half-dead stack (orphan llama-server, second
render holding the HID handle) survives.

    .\\.venv\\Scripts\\python.exe scripts/dev_stack.py start
    .\\.venv\\Scripts\\python.exe scripts/dev_stack.py start --no-olv --no-device
    .\\.venv\\Scripts\\python.exe scripts/dev_stack.py status
    .\\.venv\\Scripts\\python.exe scripts/dev_stack.py stop

Logs: logs/hub.log, logs/render.log, logs/bridge.log, logs/olv.log,
logs/infer_{llm,asr,tts}.log. All services run with UTF-8 output.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "logs"
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")

WAIT_PORTS = {8765: "hub", 8790: "render", 12393: "olv"}
_LOCK_HANDLE = None


def _acquire_instance_lock():
    """Hold a Windows file lock for the lifetime of this stack process."""
    global _LOCK_HANDLE
    import msvcrt

    LOGS.mkdir(exist_ok=True)
    path = LOGS / "dev_stack.lock"
    handle = path.open("a+", encoding="utf-8")
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _LOCK_HANDLE = handle
    return handle


def _env(always_on: bool, render_debug: bool = False) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    if always_on:
        env["WAFER_ALWAYS_ON"] = "1"
    else:
        env.pop("WAFER_ALWAYS_ON", None)
    if render_debug:
        env["WAFER_RENDER_DEBUG"] = "1"
    return env


def _port_open(port: int) -> bool:
    import socket

    with socket.socket() as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _spawn(name: str, args: list[str], env: dict[str, str]
           ) -> subprocess.Popen:
    LOGS.mkdir(exist_ok=True)
    log = (LOGS / f"{name}.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(args, cwd=ROOT, env=env, stdout=log,
                            stderr=subprocess.STDOUT)
    log.close()
    print(f"started {name} (pid {proc.pid}) -> logs/{name}.log", flush=True)
    return proc


def _wait(port: int, label: str, timeout: float = 60.0) -> bool:
    began = time.monotonic()
    while time.monotonic() - began < timeout:
        if _port_open(port):
            print(f"{label} up on :{port} after "
                  f"{time.monotonic() - began:.1f}s", flush=True)
            return True
        time.sleep(1.0)
    print(f"{label} did NOT open :{port}", flush=True)
    return False


def stop_all() -> None:
    subprocess.run([PY, str(ROOT / "scripts" / "stop_all.py")], cwd=ROOT)


def start(args: argparse.Namespace) -> int:
    if _acquire_instance_lock() is None:
        print("another dev_stack is already running; use status/stop first",
              file=sys.stderr)
        return 2
    env = _env(not args.strict_wake, args.render_debug)
    children: dict[str, subprocess.Popen] = {}

    print("== cleaning leftovers ==", flush=True)
    if args.no_trio:
        print("--no-trio: keeping the running inference trio", flush=True)
    else:
        stop_all()

    if not args.no_trio:
        print("== inference trio ==", flush=True)
        only = ["llm", "asr", "tts"]
        if args.no_tts:
            only.remove("tts")
        code = subprocess.call([PY, str(ROOT / "scripts" / "serve_inference.py"),
                                "--start", "--only", ",".join(only)],
                               cwd=ROOT, env=env)
        if code != 0:
            print("trio failed to start; aborting", flush=True)
            return code

    children["hub"] = _spawn("hub", [PY, "-m", "core.bus"], env)
    if not _wait(8765, "hub"):
        stop_all()
        return 1

    if not args.no_bridge:
        children["bridge"] = _spawn("bridge", [PY, "-m", "bridge", "--serve"],
                                    env)

    render_args = [PY, "-m", "render", "--host", "127.0.0.1",
                   "--port", "8765", "--console", "8790"]
    if not args.no_device:
        render_args.append("--device")
    children["render"] = _spawn("render", render_args, env)
    if not _wait(8790, "render"):
        stop_all()
        return 1

    if not args.no_olv:
        # Regenerate OLV's gitignored conf.yaml from the current provider
        # config so switching local/cloud takes effect on the next start.
        print("== OLV provider config ==", flush=True)
        code = subprocess.call([PY, str(ROOT / "scripts" / "setup_olv.py")],
                               cwd=ROOT, env=env)
        if code != 0:
            print("setup_olv failed; aborting", flush=True)
            stop_all()
            return code
        children["olv"] = _spawn("olv", [PY, str(ROOT / "scripts" /
                                                 "run_olv.py")], env)
        if not _wait(12393, "olv", timeout=120.0):
            stop_all()
            return 1

    print("== stack up; press Ctrl+C to stop everything ==", flush=True)
    try:
        while True:
            for name, proc in children.items():
                code = proc.poll()
                if code is not None:
                    print(f"!! {name} exited with {code}; stopping stack",
                          flush=True)
                    stop_all()
                    return 1
            time.sleep(2.0)
    except KeyboardInterrupt:
        print("== stopping ==", flush=True)
        stop_all()
        return 0


def status() -> int:
    rows = []
    for port, label in [(8765, "hub"), (8790, "render"), (8080, "llm"),
                        (8001, "asr"), (9880, "tts"), (12393, "olv")]:
        rows.append((label, port, "UP" if _port_open(port) else "DOWN"))
    for label, port, state in rows:
        print(f"{label:<8} :{port:<6} {state}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    start_ap = sub.add_parser("start")
    start_ap.add_argument("--no-trio", action="store_true")
    start_ap.add_argument("--no-tts", action="store_true",
                          help="skip TTS (debugging ASR/LLM only)")
    start_ap.add_argument("--no-olv", action="store_true")
    start_ap.add_argument("--no-bridge", action="store_true")
    start_ap.add_argument("--no-device", action="store_true",
                          help="do not attach PixelBar HID sinks")
    start_ap.add_argument("--strict-wake", action="store_true",
                          help="do NOT force WAFER_ALWAYS_ON (uses "
                               "WAFER_WAKE_MODE; default asr)")
    start_ap.add_argument("--no-cleanup", action="store_true")
    start_ap.add_argument("--render-debug", action="store_true",
                          help="log every LED text push into render.log")
    sub.add_parser("stop")
    sub.add_parser("status")
    args = ap.parse_args(argv)

    if args.cmd == "start":
        return start(args)
    if args.cmd == "stop":
        stop_all()
        return 0
    return status()


if __name__ == "__main__":
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.exit(main())
