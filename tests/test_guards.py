"""GUARD-04/05/06 (TEST_DESIGN §8): architecture invariants as static scans.

These guards turn AGENTS.md iron rules into CI-executable assertions:
  GUARD-04  rule 3: HID single-writer -- only render/sinks/pixelbar touches
            the HID write API
  GUARD-05  rule 4: no hardcoded robot name ("小饼") outside config/tests
  GUARD-06  rule 5: core/ stays free of heavy engine dependencies
            (torch / hidapi / numpy / onnxruntime / OLV)
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent

# Directories scanned for source code
SRC_DIRS = ["core", "bridge", "ctl", "engines", "gamedog", "render", "scripts"]
PY_FILES = [
    p for d in SRC_DIRS for p in (ROOT / d).rglob("*.py")
]


def read_sources() -> dict[Path, str]:
    return {p: p.read_text(encoding="utf-8", errors="replace") for p in PY_FILES}


class TestGuard04HidSingleWriter:
    """Rule 3: only render/sinks/pixelbar may reference the HID write API.

    scripts/accept_home.py is exempted: it only *enumerates* devices for
    the home acceptance check (read-only, never writes).
    """

    HID_PATTERNS = (
        re.compile(r"\bimport\s+hid\b"),
        re.compile(r"\bfrom\s+hid\b"),
        re.compile(r"\bhid\.device\s*\("),
    )

    def test_only_pixelbar_sink_touches_hid(self) -> None:
        offenders: list[str] = []
        for path, text in read_sources().items():
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith("render/sinks/pixelbar/"):
                continue  # the single writer itself
            if rel == "scripts/accept_home.py":
                continue  # read-only acceptance enumeration
            for pattern in self.HID_PATTERNS:
                if pattern.search(text):
                    offenders.append(rel)
                    break
        assert not offenders, f"HID write API leaked outside pixelbar sink: {offenders}"


class TestGuard05NoHardcodedRobotName:
    """Rule 4: the robot name is config (robot.name), never hardcoded.

    Allowed literal locations: core/naming.py (the single factory-default
    source), config templates, scripts/ (operator tools / E2E args).
    """

    def test_no_hardcoded_name_in_sources(self) -> None:
        offenders: list[str] = []
        for path, text in read_sources().items():
            rel = path.relative_to(ROOT).as_posix()
            if rel == "core/naming.py" or rel.startswith(("config", "scripts/")):
                continue
            if "小饼" in text:
                offenders.append(rel)
        assert not offenders, f"hardcoded robot name found in: {offenders}"


class TestGuard06CoreStaysLight:
    """Rule 5: core/ has zero heavy engine dependencies (HAL layering)."""

    HEAVY_IMPORTS = (
        re.compile(r"^\s*import\s+(torch|numpy|onnxruntime)\b", re.M),
        re.compile(r"^\s*from\s+(torch|numpy|onnxruntime)\b", re.M),
        re.compile(r"^\s*import\s+hid\b", re.M),
        re.compile(r"\bOpenLLMVTuber\b|\bolv\.upstream\b"),
    )

    def test_core_has_no_heavy_imports(self) -> None:
        offenders: list[str] = []
        for path in (ROOT / "core").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in self.HEAVY_IMPORTS:
                if pattern.search(text):
                    offenders.append(path.relative_to(ROOT).as_posix())
                    break
        assert not offenders, f"heavy deps leaked into core/: {offenders}"


class TestGuard09NoSecretsInRepo:
    """GUARD-09: no hardcoded API keys in tracked sources."""

    SECRET_PATTERNS = (
        re.compile(r"sk-[A-Za-z0-9]{20,}"),
        re.compile(r"api[_-]?key\s*=\s*[\"'][A-Za-z0-9\-]{16,}[\"']", re.I),
    )

    def test_no_secrets_in_sources(self) -> None:
        offenders: list[str] = []
        for path, text in read_sources().items():
            for pattern in self.SECRET_PATTERNS:
                if pattern.search(text):
                    offenders.append(path.relative_to(ROOT).as_posix())
                    break
        assert not offenders, f"possible hardcoded secrets in: {offenders}"
