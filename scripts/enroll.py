"""Speaker enrollment CLI (T5.3): record samples -> embedding -> speakers.yaml.

Three input modes (first that applies wins):
  --from-dir DIR   read *.wav (16 kHz mono s16) files as samples (no mic needed)
  --sim-seed N     deterministic fake voice, no model no mic (CI / demo)
  (default)        record --samples clips from the microphone via sounddevice

Examples:
  # real, at home (model + mic):
  .venv\\Scripts\\python.exe scripts\\enroll.py --speaker-id master --name 主人 \
      --role admin --priority 1 --samples 3

  # from existing wavs:
  .venv\\Scripts\\python.exe scripts\\enroll.py --speaker-id wife --name 老婆 \
      --role member --priority 2 --from-dir temp_script\\wife_wavs

  # sim (CI):
  .venv\\Scripts\\python.exe scripts\\enroll.py --speaker-id master --name 主人 \
      --role admin --priority 1 --sim-seed 7
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml

from core.gate.profiles import GateSetup, load_gate_setup
from engines.voiceprint import (
    DEFAULT_DIM,
    SAMPLE_RATE,
    CampplusVerifier,
    EnrollmentStore,
    SeededEmbedder,
    average_embedding,
)

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = Path("config/speakers.yaml")


def read_wav_pcm(path: Path) -> bytes:
    """Read a 16 kHz mono 16-bit wav into raw PCM bytes (validated)."""
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != SAMPLE_RATE:
            raise ValueError(f"{path}: sample rate {w.getframerate()} != {SAMPLE_RATE}")
        if w.getnchannels() != 1:
            raise ValueError(f"{path}: expected mono, got {w.getnchannels()} channels")
        if w.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16-bit, got {w.getsampwidth()*8}-bit")
        return w.readframes(w.getnframes())


def pick_mic_device() -> int | None:
    """Prefer the PixelBar mic on WASAPI (what the browser uses).

    The MME alias of some USB mics records pure silence (measured at home:
    -63 dBFS while the Windows meter moved), which silently poisoned the
    enrollment embedding.
    """
    import sounddevice as sd

    hostapis = sd.query_hostapis()
    devices = sd.query_devices()
    wasapi = next((i for i, api in enumerate(hostapis)
                   if "WASAPI" in api["name"]), None)
    candidates = [
        (idx, dev) for idx, dev in enumerate(devices)
        if dev["max_input_channels"] > 0
        and (wasapi is None or dev["hostapi"] == wasapi)
        and ("PixelBar" in dev["name"] or "Halo" in dev["name"])
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda pair: abs(pair[1]["default_samplerate"] - 48000))
    return int(candidates[0][0])


def select_channel(audio):
    """Pick the input channel with the most energy (silent channels are common).

    WASAPI exposes the PixelBar microphone as a 2-channel endpoint while MME
    exposes it as mono. Recording channel 0 blindly can produce silence even
    though the other channel has the microphone.
    """
    import numpy as np

    data = np.asarray(audio)
    if data.ndim == 1 or data.shape[1] == 1:
        return data.reshape(-1)
    energy = np.mean(data.astype(np.float64) ** 2, axis=0)
    return data[:, int(np.argmax(energy))]


def record_sample(seconds: float, sample_rate: int = SAMPLE_RATE,
                  device: int | None = None) -> bytes:
    """Record one clip from the microphone (WASAPI preferred) -> 16k s16 PCM."""
    import numpy as np
    import sounddevice as sd  # lazy: only needed for live enrollment

    if device is None:
        device = pick_mic_device()
    rate = sample_rate
    channels = 1
    if device is not None:
        info = sd.query_devices(device)
        rate = int(info["default_samplerate"])
        channels = max(1, int(info["max_input_channels"]))
    audio = sd.rec(int(seconds * rate), samplerate=rate, channels=channels,
                   dtype="int16", device=device)
    sd.wait()
    pcm = select_channel(audio)
    if rate == sample_rate:
        return pcm.astype(np.int16).tobytes()
    n_out = int(round(pcm.size * sample_rate / rate))
    x_out = np.linspace(0, pcm.size - 1, n_out)
    return np.interp(x_out, np.arange(pcm.size), pcm).astype(np.int16).tobytes()


def build_verifier(setup: GateSetup, use_sherpa: bool = False):
    """Real CAM++ verifier when the model is configured and present.

    `use_sherpa` selects SherpaCampplusVerifier -- the same implementation the
    OLV gate uses at runtime (sherpa-onnx, no extra deps). The default keeps
    the legacy FunASR `campplus` package path.
    """
    if setup.voiceprint_model is None:
        raise SystemExit(
            "no voiceprint.model in speakers.yaml -- use --sim-seed for CI, "
            "or fill in the model path (see docs/DEVLOG.md T5.1)"
        )
    store = EnrollmentStore(setup.embedding_dir)
    if use_sherpa:
        from engines.voiceprint import SherpaCampplusVerifier

        return SherpaCampplusVerifier(store, setup.voiceprint_model)
    return CampplusVerifier(store, setup.voiceprint_model)


def enroll_samples(
    embed_fn,
    samples: list[bytes],
) -> list[float]:
    """Embed each sample and average into one unit vector."""
    embeddings = [embed_fn(pcm) for pcm in samples]
    if not embeddings:
        raise ValueError("no samples to enroll")
    return average_embedding(embeddings)


def update_speakers_yaml(
    config_path: Path,
    speaker_id: str,
    name: str,
    role: str,
    priority: int,
    embedding_rel: str,
) -> None:
    """Insert/update one speaker entry, keeping the rest of the file intact."""
    config_path = Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    raw: dict = {}
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    speakers = raw.setdefault("speakers", {})
    speakers[speaker_id] = {
        "name": name,
        "role": role,
        "priority": priority,
        "embedding": embedding_rel,
    }
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enroll a speaker for the voiceprint gate")
    parser.add_argument("--speaker-id", required=True, help="unique id, e.g. master")
    parser.add_argument("--name", required=True, help="display name, e.g. 主人")
    parser.add_argument("--role", choices=["admin", "member"], default="member")
    parser.add_argument("--priority", type=int, default=99,
                        help="lower wins arbitration (master=1, wife=2, elder=3)")
    parser.add_argument("--samples", type=int, default=3, help="clips to record (mic mode)")
    parser.add_argument("--seconds", type=float, default=5.0, help="seconds per clip (mic mode)")
    parser.add_argument("--from-dir", type=Path, default=None,
                        help="read samples from wav files in this dir instead of mic")
    parser.add_argument("--sherpa", action="store_true",
                        help="enroll with sherpa-onnx CAM++ (same as the OLV gate)")
    parser.add_argument("--sim-seed", type=int, default=None,
                        help="deterministic fake voice (no model/mic needed)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--overwrite", action="store_true",
                        help="re-enroll even if the speaker already has an embedding")
    args = parser.parse_args(argv)

    setup = load_gate_setup(args.config)

    # Collect sample PCM bytes per mode
    if args.sim_seed is not None:
        samples: list[bytes] = [b"sim"] * 3
    elif args.from_dir is not None:
        wav_files = sorted(args.from_dir.glob("*.wav"))
        if not wav_files:
            raise SystemExit(f"no .wav files in {args.from_dir}")
        samples = [read_wav_pcm(p) for p in wav_files]
        print(f"已从 {args.from_dir} 读取 {len(samples)} 个 wav 文件")
    else:
        import numpy as np

        device = pick_mic_device()
        print(f"准备录制 {args.samples} 段，每段 {args.seconds:.0f} 秒（设备：{device}）。")
        print("现在可以开始说自然中文了；每段说完会自动结束，不用按键。")
        samples = []
        for index in range(args.samples):
            print(f"  第 {index + 1}/{args.samples} 段：现在可以开始说话了……",
                  flush=True)
            pcm = record_sample(args.seconds, device=device)
            data = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
            rms = float(np.sqrt(np.mean(data ** 2))) / 32768.0 if data.size else 0.0
            level = 20 * np.log10(rms + 1e-12)
            print(f"    音量：{level:.1f} dBFS", flush=True)
            if rms < 0.003:
                raise SystemExit(
                    "这一段几乎静音（rms < -50 dBFS）：麦克风没有收到声音，"
                    "拒绝写入坏声纹。请检查输入设备/音量后重试")
            samples.append(pcm)

    # Embed + average
    if args.sim_seed is not None:
        embedding = SeededEmbedder(args.sim_seed).embedding()
        dim = len(embedding)
    else:
        verifier = build_verifier(setup, use_sherpa=args.sherpa)
        embedding = enroll_samples(verifier.embed, samples)
        dim = len(embedding)

    if dim == 0:
        raise SystemExit("empty embedding")
    print(f"embedding dim = {dim} (expected {DEFAULT_DIM} for CAM++)")

    # Persist embedding JSON
    store = EnrollmentStore(setup.embedding_dir)
    existing = store.load()
    if args.speaker_id in existing and not args.overwrite:
        raise SystemExit(
            f"speaker '{args.speaker_id}' 已注册过；要重新注册请加 --overwrite"
        )
    emb_path = store.save(args.speaker_id, embedding)
    print(f"声纹已保存：{emb_path}")

    # Update speakers.yaml
    emb_rel = emb_path.as_posix()
    update_speakers_yaml(args.config, args.speaker_id, args.name,
                         args.role, args.priority, emb_rel)
    print(f"speakers.yaml 已更新：{args.config}")

    # Report resulting setup
    final = load_gate_setup(args.config)
    profile = final.profiles.get(args.speaker_id)
    assert profile is not None
    print(json.dumps({
        "speaker_id": profile.speaker_id,
        "name": profile.name,
        "role": profile.role,
        "priority": profile.priority,
        "enrolled": sorted(final.profiles.keys()),
    }, ensure_ascii=False, indent=2))
    print("注册完成：现在可以重启服务栈，开始做声纹/门禁测试。")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
