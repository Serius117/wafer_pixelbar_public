"""M5 tests: voiceprint engine (math, store, verifier, profiles loader)."""

import math

import pytest

from core.gate.policy import GateConfig, SpeakerProfile
from core.gate.profiles import GateSetup, load_gate_setup
from engines.voiceprint import (
    CampplusVerifier,
    EnrollmentStore,
    SeededEmbedder,
    SeededVerifier,
    average_embedding,
    cosine_similarity,
    normalize,
)


class TestCosineMath:
    def test_identical_vectors(self) -> None:
        v = [1.0, 2.0, 3.0]
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal(self) -> None:
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite(self) -> None:
        assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_zero_vector_returns_zero(self) -> None:
        assert cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0

    def test_dimension_mismatch_raises(self) -> None:
        with pytest.raises(ValueError):
            cosine_similarity([1.0], [1.0, 2.0])

    def test_normalize(self) -> None:
        v = normalize([3.0, 4.0])
        assert math.sqrt(sum(x * x for x in v)) == pytest.approx(1.0)

    def test_normalize_zero(self) -> None:
        assert normalize([0.0, 0.0]) == [0.0, 0.0]


class TestAverageEmbedding:
    def test_average_of_same_direction(self) -> None:
        emb = average_embedding([[2.0, 0.0], [4.0, 0.0]])
        assert emb == pytest.approx([1.0, 0.0])

    def test_average_of_opposites_cancels(self) -> None:
        emb = average_embedding([[1.0, 0.0], [-1.0, 0.0]])
        assert emb == [0.0, 0.0]

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            average_embedding([])

    def test_dim_mismatch_raises(self) -> None:
        with pytest.raises(ValueError):
            average_embedding([[1.0], [1.0, 2.0]])


class TestEnrollmentStore:
    def test_save_load_roundtrip(self, tmp_path) -> None:
        store = EnrollmentStore(tmp_path / "speakers")
        store.save("master", [0.1, 0.2, 0.3])
        store.save("wife", [0.4, 0.5, 0.6])

        loaded = store.load()
        assert loaded["master"] == pytest.approx([0.1, 0.2, 0.3])
        assert loaded["wife"] == pytest.approx([0.4, 0.5, 0.6])

    def test_delete(self, tmp_path) -> None:
        store = EnrollmentStore(tmp_path)
        store.save("master", [1.0])
        store.delete("master")
        assert store.load() == {}

    def test_corrupted_file_skipped(self, tmp_path) -> None:
        store = EnrollmentStore(tmp_path)
        store.save("master", [1.0, 0.0])
        (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
        loaded = store.load()
        assert "master" in loaded
        assert "bad" not in loaded


class TestEmbeddingVerifier:
    def _store_with_two_speakers(self, tmp_path):
        store = EnrollmentStore(tmp_path)
        store.save("master", normalize([1.0, 0.0, 0.0]))
        store.save("wife", normalize([0.0, 1.0, 0.0]))
        return store

    def test_scores_highest_for_matching_speaker(self, tmp_path) -> None:
        store = self._store_with_two_speakers(tmp_path)

        class FixedEmbed(SeededVerifier):
            def _embed_impl(self, pcm: bytes):
                return normalize([1.0, 0.0, 0.0])

        verifier = FixedEmbed(store, seed=1)
        scores = verifier.scores(b"x")
        assert scores["master"] == pytest.approx(1.0)
        assert scores["wife"] == pytest.approx(0.0)

    def test_scores_cached_until_refresh(self, tmp_path) -> None:
        store = self._store_with_two_speakers(tmp_path)

        class FixedEmbed(SeededVerifier):
            def _embed_impl(self, pcm: bytes):
                return normalize([1.0, 0.0, 0.0])

        verifier = FixedEmbed(store, seed=1)
        assert verifier.scores(b"x")["master"] == pytest.approx(1.0)

        # enroll a new speaker -> cache must be refreshed to see it
        store.save("elder", normalize([1.0, 1.0, 0.0]))
        assert "elder" not in verifier.scores(b"x")
        verifier.refresh()
        assert "elder" in verifier.scores(b"x")

    def test_seeded_verifier_reproducible(self, tmp_path) -> None:
        emb1 = SeededEmbedder(seed=7).embedding()
        emb2 = SeededEmbedder(seed=7).embedding()
        assert emb1 == emb2
        assert math.sqrt(sum(x * x for x in emb1)) == pytest.approx(1.0)

    def test_seeded_verifier_scores(self, tmp_path) -> None:
        store = EnrollmentStore(tmp_path)
        store.save("master", SeededEmbedder(seed=7).embedding())
        store.save("wife", SeededEmbedder(seed=99).embedding())

        speaking_as_master = SeededVerifier(store, seed=7)
        scores = speaking_as_master.scores(b"pcm")
        assert scores["master"] == pytest.approx(1.0, abs=1e-6)
        assert scores["wife"] < 0.5  # random 192-dim vectors are near-orthogonal


class TestCampplusVerifier:
    def test_importable_without_torch(self) -> None:
        """CI guard: module import must never pull torch/onnxruntime."""
        import sys

        import engines.voiceprint  # noqa: F401

        assert "torch" not in sys.modules
        assert "onnxruntime" not in sys.modules

    def test_pcm_to_embedding_via_fake_model(self, tmp_path) -> None:
        """PCM bytes -> float32 waveform -> model.extract_emb -> flat list."""

        class FakeCampplus:
            def extract_emb(self, wav) -> list[list[float]]:
                import numpy as np

                assert isinstance(wav, np.ndarray)
                assert wav.dtype.name == "float32"
                # amplitude sanity: int16 PCM / 32768 must be in [-1, 1]
                assert float(np.abs(wav).max()) <= 1.0
                return [[0.5, 0.5, 0.5, 0.5]]

        store = EnrollmentStore(tmp_path)
        verifier = CampplusVerifier(
            store, model_path=tmp_path / "model.bin", model_factory=FakeCampplus
        )
        # 4 samples of int16 PCM
        pcm = bytes([0x00, 0x40, 0x00, 0x80, 0xFF, 0x7F, 0x00, 0x00])
        emb = verifier.embed(pcm)
        assert emb == pytest.approx([0.5, 0.5, 0.5, 0.5])

    def test_missing_model_raises_filenotfound(self, tmp_path) -> None:
        verifier = CampplusVerifier(
            EnrollmentStore(tmp_path), model_path=tmp_path / "nope.bin"
        )
        with pytest.raises(FileNotFoundError):
            verifier.embed(b"\x00" * 100)


class TestProfilesLoader:
    def test_full_yaml(self, tmp_path) -> None:
        cfg = tmp_path / "speakers.yaml"
        cfg.write_text(
            """
voiceprint:
  model: models/campplus.bin
  embedding_dir: data/speakers
speakers:
  master:
    name: 主人
    role: admin
    priority: 1
  wife:
    name: 老婆
    role: member
    priority: 2
gate:
  t_pass: 0.70
  t_gray: 0.60
""",
            encoding="utf-8",
        )
        setup = load_gate_setup(cfg)
        assert isinstance(setup, GateSetup)
        assert setup.profiles["master"] == SpeakerProfile(
            speaker_id="master", name="主人", role="admin", priority=1
        )
        assert setup.profiles["wife"].role == "member"
        assert setup.config.t_pass == 0.70
        assert setup.config.t_gray == 0.60
        assert setup.voiceprint_model is not None
        assert setup.voiceprint_model.name == "campplus.bin"

    def test_missing_file_returns_defaults(self, tmp_path) -> None:
        setup = load_gate_setup(tmp_path / "absent.yaml")
        assert setup.profiles == {}
        assert setup.config == GateConfig()
        assert setup.voiceprint_model is None
