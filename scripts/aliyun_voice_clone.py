"""Create / inspect a DashScope CosyVoice cloned voice from a local WAV.

Official API (Beijing):
  POST https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/customization

The reference audio is sent as a Base64 Data URI, so the local prompt file
never needs to be hosted publicly.  The API key is read from the environment
(or repo-root .env) and is never printed.

Examples:
  python scripts/aliyun_voice_clone.py \
      --audio data/prompts/xiaobing_ref_20261028.wav --prefix xiaobing --wait
  python scripts/aliyun_voice_clone.py --voice-id cosyvoice-v3.5-flash-xiaobing-xxxxxx
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import dashscope_common as dc  # noqa: E402


def wav_duration(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
            return frames / float(rate) if rate else None
    except (wave.Error, OSError):
        return None


def audio_data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "audio/wav"
    if path.suffix.lower() == ".wav":
        mime = "audio/wav"  # Linux mimetypes says audio/x-wav; API wants wav
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def create_voice(*, api_key: str, workspace_id: str, audio: str,
                 prefix: str, target_model: str = "cosyvoice-v3.5-flash",
                 region: str = dc.DEFAULT_REGION, endpoint: str = "",
                 language: str = "zh", enable_preprocess: bool = False,
                 volume_normalization: bool = False,
                 max_prompt_audio_length: float = 10.0,
                 timeout: float = 120.0) -> dict:
    if (not prefix.isascii() or not prefix.isalnum()
            or not 1 <= len(prefix) <= 10):
        raise RuntimeError("prefix must be ASCII alphanumeric and <= 10 chars")
    if not 3.0 <= max_prompt_audio_length <= 30.0:
        raise RuntimeError("max_prompt_audio_length must be in [3.0, 30.0]")
    payload = {
        "model": "voice-enrollment",
        "input": {
            "action": "create_voice",
            "target_model": target_model,
            "prefix": prefix,
            "audio": audio,
            "language_hints": [language],
            "enable_preprocess": bool(enable_preprocess),
            "enable_volume_normalization": (
                "true" if volume_normalization else "false"),
            "max_prompt_audio_length": float(max_prompt_audio_length),
        },
    }
    url = endpoint or dc.clone_endpoint(workspace_id, region)
    response = dc.post_json(url, payload, api_key, timeout=timeout)
    output = response.get("output") or {}
    if not output.get("voice_id"):
        raise RuntimeError(f"voice_id missing in response: {response}")
    return response


def query_voice(*, api_key: str, workspace_id: str, voice_id: str,
                region: str = dc.DEFAULT_REGION, endpoint: str = "",
                timeout: float = 60.0) -> dict:
    payload = {
        "model": "voice-enrollment",
        "input": {"action": "query_voice", "voice_id": voice_id},
    }
    url = endpoint or dc.clone_endpoint(workspace_id, region)
    return dc.post_json(url, payload, api_key, timeout=timeout)


def wait_voice(*, api_key: str, workspace_id: str, voice_id: str,
               region: str = dc.DEFAULT_REGION, endpoint: str = "",
               timeout_s: float = 180.0, interval_s: float = 5.0) -> str:
    deadline = time.monotonic() + timeout_s
    while True:
        response = query_voice(api_key=api_key, workspace_id=workspace_id,
                               voice_id=voice_id, region=region,
                               endpoint=endpoint)
        status = str((response.get("output") or {}).get("status", ""))
        print(json.dumps({"voice_id": voice_id, "status": status},
                         ensure_ascii=False), flush=True)
        if status == "OK":
            return status
        if status == "UNDEPLOYED":
            raise RuntimeError("voice review failed (status=UNDEPLOYED)")
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"voice still {status or 'UNKNOWN'} after {timeout_s:.0f}s")
        time.sleep(interval_s)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", default="",
                    help="reference wav/mp3/m4a (default: prompt path)")
    ap.add_argument("--voice-id", default="",
                    help="only query this existing voice")
    ap.add_argument("--prefix", default="xiaobing")
    ap.add_argument("--target-model", default="cosyvoice-v3.5-flash")
    ap.add_argument("--workspace-id", default="")
    ap.add_argument("--workspace-id-env", default=dc.WORKSPACE_ID_ENV)
    ap.add_argument("--api-key-env", default=dc.API_KEY_ENV)
    ap.add_argument("--region", default=dc.DEFAULT_REGION)
    ap.add_argument("--endpoint", default="",
                    help="full customization URL override (tests)")
    ap.add_argument("--language", default="zh")
    ap.add_argument("--enable-preprocess", action="store_true",
                    help="denoise/enhance; keep off for the clean prompt")
    ap.add_argument("--volume-normalization", action="store_true")
    ap.add_argument("--max-prompt-audio-length", type=float, default=10.0)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--wait", action="store_true",
                    help="poll query_voice until status=OK")
    ap.add_argument("--wait-timeout", type=float, default=180.0)
    args = ap.parse_args(argv)

    dc.load_env_file()
    api_key = os.environ.get(args.api_key_env, "").strip()
    workspace = (args.workspace_id
                 or os.environ.get(args.workspace_id_env, "")).strip()
    if not api_key:
        ap.error(f"missing {args.api_key_env} (environment or .env)")
    if not workspace and not args.endpoint:
        ap.error(f"missing {args.workspace_id_env} (environment or .env)")

    try:
        if args.voice_id:
            response = query_voice(
                api_key=api_key, workspace_id=workspace,
                voice_id=args.voice_id, region=args.region,
                endpoint=args.endpoint)
            print(json.dumps(response, ensure_ascii=False, indent=2))
            return 0

        audio_path = Path(args.audio or
                          (ROOT / "data" / "prompts"
                           / "xiaobing_ref_20261028.wav"))
        if not audio_path.is_absolute():
            audio_path = ROOT / audio_path
        if not audio_path.exists():
            ap.error(f"reference audio not found: {audio_path}")
        duration = wav_duration(audio_path)
        if duration is not None and duration < 3.0:
            ap.error(f"reference audio too short: {duration:.2f}s (need >=3s)")
        print(json.dumps({
            "audio": str(audio_path),
            "duration_s": round(duration, 3) if duration else None,
            "target_model": args.target_model,
            "prefix": args.prefix,
        }, ensure_ascii=False), flush=True)

        response = create_voice(
            api_key=api_key, workspace_id=workspace,
            audio=audio_data_uri(audio_path), prefix=args.prefix,
            target_model=args.target_model, region=args.region,
            endpoint=args.endpoint, language=args.language,
            enable_preprocess=args.enable_preprocess,
            volume_normalization=args.volume_normalization,
            max_prompt_audio_length=args.max_prompt_audio_length,
            timeout=args.timeout)
        voice_id = response["output"]["voice_id"]
        print(json.dumps({
            "voice_id": voice_id,
            "request_id": response.get("request_id"),
            "next": "put this in .env as DASHSCOPE_VOICE_ID",
        }, ensure_ascii=False, indent=2), flush=True)
        if args.wait:
            wait_voice(api_key=api_key, workspace_id=workspace,
                       voice_id=voice_id, region=args.region,
                       endpoint=args.endpoint, timeout_s=args.wait_timeout)
        return 0
    except RuntimeError as exc:
        print(f"voice clone failed: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
