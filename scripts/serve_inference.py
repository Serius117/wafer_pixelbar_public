"""Start / stop / inspect the local inference trio (plan A, D40).

Launches:
  llm :8080  llama-server  Qwen3.5-9B Q4  (ctx 4096, --reasoning off)
             (skipped when llm.provider=openai_compat)
  asr :8001  llama-server  Qwen3-ASR 0.6B Q8 + mmproj (ctx 2048)
  tts :9880  gpt_sovits_server.py / dashscope_tts_server.py /
             minimax_tts_server.py (config-driven)

Config: config/inference.yaml + optional config/providers.local.yaml.
Logs: logs/infer_<name>.log. PID file: logs/infer_pids.json.

    .\\.venv\\Scripts\\python.exe scripts/serve_inference.py --start
    .\\.venv\\Scripts\\python.exe scripts/serve_inference.py --status
    .\\.venv\\Scripts\\python.exe scripts/serve_inference.py --stop
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CONFIG = ROOT / "config" / "inference.yaml"
PID_FILE = ROOT / "logs" / "infer_pids.json"
NAMES = ("llm", "asr", "tts")


def load_config(path: Path | None = None) -> dict:
    from engines.provider_config import load_inference
    from engines.provider_state import apply_state

    if path is None:
        return apply_state(load_inference())
    return load_inference(path=path)


def _tts_settings(c: dict) -> tuple[str, dict]:
    from engines.provider_config import tts_profile

    return tts_profile(c)


def _enabled(conf: dict, name: str) -> bool:
    if name != "llm":
        return True
    provider = str((conf.get("llm") or {}).get("provider") or "local")
    return provider != "openai_compat"


def _cmd(conf: dict, name: str) -> list[str]:
    server = str(ROOT / conf["llama_server"])
    cuda = conf.get("llama_server_cuda")
    if cuda and (ROOT / str(cuda)).exists():
        server = str(ROOT / str(cuda))
    if name == "llm":
        c = conf["llm"]
        return [server, "-m", str(ROOT / c["model"]), "-ngl",
                str(c["n_gpu_layers"]), "-c", str(c["ctx"]),
                "--host", c["host"], "--port", str(c["port"]),
                *[str(a) for a in c.get("extra_args", [])]]
    if name == "asr":
        c = conf["asr"]
        device = str(c.get("device", "cpu")).lower()
        ngl = 99 if device == "gpu" else 0
        return [server, "-m", str(ROOT / c["model"]), "--mmproj",
                str(ROOT / c["mmproj"]), "-ngl", str(ngl),
                "-c", str(c["ctx"]), "--host", c["host"],
                "--port", str(c["port"]), "--alias", c["alias"],
                *[str(a) for a in c.get("extra_args", [])]]
    provider, c = _tts_settings(conf)
    cmd = [str(ROOT / c["python"]), str(ROOT / c["server"]),
           "--host", c["host"], "--port", str(c["port"])]
    if provider == "aliyun_cosyvoice":
        cmd += ["--model", str(c.get("model", "cosyvoice-v3.5-flash"))]
        if c.get("voice_id"):
            cmd += ["--voice-id", str(c["voice_id"])]
        for key, flag in (
            ("region", "--region"),
            ("api_key_env", "--api-key-env"),
            ("workspace_id_env", "--workspace-id-env"),
            ("voice_id_env", "--voice-id-env"),
            ("workspace_id", "--workspace-id"),
            ("endpoint", "--endpoint"),
            ("sample_rate", "--sample-rate"),
            ("format", "--format"),
            ("language", "--language"),
            ("timeout", "--timeout"),
        ):
            if c.get(key) not in (None, ""):
                cmd += [flag, str(c[key])]
        cmd += [str(a) for a in c.get("extra_args", [])]
        return cmd
    if provider == "minimax":
        cmd += ["--model", str(c.get("model", "speech-2.8-hd"))]
        if c.get("voice_id"):
            cmd += ["--voice-id", str(c["voice_id"])]
        for key, flag in (
            ("endpoint", "--endpoint"),
            ("base_url", "--base-url"),
            ("api_key_env", "--api-key-env"),
            ("group_id_env", "--group-id-env"),
            ("voice_id_env", "--voice-id-env"),
            ("sample_rate", "--sample-rate"),
            ("format", "--format"),
            ("timeout", "--timeout"),
        ):
            if c.get(key) not in (None, ""):
                cmd += [flag, str(c[key])]
        cmd += [str(a) for a in c.get("extra_args", [])]
        return cmd
    if provider == "qwen_tts":
        cmd += ["--model", str(c.get("model", "qwen3-tts-flash"))]
        if c.get("voice"):
            cmd += ["--voice", str(c["voice"])]
        for key, flag in (
            ("endpoint", "--endpoint"),
            ("ws_url", "--ws-url"),
            ("api_key_env", "--api-key-env"),
            ("voice_env", "--voice-env"),
            ("sample_rate", "--sample-rate"),
            ("timeout", "--timeout"),
        ):
            if c.get(key) not in (None, ""):
                cmd += [flag, str(c[key])]
        cmd += [str(a) for a in c.get("extra_args", [])]
        return cmd
    if provider not in ("gpt_sovits", "cosyvoice"):
        raise SystemExit(f"unknown tts provider: {provider}")
    cmd += ["--model-dir", str(ROOT / c["model_dir"])]
    if c.get("spk_id"):
        cmd += ["--spk-id", str(c["spk_id"])]
    elif c.get("prompt_wav"):
        cmd += ["--prompt-wav", str(ROOT / c["prompt_wav"])]
        if c.get("prompt_text"):
            cmd += ["--prompt-text", str(c["prompt_text"])]
        if c.get("zero_shot_spk_id"):
            cmd += ["--zero-shot-spk-id", str(c["zero_shot_spk_id"])]
    else:
        raise SystemExit("tts config needs spk_id (v1) or prompt_wav (v2)")
    cmd += ["--device", c.get("device", "cuda"),
            *[str(a) for a in c.get("extra_args", [])]]
    if c.get("fp16"):
        cmd.append("--fp16")
    return cmd


def _health_url(conf: dict, name: str) -> str:
    c = conf[name]
    return f"http://{c['host']}:{c['port']}/health"


def _read_pids() -> dict:
    try:
        return json.loads(PID_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def status(conf: dict) -> int:
    pids = _read_pids()
    rows = []
    for name in NAMES:
        if not _enabled(conf, name):
            rows.append({"service": name,
                         "provider": str((conf.get("llm") or {}).get("provider")),
                         "healthy": True, "remote": True})
            continue
        url = _health_url(conf, name)
        try:
            ok = httpx.get(url, timeout=2.0).status_code == 200
        except Exception:
            ok = False
        pid = pids.get(name)
        alive = False
        if pid:
            try:
                out = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                    capture_output=True, text=True, timeout=10)
                alive = str(pid) in out.stdout
            except Exception:
                alive = False
        rows.append({"service": name, "port": conf[name]["port"],
                     "healthy": ok, "pid": pid, "alive": alive})
    print(json.dumps(rows, ensure_ascii=False, indent=2), flush=True)
    return 0 if all(r["healthy"] for r in rows) else 1


def _port_owner(port: int) -> int | None:
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True,
            text=True, timeout=15).stdout
    except Exception:  # noqa: BLE001
        return None
    needle = f":{port} "
    for line in out.splitlines():
        if needle in line and "LISTENING" in line:
            parts = line.split()
            if parts and parts[-1].isdigit():
                return int(parts[-1])
    return None


def _kill(pid: int, why: str) -> None:
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=20)
        print(f"stopped {why} (pid {pid})", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"kill {why} (pid {pid}) failed: {e}", flush=True)


def stop(conf: dict | None = None, only: list[str] | None = None) -> int:
    """Stop the whole trio, or just the named services when ``only`` is set."""
    selected = set(only) if only else set(NAMES)
    pids = _read_pids()
    killed = set()
    for name, pid in list(pids.items()):
        if name not in selected:
            continue
        _kill(pid, name)
        killed.add(pid)
        pids.pop(name, None)
    if pids:
        PID_FILE.write_text(json.dumps(pids, indent=2), encoding="utf-8")
    else:
        PID_FILE.unlink(missing_ok=True)
    if conf:
        for name in selected:
            owner = _port_owner(int(conf[name]["port"]))
            if owner and owner not in killed:
                _kill(owner, f"{name} orphan")
    return 0


def _child_env() -> dict[str, str]:
    """UTF-8 stdio for children: otherwise CJK logs turn into '?' (cp936)."""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def start(conf: dict, only: list[str], timeout: float) -> int:
    (ROOT / "logs").mkdir(exist_ok=True)
    skipped = [n for n in only if not _enabled(conf, n)]
    only = [n for n in only if _enabled(conf, n)]
    for name in skipped:
        print(f"skipping {name}: provider="
              f"{(conf.get('llm') or {}).get('provider')} (remote)", flush=True)
    pids = _read_pids()
    procs: dict[str, subprocess.Popen] = {}
    logs = {}
    try:
        for name in only:
            log = (ROOT / "logs" / f"infer_{name}.log").open(
                "w", encoding="utf-8")
            logs[name] = log
            proc = subprocess.Popen(_cmd(conf, name), cwd=ROOT,
                                    stdout=log, stderr=subprocess.STDOUT,
                                    env=_child_env())
            procs[name] = proc
            pids[name] = proc.pid
            print(f"starting {name} (pid {proc.pid}) ...", flush=True)
            PID_FILE.write_text(json.dumps(pids, indent=2), encoding="utf-8")

        for name in only:
            proc = procs[name]
            url = _health_url(conf, name)
            began = time.monotonic()
            while time.monotonic() - began < timeout:
                if proc.poll() is not None:
                    print(f"{name} exited ({proc.returncode}); "
                          f"see logs/infer_{name}.log", flush=True)
                    return 2
                try:
                    if httpx.get(url, timeout=1.0).status_code == 200:
                        print(f"{name} healthy after "
                              f"{time.monotonic() - began:.1f}s", flush=True)
                        break
                except Exception:
                    time.sleep(1.0)
            else:
                print(f"{name} never healthy on {url}", flush=True)
                return 3
        print(json.dumps({"pids": pids}, ensure_ascii=False), flush=True)
        return 0
    except Exception:
        stop()
        raise
    finally:
        for log in logs.values():
            log.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--start", action="store_true")
    group.add_argument("--stop", action="store_true")
    group.add_argument("--status", action="store_true")
    ap.add_argument("--config", default=None,
                    help="config path (default: config/inference.yaml + "
                         "providers.local.yaml overlay; smoke passes an "
                         "explicit config so CI ignores the local overlay)")
    ap.add_argument("--only", default="llm,asr,tts",
                    help="comma list (llm,asr,tts)")
    ap.add_argument("--timeout", type=float, default=180.0)
    args = ap.parse_args(argv)

    conf = load_config(Path(args.config) if args.config else None)
    only = [n.strip() for n in args.only.split(",") if n.strip()]
    bad = [n for n in only if n not in NAMES]
    if bad:
        ap.error(f"unknown service(s): {bad}")
    if args.stop:
        return stop(conf, only)
    if args.status:
        return status(conf)
    return start(conf, only, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
