"""GPT-SoVITS v2Pro TTS server with the wafer HTTP contract.

Runs in the GPT-SoVITS venv (Python 3.12 / torch 2.7.1 cu128). Exposes the
same endpoints as scripts/cosyvoice_server.py so engines/tts_cosyvoice.py and
OLV's wafer_http_tts work unchanged:

    GET  /health            -> 200 when the model is loaded
    POST /synthesize        -> audio/wav (32 kHz s16le mono)
    POST /synthesize/frames -> u32 BE length + wav, one complete frame

The reference clip/prompt text are fixed at startup (same as CosyVoice).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import struct
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
GPT_ROOT = ROOT / "third_party" / "GPT-SoVITS"
sys.path.insert(0, str(GPT_ROOT))
os.chdir(GPT_ROOT)

from tools.portable_runtime import activate  # noqa: E402

activate(change_cwd=True)

from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config  # noqa: E402


def wav_bytes(sr: int, audio: np.ndarray) -> bytes:
    pcm = np.asarray(audio)
    if pcm.dtype != np.int16:
        pcm = (np.clip(pcm.astype(np.float32), -1, 1) * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class GptSoVitsServer:
    def __init__(self, model_dir: str, prompt_wav: str, prompt_text: str,
                 device: str, fp16: bool) -> None:
        def _abs(path: str) -> Path:
            p = Path(path)
            return p if p.is_absolute() else (ROOT / p)

        model_root = _abs(model_dir).resolve()
        config = {
            "custom": {
                "bert_base_path": str(model_root / "chinese-roberta-wwm-ext-large"),
                "cnhuhbert_base_path": str(model_root / "chinese-hubert-base"),
                "device": device,
                "is_half": bool(fp16),
                "t2s_weights_path": str(model_root / "s1v3.ckpt"),
                "vits_weights_path": str(model_root / "v2Pro" / "s2Gv2Pro.pth"),
                "version": "v2Pro",
            }
        }
        self._config = TTS_Config(config)
        self._tts = TTS(self._config)
        self._prompt_wav = str(_abs(prompt_wav).resolve())
        self._prompt_text = prompt_text
        self._lock = threading.Lock()
        print(json.dumps({
            "engine": "gpt-sovits-v2pro",
            "version": self._config.version,
            "prompt_wav": self._prompt_wav,
            "device": device,
            "fp16": bool(fp16),
        }, ensure_ascii=False), flush=True)

    def synthesize(self, text: str) -> bytes:
        text = (text or "").strip()
        if not text:
            return b""
        with self._lock:
            req = {
                "text": text,
                "text_lang": "zh",
                "ref_audio_path": self._prompt_wav,
                "prompt_text": self._prompt_text,
                "prompt_lang": "zh",
                "text_split_method": "cut5",
                "batch_size": 1,
                "parallel_infer": True,
                "streaming_mode": False,
                "media_type": "wav",
            }
            chunks: list[np.ndarray] = []
            sample_rate = 32000
            for out_sr, audio in self._tts.run(req):
                sample_rate = int(out_sr)
                chunks.append(np.asarray(audio))
            if not chunks:
                return b""
            return wav_bytes(sample_rate, np.concatenate(chunks))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--prompt-wav", required=True)
    ap.add_argument("--prompt-text", default="")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9880)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--fp16", action="store_true")
    # accepted for serve_inference compatibility (unused)
    ap.add_argument("--spk-id", default="")
    ap.add_argument("--zero-shot-spk-id", default="")
    args = ap.parse_args(argv)

    server_impl = GptSoVitsServer(args.model_dir, args.prompt_wav,
                                  args.prompt_text, args.device, args.fp16)
    # Warm the reference cache / CUDA kernels so the first real turn does not
    # pay the lazy-load cost (observed ~3s on the 4070S).
    try:
        server_impl.synthesize("你好。")
        print("gpt-sovits warmup done", flush=True)
    except Exception as exc:  # noqa: BLE001 -- warmup must not block startup
        print(f"gpt-sovits warmup failed: {exc}", flush=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            pass

        def _text(self) -> str | None:
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self.send_response(400)
                self.end_headers()
                return None
            text = str(body.get("text", "")).strip()
            if not text:
                self.send_response(400)
                self.end_headers()
                return None
            return text

        def do_GET(self):  # noqa: N802
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):  # noqa: N802
            if self.path not in ("/synthesize", "/synthesize/frames"):
                self.send_response(404)
                self.end_headers()
                return
            text = self._text()
            if text is None:
                return
            try:
                wav = server_impl.synthesize(text)
            except Exception as exc:  # noqa: BLE001
                self.send_response(500)
                self.end_headers()
                print(f"synth error: {exc}", file=sys.stderr, flush=True)
                return
            if not wav:
                self.send_response(500)
                self.end_headers()
                return
            if self.path == "/synthesize/frames":
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(struct.pack(">I", len(wav)) + wav)
                self.close_connection = True
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav)))
            self.end_headers()
            self.wfile.write(wav)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"gpt-sovits-server listening on http://{args.host}:{args.port}",
          flush=True)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
