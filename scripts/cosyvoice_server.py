"""CosyVoice TTS server (D32 / TTS-03): runs in the CosyVoice venv.

Runs OUTSIDE the main wafer venv (its requirements pin numpy/torch/pydantic
versions that would pollute the main stack). Start it with the CosyVoice
venv interpreter (Windows paths, adjust to your machine):

    third_party/CosyVoice/.venv/Scripts/python.exe scripts/cosyvoice_server.py
        --model-dir <shared>/models/cosyvoice2-0.5b
        --prompt-wav models/tts/prompt_zh_female.wav
        --port 9880 --device cpu

Contract (consumed by engines/tts_cosyvoice.py CosyVoiceTts):
    GET  /health          -> 200 when the model is loaded
    POST /synthesize      {"text": "..."} -> audio/wav (24k s16le mono)

Voice: zero-shot against --prompt-wav (a ~5s reference clip of the target
voice; CosyVoice2-0.5B has no built-in spk2info on ModelScope, see D32).
The server is single-flight: one synthesis at a time (a lock), which is
fine for a one-robot home assistant.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import struct
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "third_party" / "CosyVoice"))
# CosyVoice's flow modules import `matcha` from the vendored submodule
# (not pip-installed here); put it on the path before importing CosyVoice.
sys.path.insert(0, str(ROOT / "third_party" / "CosyVoice" / "third_party" / "Matcha-TTS"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("cosyvoice-server")

SAMPLE_RATE = 24000


def install_offline_wetext() -> None:
    """Make CosyVoice's `wetext` frontend work without a ModelScope token.

    wetext.Normalizer(...) calls `snapshot_download("pengzhendong/wetext")`,
    which requires an authenticated ModelScope session on this machine
    ("Authentication token does not exist"). We keep the FSTs under
    models/wetext/ and point wetext at them instead (R.19, 2026-10-25).
    """
    local = ROOT / "models" / "wetext"
    if not (local / "zh" / "tn" / "tagger.fst").exists():
        logger.info("wetext: local FSTs missing at %s (unchanged)", local)
        return
    try:
        import wetext.wetext as _wetext
    except Exception as exc:  # noqa: BLE001 -- wetext optional
        logger.info("wetext not importable (%s); text frontend stays off", exc)
        return

    def _local_snapshot(*_args, **_kwargs) -> str:
        return str(local)

    _wetext.snapshot_download = _local_snapshot
    logger.info("wetext: using local FSTs at %s", local)


install_offline_wetext()


def tensor_to_wav_bytes(tensor) -> bytes:
    """torch tensor [samples] float -> wav bytes (24k s16le mono)."""
    import numpy as np

    audio = tensor.detach().cpu().numpy().reshape(-1).astype(np.float32)
    pcm = np.clip(audio, -1.0, 1.0)
    pcm16 = (pcm * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm16.tobytes())
    return buf.getvalue()


def tensor_to_pcm(tensor) -> bytes:
    """torch tensor [samples] float -> raw s16le mono PCM bytes (24k)."""
    import numpy as np

    audio = tensor.detach().cpu().numpy().reshape(-1).astype(np.float32)
    pcm16 = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
    return pcm16.tobytes()


CLAUSE_ENDS = "，。！？；、,.!?;"


def split_clauses(text: str, first_max: int = 10,
                  rest_max: int = 40) -> list[str]:
    """Split a reply into clauses for low first-audio latency.

    v1 streaming only starts the vocoder after the whole *requested* string has
    been tokenized, so first-byte grows with text length (measured 2026-10-18:
    3 chars 1.41s, 29 chars 3.01s). The first clause is kept short (fast first
    audio); the rest is merged into larger chunks to avoid per-call overhead
    (each call re-runs the text frontend / prefill).
    """
    raw: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in CLAUSE_ENDS:
            if buf.strip(CLAUSE_ENDS).strip():
                raw.append(buf.strip())
            buf = ""
    if buf.strip():
        raw.append(buf.strip())
    if not raw:
        return [text]

    out: list[str] = []
    first = raw[0]
    while len(first) > first_max:
        out.append(first[:first_max])
        first = first[first_max:]
    if first.strip():
        out.append(first)

    rest = "".join(raw[1:])
    while len(rest) > rest_max:
        out.append(rest[:rest_max])
        rest = rest[rest_max:]
    if rest.strip():
        out.append(rest)
    return [c for c in out if c.strip()]


class SynthesisServer:
    """Holds the loaded model; serves one synthesis at a time."""

    def __init__(self, model_dir: str, prompt_wav: str, prompt_text: str,
                 device: str, fp16: bool = False, spk_id: str = "",
                 zero_shot_spk_id: str = "") -> None:
        self._lock = threading.Lock()
        self._lock_timeout = 60.0
        self._spk_id = spk_id
        self._zs_id = zero_shot_spk_id
        if spk_id:  # CosyVoice v1 SFT: built-in speakers, no reference clip
            from cosyvoice.cli.cosyvoice import CosyVoice

            logger.info("loading CosyVoice v1 SFT (device=%s, fp16=%s, "
                        "spk=%s) ...", device, fp16, spk_id)
            self._model = CosyVoice(model_dir, load_jit=False, load_trt=False,
                                    fp16=fp16)
        else:
            from cosyvoice.cli.cosyvoice import CosyVoice2

            logger.info("loading CosyVoice2 from %s (device=%s, fp16=%s) ...",
                        model_dir, device, fp16)
            self._model = CosyVoice2(model_dir, load_jit=False, load_trt=False,
                                     fp16=fp16)
            if zero_shot_spk_id:
                # Cache the prompt embedding once: otherwise every clause
                # re-runs the speech tokenizer + campplus (+~0.8s each).
                self._model.add_zero_shot_spk(prompt_text, prompt_wav,
                                              zero_shot_spk_id)
                logger.info("cached zero-shot speaker: %s", zero_shot_spk_id)
        self._device = device
        # CosyVoice2 zero-shot takes a FILE PATH for prompt_wav, not a tensor
        self._prompt_wav_path = prompt_wav
        self._prompt_text = prompt_text
        if spk_id:
            logger.info("built-in speaker: %s", spk_id)
        else:
            logger.info("prompt voice: %s", prompt_wav)

    def _acquire(self) -> None:
        if not self._lock.acquire(timeout=self._lock_timeout):
            raise RuntimeError("tts server busy (another synthesis is running)")

    def synthesize(self, text: str, spk_id: str = "") -> bytes:
        speaker = spk_id or self._spk_id
        self._acquire()
        try:
            if speaker:
                stream = self._model.inference_sft(
                    text, speaker, stream=False)
            else:
                if not self._prompt_wav_path:
                    raise ValueError("no speaker: give spk_id or prompt_wav")
                stream = self._model.inference_zero_shot(
                    text, self._prompt_text, self._prompt_wav_path,
                    zero_shot_spk_id=self._zs_id,
                    stream=False,
                )
            chunks = [tensor_to_wav_bytes(out["tts_speech"]) for out in stream]
        finally:
            self._lock.release()
        # v1: single-chunk contract
        return chunks[0] if chunks else b""


    def synthesize_clause_wavs(self, text: str, spk_id: str = "",
                               text_frontend: bool = True):
        """Yield wavs for the caller (one per request, no clause splitting).

        History (2026-10-25): clause splitting was removed after measurement --
        CosyVoice has ~1.2-2.0s fixed cost per call, so a 3-char clause cost
        2.06s (RTF 3.7) and playback hit a 2-4s hole before the next clause.
        Single-chunk-per-sentence removes the stutter; first-audio cost is the
        whole sentence again (~2.3s for 17 chars), which is the model's floor.

        The legacy `/synthesize/stream` below still splits clauses for
        debugging; this path is what `/synthesize/frames` uses.
        """
        speaker = spk_id or self._spk_id
        if not speaker and not self._prompt_wav_path:
            raise ValueError("no speaker: give spk_id or prompt_wav")
        self._acquire()
        try:
            if speaker:
                stream = self._model.inference_sft(
                    text, speaker, stream=False, text_frontend=text_frontend)
            else:
                stream = self._model.inference_zero_shot(
                    text, self._prompt_text, self._prompt_wav_path,
                    zero_shot_spk_id=self._zs_id,
                    stream=False, text_frontend=text_frontend)
            chunks = [tensor_to_wav_bytes(out["tts_speech"]) for out in stream]
            if chunks:
                yield chunks[0]
        finally:
            self._lock.release()

    def synthesize_pcm(self, text: str, spk_id: str = "",
                       text_frontend: bool = True):
        """Yield raw s16le 24k PCM chunks as the model produces them.

        Clause-split + streaming keeps first audio low (non-streaming first
        packet was 2.3-6s in the 2026-10-18 measurements).
        """
        speaker = spk_id or self._spk_id
        if not speaker and not self._prompt_wav_path:
            raise ValueError("no speaker: give spk_id or prompt_wav")
        self._acquire()
        try:
            for clause in split_clauses(text):
                if speaker:
                    stream = self._model.inference_sft(
                        clause, speaker, stream=True,
                        text_frontend=text_frontend)
                else:
                    stream = self._model.inference_zero_shot(
                        clause, self._prompt_text, self._prompt_wav_path,
                        zero_shot_spk_id=self._zs_id,
                        stream=True, text_frontend=text_frontend)
                for out in stream:
                    yield tensor_to_pcm(out["tts_speech"])
        finally:
            self._lock.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CosyVoice TTS server")
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--prompt-wav", default="",
                        help="~5s reference clip (zero-shot only)")
    parser.add_argument("--prompt-text", default="这是一段参考音频的文本内容。",
                        help="transcript of the reference clip (zero-shot)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9880)
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    parser.add_argument("--fp16", action="store_true",
                        help="half precision weights (needs CUDA)")
    parser.add_argument("--spk-id", default="",
                        help="v1 SFT built-in speaker (e.g. 中文女); "
                             "omit for CosyVoice2 zero-shot")
    parser.add_argument("--zero-shot-spk-id", default="",
                        help="cache the prompt embedding under this id "
                             "(CosyVoice2 only)")
    args = parser.parse_args(argv)

    if not args.spk_id and not args.prompt_wav:
        parser.error("give --prompt-wav (zero-shot) or --spk-id (v1 SFT)")

    server_impl = SynthesisServer(args.model_dir, args.prompt_wav,
                                  args.prompt_text, args.device, args.fp16,
                                  args.spk_id, args.zero_shot_spk_id)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):  # quiet
            pass

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
            if self.path not in ("/synthesize", "/synthesize/stream",
                                 "/synthesize/frames"):
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                text = str(body.get("text", "")).strip()
                spk_id = str(body.get("spk_id", "") or "")
                text_frontend = bool(body.get("text_frontend", True))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.send_response(400)
                self.end_headers()
                return
            if not text:
                self.send_response(400)
                self.end_headers()
                return

            if self.path == "/synthesize/frames":
                # Framed WAV chunks: u32 BE length + full wav, repeated.
                # Each frame is independently decodable (D45 / R.17).
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    for wav in server_impl.synthesize_clause_wavs(
                            text, spk_id, text_frontend):
                        frame = struct.pack(">I", len(wav)) + wav
                        self.wfile.write(frame)
                        self.wfile.flush()
                except Exception:
                    logger.exception("framed synthesis failed")
                self.close_connection = True
                return

            if self.path == "/synthesize/stream":
                # Raw PCM, delimited by connection close (no length known).
                self.send_response(200)
                self.send_header(
                    "Content-Type", f"audio/L16;rate={SAMPLE_RATE};channels=1")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    for pcm in server_impl.synthesize_pcm(text, spk_id,
                                                          text_frontend):
                        self.wfile.write(pcm)
                        self.wfile.flush()
                except Exception:
                    logger.exception("stream synthesis failed")
                self.close_connection = True
                return

            try:
                wav = server_impl.synthesize(text, spk_id)
            except Exception:
                logger.exception("synthesis failed")
                self.send_response(500)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav)))
            self.end_headers()
            self.wfile.write(wav)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    logger.info("cosyvoice-server listening on http://%s:%d", args.host, args.port)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
