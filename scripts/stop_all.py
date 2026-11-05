"""Kill every wafer-pixel runtime process (hub, render, bridge, OLV, trio).

Why: repeated "restart the stack" attempts left orphans (a GPU ASR holding
8001, a second render holding the HID handle, venv stub + real interpreter
pairs). This script is the single, reliable way to get back to a clean slate.

Targets are matched by command line (services) and by listening port
(8080/8001/9880/8765/8790/8791/12393). Only recognized executables
(python / llama-server) are killed by port, so unrelated software is safe.

    .\\.venv\\Scripts\\python.exe scripts/stop_all.py --dry-run
    .\\.venv\\Scripts\\python.exe scripts/stop_all.py
    .\\.venv\\Scripts\\python.exe scripts/stop_all.py --json

Exit code is 0 even when nothing was running.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]

SERVICE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("hub", re.compile(r"-m\s+core\.bus\b")),
    ("render", re.compile(r"-m\s+render\b")),
    ("bridge", re.compile(r"-m\s+bridge\b")),
    ("ctl", re.compile(r"-m\s+ctl\b")),
    ("gamedog", re.compile(r"-m\s+gamedog\b")),
    ("olv", re.compile(r"run_server\.py")),
    ("tts", re.compile(r"cosyvoice_server\.py")),
    ("trio-launcher", re.compile(r"serve_inference\.py")),
    ("dev-stack", re.compile(r"-m\s+scripts\.dev_stack\b")),
    ("hub-recorder", re.compile(r"hub_recorder\.py")),
]

PORTS: dict[int, str] = {
    8080: "llm",
    8001: "asr",
    9880: "tts",
    8765: "hub",
    8790: "render-console",
    8791: "render-verify",
    12393: "olv",
}

PORT_KILLABLE = {"python.exe", "pythonw.exe", "llama-server.exe",
                 "node.exe", "ffmpeg.exe"}


def _cmdline(proc: psutil.Process) -> str:
    try:
        return " ".join(proc.cmdline())
    except (psutil.Error, OSError):
        return ""


def find_targets() -> list[dict[str, str | int]]:
    me = {psutil.Process().pid, *[p.pid for p in
                                  psutil.Process().parents()]}
    found: dict[int, dict[str, str | int]] = {}

    for proc in psutil.process_iter(["pid", "name"]):
        if proc.info["pid"] in me:
            continue
        cmd = _cmdline(proc)
        if not cmd:
            continue
        for label, pattern in SERVICE_PATTERNS:
            if pattern.search(cmd):
                found[proc.info["pid"]] = {"pid": proc.info["pid"],
                                           "name": proc.info["name"] or "",
                                           "why": label, "cmd": cmd}
                break
        else:
            if "llama-server" in cmd and "wafer_pixel" in cmd:
                found[proc.info["pid"]] = {"pid": proc.info["pid"],
                                           "name": proc.info["name"] or "",
                                           "why": "llama-server",
                                           "cmd": cmd}

    for conn in psutil.net_connections(kind="tcp"):
        if conn.status != psutil.CONN_LISTEN or not conn.laddr:
            continue
        port = conn.laddr.port
        if port not in PORTS or not conn.pid or conn.pid in me:
            continue
        try:
            proc = psutil.Process(conn.pid)
            name = proc.name()
        except (psutil.Error, OSError):
            continue
        if name.lower() not in PORT_KILLABLE:
            continue
        if conn.pid not in found:
            found[conn.pid] = {"pid": conn.pid, "name": name,
                               "why": f"port {port} ({PORTS[port]})",
                               "cmd": _cmdline(proc)}
    return sorted(found.values(), key=lambda row: int(row["pid"]))


def kill_tree(pid: int) -> list[int]:
    killed: list[int] = []
    try:
        proc = psutil.Process(pid)
    except psutil.Error:
        return killed
    try:
        children = proc.children(recursive=True)
    except psutil.Error:
        children = []
    for child in reversed(children):
        try:
            child.kill()
            killed.append(child.pid)
        except psutil.Error:
            pass
    for action in (proc.terminate, proc.kill):
        try:
            action()
        except psutil.Error:
            pass
        try:
            proc.wait(timeout=5)
            killed.append(pid)
            break
        except psutil.TimeoutExpired:
            continue
        except psutil.Error:
            break
    return killed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    targets = find_targets()
    if args.dry_run:
        rows = targets
        report = {"dry_run": True, "targets": rows}
    else:
        killed: list[int] = []
        for row in targets:
            killed.extend(kill_tree(int(row["pid"])))
        time.sleep(1.5)
        left = find_targets()
        report = {"killed": sorted(set(killed)), "remaining": left}

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    else:
        for row in targets:
            print(f"{row['why']:<18} pid={row['pid']:<7} "
                  f"{row['name']:<18} {str(row['cmd'])[:70]}", flush=True)
        if not args.dry_run:
            print(f"killed {len(set(report['killed']))} process(es); "
                  f"remaining {len(report['remaining'])}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
