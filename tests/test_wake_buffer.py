"""Standby wake-tail merge (TASK.WAKE.08, 2026-10-29).

VAD may split "小饼…小饼" into two segments; KWS needs the concatenated
four-syllable phrase. These are pure-buffer tests (no sherpa/no mic).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))


def _merge(monkeypatch):
    monkeypatch.setenv("WAFER_NO_WARMUP", "1")
    from olv.wafer_hooks import _merge_standby_audio

    return _merge_standby_audio


def test_merge_concatenates_previous_and_current(monkeypatch):
    merge = _merge(monkeypatch)
    prev = np.ones(16000 * 2, dtype=np.float32)
    cur = np.ones(16000, dtype=np.float32) * 2
    merged, tail = merge(prev, cur, tail_seconds=2.0, max_seconds=6.0)
    assert merged.size == 16000 * 3
    assert tail.size == 16000 * 2
    assert tail[-1] == 2


def test_merge_caps_max_and_trims_tail(monkeypatch):
    merge = _merge(monkeypatch)
    prev = np.ones(16000 * 5, dtype=np.float32)
    cur = np.ones(16000 * 5, dtype=np.float32) * 3
    merged, tail = merge(prev, cur, tail_seconds=2.0, max_seconds=6.0)
    assert merged.size == 16000 * 6
    assert tail.size == 16000 * 2
    assert tail[-1] == 3


def test_merge_without_previous(monkeypatch):
    merge = _merge(monkeypatch)
    cur = np.ones(16000, dtype=np.float32)
    merged, tail = merge(None, cur, tail_seconds=3.0, max_seconds=6.0)
    assert merged.size == 16000
    assert tail.size == 16000
