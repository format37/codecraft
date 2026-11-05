"""Strategy manifests (strategies/<name>/strategy.yaml)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import Config

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


@dataclass
class Source:
    git: str | None = None
    ref: str | None = None
    dir: str | None = None
    path: str | None = None
    include: list[str] | None = None
    merge: bool = False          # copy the files over the official client instead of replacing it


@dataclass
class Strategy:
    name: str
    dir: Path
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

    @property
    def is_builtin(self) -> bool:
        return self.builtin is not None


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
        limits=dict(data.get("limits") or {}),
    )
    allowed = {"tick_timeout_sec", "cpu_time_sec", "memory_mb", "cpus", "pids", "accept_timeout_sec"}
    unknown = set(strategy.limits) - allowed
    if unknown:
        raise ValueError(f"{path}: unknown limits {sorted(unknown)}; allowed: {sorted(allowed)}")
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
