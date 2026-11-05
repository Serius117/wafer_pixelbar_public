"""Voiceprint engine: CAM++ embedding + cosine scoring (T5.1).

Real engine (3dspeaker CAM++ via campplus ONNX, 28MB, CPU) with lazy heavy
imports so CI (torch-free) can import this module and test the math, the
enrollment store, and the scoring logic. The model itself is only touched
on the home machine.

Embeddings are plain float lists end to end:
  - core stays numpy-free (layering rule, design doc §2.2)
  - on-disk format is JSON with plain floats (inspectable, portable)

Enrollment format (data/speakers/<speaker_id>.json):
  {"speaker_id": "master", "dim": 192, "embedding": [...]}
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
DEFAULT_DIM = 192  # CAM++ embedding dimension


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two equal-length vectors (pure python)."""
    if len(a) != len(b):
        raise ValueError(f"dimension mismatch: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def normalize(v: Sequence[float]) -> list[float]:
    """Return unit-length copy of v (zero vector passes through as zeros)."""
    norm = math.sqrt(sum(x * x for x in v))
    if norm == 0.0:
        return list(v)
    return [x / norm for x in v]


def average_embedding(embeddings: Sequence[Sequence[float]]) -> list[float]:
    """Average multiple sample embeddings into one unit vector (enrollment)."""
    if not embeddings:
        raise ValueError("no embeddings to average")
    dim = len(embeddings[0])
    if any(len(e) != dim for e in embeddings):
        raise ValueError("inconsistent embedding dimensions")
    mean = [sum(e[i] for e in embeddings) / len(embeddings) for i in range(dim)]
    return normalize(mean)


class EnrollmentStore:
    """Speaker embedding persistence: one JSON file per speaker."""

    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, speaker_id: str) -> Path:
        return self._dir / f"{speaker_id}.json"

    def load(self) -> dict[str, list[float]]:
        """Load all enrolled embeddings: {speaker_id: embedding}."""
        result: dict[str, list[float]] = {}
        for path in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                result[str(data["speaker_id"])] = [float(x) for x in data["embedding"]]
            except (json.JSONDecodeError, KeyError, OSError) as e:
                logger.warning("skip bad enrollment file %s: %s", path, e)
        return result

    def save(self, speaker_id: str, embedding: Sequence[float]) -> Path:
        """Persist one speaker's embedding, atomically enough for home use."""
        path = self.path_for(speaker_id)
        payload = {
            "speaker_id": speaker_id,
            "dim": len(embedding),
            "embedding": [float(x) for x in embedding],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def delete(self, speaker_id: str) -> None:
        self.path_for(speaker_id).unlink(missing_ok=True)


class EmbeddingVerifier:
    """SpeakerVerifier scoring against enrolled embeddings (pure python).

    Subclasses provide the actual `_embed_impl`; this class owns the
    enrollment cache and cosine scoring (shared by real and sim engines).
    """

    def __init__(self, store: EnrollmentStore) -> None:
        self._store = store
        self._enrolled: dict[str, list[float]] | None = None

    @property
    def store(self) -> EnrollmentStore:
        return self._store

    def _embed_impl(self, pcm: bytes) -> Sequence[float]:
        raise NotImplementedError

    def embed(self, pcm: bytes) -> list[float]:
        return [float(x) for x in self._embed_impl(pcm)]

    def scores(self, pcm: bytes) -> dict[str, float]:
        """Cosine similarity per enrolled speaker id (SpeakerVerifier protocol)."""
        if self._enrolled is None:
            self._enrolled = self._store.load()
        emb = self.embed(pcm)
        return {sid: cosine_similarity(emb, ref) for sid, ref in self._enrolled.items()}

    def refresh(self) -> None:
        """Drop the enrollment cache (after enrolling a new speaker)."""
        self._enrolled = None


class CampplusVerifier(EmbeddingVerifier):
    """Real SpeakerVerifier backed by 3dspeaker CAM++ (campplus ONNX).

    Heavy deps (numpy, campplus) are imported lazily inside `_embed_impl`
    so importing this module never pulls torch/onnxruntime in CI.

    `model_factory` is injectable for tests (fake CAM++ object with
    `extract_emb(wav_ndarray)`).
    """

    def __init__(
        self,
        store: EnrollmentStore,
        model_path: Path,
        model_factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(store)
        self._model_path = Path(model_path)
        self._model_factory = model_factory
        self._model: Any | None = None

    @property
    def model_path(self) -> Path:
        return self._model_path

    def _ensure_model(self) -> Any:
        if self._model is None:
            if self._model_factory is not None:
                self._model = self._model_factory()
            else:
                if not self._model_path.exists():
                    raise FileNotFoundError(
                        f"CAM++ model not found: {self._model_path} "
                        "(see docs/DEVLOG.md for the hf-mirror download)"
                    )
                from campplus.onnx import CAMPPlus  # lazy: home machine only

                self._model = CAMPPlus(str(self._model_path))
            logger.info("CAM++ model loaded: %s", self._model_path)
        return self._model

    def _embed_impl(self, pcm: bytes) -> Sequence[float]:
        """16 kHz mono s16 PCM bytes -> CAM++ embedding (length 192)."""
        import numpy as np  # lazy: home machine only

        model = self._ensure_model()
        wav = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        emb = model.extract_emb(wav)
        arr = np.asarray(emb, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            raise ValueError("empty embedding from CAM++")
        return arr.tolist()


class SeededEmbedder:
    """Deterministic fake voice: one fixed unit vector per seed (CI/demo).

    Makes `enroll --sim-seed` and calibration reproducible without any model.
    """

    def __init__(self, seed: int, dim: int = DEFAULT_DIM) -> None:
        self._seed = seed
        self._dim = dim

    @property
    def seed(self) -> int:
        return self._seed

    def embedding(self) -> list[float]:
        import random

        rng = random.Random(self._seed)
        return normalize([rng.gauss(0.0, 1.0) for _ in range(self._dim)])


class SeededVerifier(EmbeddingVerifier):
    """Verifier whose embed() ignores audio and returns a seeded vector.

    Register speakers with distinct seeds; a caller "speaking as" seed N
    scores ~1.0 against speaker N and ~0 against the rest. Used by CI to
    exercise the full gate chain without models.
    """

    def __init__(self, store: EnrollmentStore, seed: int) -> None:
        super().__init__(store)
        self._embedder = SeededEmbedder(seed)

    def _embed_impl(self, pcm: bytes) -> Sequence[float]:
        return self._embedder.embedding()


class SherpaCampplusVerifier(EmbeddingVerifier):
    """Real SpeakerVerifier backed by sherpa-onnx SpeakerEmbeddingExtractor.

    Zero new dependencies: sherpa-onnx is already installed (KWS), and the
    CAM++ ONNX model lives on the shared disk (E.3). Input is 16k mono s16
    PCM bytes; output is a normalized 192-dim embedding.
    """

    def __init__(self, store: EnrollmentStore, model_path: Path) -> None:
        super().__init__(store)
        self._model_path = Path(model_path)
        self._extractor = None

    def _ensure_extractor(self):
        if self._extractor is None:
            import sherpa_onnx  # lazy: engine dependency stays out of core

            if not self._model_path.exists():
                raise FileNotFoundError(f"CAM++ model not found: {self._model_path}")
            cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(self._model_path), num_threads=1, provider="cpu")
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
            logger.info("sherpa CAM++ extractor loaded (dim=%d)", self._extractor.dim)
        return self._extractor

    def _embed_impl(self, pcm: bytes) -> Sequence[float]:
        import numpy as np

        extractor = self._ensure_extractor()
        samples = np.frombuffer(pcm, dtype=np.int16)
        if samples.size < 1600:  # < 0.1s: not enough voice
            return [0.0] * 192
        stream = extractor.create_stream()
        stream.accept_waveform(16000, samples)
        stream.input_finished()
        if not extractor.is_ready(stream):
            return [0.0] * 192
        vec = extractor.compute(stream)
        norm = np.linalg.norm(vec)
        if norm == 0:
            return [0.0] * 192
        return (vec / norm).tolist()
