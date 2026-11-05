"""Strategies: seed manifests (strategies/<name>/strategy.yaml) and submitted versions (database rows)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import LIMIT_KEYS, Config

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


@dataclass
class Source:
    git: str | None = None
    ref: str | None = None
    dir: str | None = None       # relative to the manifest directory, or absolute
    path: str | None = None
    include: list[str] | None = None
    merge: bool = False          # copy the files over the official client instead of replacing it


@dataclass
class Strategy:
    name: str
    dir: Path | None = None
    title: str = ""
    language: str | None = None
    gpu: bool = False
    builtin: str | None = None
    enabled: bool = True
    source: Source = field(default_factory=Source)
    overlay: str | None = None
    prebuild: str | None = None
    env: dict = field(default_factory=dict)
    limits: dict = field(default_factory=dict)   # overrides of config.yaml `limits` for this strategy
    version_id: int | None = None                # the submitted version this object describes (None for seeds)
    version_number: int | None = None
    label: str | None = None                     # name shown for the seat (defaults to name)

    @property
    def is_builtin(self) -> bool:
        return self.builtin is not None

    @property
    def display(self) -> str:
        return self.label or self.name

    def source_root(self) -> Path | None:
        if self.source.dir is None:
            return None
        p = Path(self.source.dir)
        if not p.is_absolute():
            if self.dir is None:
                raise ValueError(f"{self.name}: relative source dir without a manifest directory")
            p = self.dir / p
        return p.resolve()


def check_limits(limits: dict, where: str) -> dict:
    unknown = set(limits) - LIMIT_KEYS
    if unknown:
        raise ValueError(f"{where}: unknown limits {sorted(unknown)}; allowed: {sorted(LIMIT_KEYS)}")
    return limits


def load_strategy(path: Path) -> Strategy:
    data = yaml.safe_load(path.read_text()) or {}
    name = str(data.get("name") or path.parent.name)
    if not NAME_RE.match(name):
        raise ValueError(f"{path}: invalid strategy name {name!r} (use [a-z0-9_-])")
    src = data.get("source") or {}
    include = src.get("include")
    if isinstance(include, str):
        include = [include]
    strategy = Strategy(
        name=name,
        dir=path.parent,
        title=str(data.get("title") or name),
        language=data.get("language"),
        gpu=bool(data.get("gpu", False)),
        builtin=data.get("builtin"),
        enabled=bool(data.get("enabled", True)),
        source=Source(git=src.get("git"), ref=src.get("ref"), dir=src.get("dir"), path=src.get("path"), include=include,
                      merge=bool(src.get("merge", False))),
        overlay=data.get("overlay"),
        prebuild=data.get("prebuild"),
        env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
        limits=check_limits(dict(data.get("limits") or {}), str(path)),
    )
    if not strategy.is_builtin:
        if not strategy.language:
            raise ValueError(f"{path}: 'language' is required for a non-builtin strategy")
        if not (strategy.source.git or strategy.source.dir):
            raise ValueError(f"{path}: source.git or source.dir is required")
    return strategy


def load_strategies(cfg: Config) -> dict[str, Strategy]:
    result: dict[str, Strategy] = {}
    if not cfg.strategies_dir.exists():
        return result
    for manifest in sorted(cfg.strategies_dir.glob("*/strategy.yaml")):
        strategy = load_strategy(manifest)
        if strategy.name in result:
            raise ValueError(f"duplicate strategy name {strategy.name!r}")
        result[strategy.name] = strategy
    return result


def strategy_from_row(row, seeds: dict[str, Strategy] | None = None) -> Strategy:
    """A runnable Strategy for a database row: the seed manifest when there is one (env, limits),
    else a bare object with the row's language/gpu/limits. The image is chosen by the caller."""
    name = row["name"]
    if seeds and name in seeds and row["kind"] == "seed":
        s = seeds[name]
        s.enabled = bool(row["enabled"])
        return s
    limits = {}
    if row["limits"]:
        try:
            limits = json.loads(row["limits"])
        except ValueError:
            limits = {}
    return Strategy(name=name, title=row["title"] or name, language=row["language"], gpu=bool(row["gpu"]),
                    builtin=row["builtin"], enabled=bool(row["enabled"]), limits=limits,
                    version_id=row["active_version"])


def strategy_for_version(cfg: Config, version, strategy_row) -> Strategy:
    """Build description of a submitted version: zip sources live under data/submissions, git sources
    are cloned by the builder."""
    include = json.loads(version["include"]) if version["include"] else None
    if version["source_kind"] == "zip":
        src = Source(dir=str(cfg.data_dir / version["source"]), include=include, merge=bool(version["merge"]))
    elif version["source_kind"] == "git":
        src = Source(git=version["source"], ref=version["git_ref"] or None, include=include, merge=bool(version["merge"]))
    else:
        raise ValueError(f"unknown source kind {version['source_kind']!r}")
    limits = {}
    if strategy_row is not None and strategy_row["limits"]:
        limits = json.loads(strategy_row["limits"])
    return Strategy(name=version["strategy"], title=strategy_row["title"] if strategy_row else version["strategy"],
                    language=version["language"], gpu=bool(version["gpu"]), source=src, limits=limits,
                    version_id=version["id"], version_number=version["number"])
