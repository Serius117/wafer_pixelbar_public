"""Multi-project support: projects.yaml loader + alias utilities (D33).

projects.yaml is the single source of truth for project aliases and the
current default. The Bridge subscribes to PROJECT_SWITCH events at runtime;
on restart it reads the yaml's `default` field to recover.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("config/projects.yaml")


@dataclass(frozen=True, slots=True)
class ProjectEntry:
    alias: str
    path: Path
    description: str = ""


@dataclass(slots=True)
class ProjectConfig:
    entries: dict[str, ProjectEntry] = field(default_factory=dict)
    default_alias: str = ""

    def resolve(self, alias: str | None) -> ProjectEntry | None:
        """Resolve an alias (or the default) to a ProjectEntry."""
        if alias is None:
            alias = self.default_alias
        return self.entries.get(alias)

    @property
    def default_path(self) -> Path | None:
        entry = self.resolve(None)
        return entry.path if entry else None

    def aliases(self) -> list[str]:
        return list(self.entries.keys())


def load_projects(config_path: Path = DEFAULT_CONFIG_PATH) -> ProjectConfig:
    """Parse projects.yaml; missing file -> empty config (caller handles)."""
    config_path = Path(config_path)
    if not config_path.exists():
        return ProjectConfig()

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    entries: dict[str, ProjectEntry] = {}
    repo_root = config_path.resolve().parent.parent
    for alias, spec in (raw.get("projects") or {}).items():
        if not isinstance(spec, dict) or "path" not in spec:
            logger.warning("skip bad project entry: %s", alias)
            continue
        raw_path = Path(str(spec["path"]))
        # Relative paths resolve against the repo root so the same config
        # works on the dev machine and the home machine (D48 follow-up).
        path = raw_path if raw_path.is_absolute() else (repo_root / raw_path)
        entries[str(alias)] = ProjectEntry(
            alias=str(alias),
            path=path,
            description=str(spec.get("description", "")),
        )

    default = str(raw.get("default", ""))
    if default and default not in entries:
        logger.warning("default alias '%s' not in projects, ignoring", default)
        default = ""

    return ProjectConfig(entries=entries, default_alias=default)


def save_default(config_path: Path, alias: str) -> None:
    """Persist the current default alias (called on every project switch)."""
    config_path = Path(config_path)
    if not config_path.exists():
        return
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw["default"] = alias
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def add_project(config_path: Path, alias: str, path: Path,
                description: str = "") -> None:
    """Register a new project alias (console project_add)."""
    config_path = Path(config_path)
    raw: dict = {}
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    projects = raw.setdefault("projects", {})
    if alias in projects:
        raise ValueError(f"alias '{alias}' already registered")
    projects[alias] = {"path": str(path)}
    if description:
        projects[alias]["description"] = description
    config_path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def suggest_alias_candidates(spoken: str, aliases: list[str], top: int = 3) -> list[str]:
    """Rank registered aliases by character-overlap similarity to what was
    spoken. Used for the not-found clarification (D33: closest first)."""
    def overlap(a: str, b: str) -> int:
        return len(set(a) & set(b))

    ranked = sorted(aliases, key=lambda x: -overlap(spoken, x))
    return ranked[:top]
