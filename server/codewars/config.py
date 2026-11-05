"""Configuration: config.yaml deep-merged over built-in defaults, plus project paths."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

DEFAULTS: dict = {
    "engine": {
        "image": "codewars/engine:1.3.0",
        "binary": "engine/aicup2020",
        "memory_mb": 512,
        "cpus": 1.0,
        "startup_timeout_sec": 30,
    },
    "limits": {
        "tick_timeout_sec": 1.0,
        "cpu_time_sec": 40,
        "memory_mb": 1024,
        "cpus": 1.0,
        "pids": 256,
        "accept_timeout_sec": 60,
        "game_wall_timeout_sec": None,
    },
    "formats": {"Round1": 1, "Round2": 1, "Finals": 1},
    "points": {"Round1": [8, 4, 2, 1], "Round2": [8, 4, 2, 1], "Finals": [2, 0]},
    "matchmaking": {"rating_sigma": 200, "include_builtin": True, "fill_with_builtin": True},
    "rating": {"initial": 1200, "k": 32, "k_min": 16, "k_half_life_games": 50},
    "loop": {"parallel_games": 1, "pause_sec": 5},
    "web": {"host": "0.0.0.0", "port": 8080},
    "docker": {"prefix": "cw", "label": "codewars.game"},
}

# Seats per game format; the engine supports at most 4 players.
SEATS = {"Round1": 4, "Round2": 4, "Finals": 2}
MAX_TICKS = 1000


def deep_merge(base: dict, override: dict) -> dict:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


class Config:
    def __init__(self, data: dict, root: Path):
        self.data = data
        self.root = root

    @classmethod
    def load(cls, root: str | os.PathLike | None = None) -> "Config":
        root_path = Path(root or os.environ.get("CW_ROOT") or Path(__file__).resolve().parents[2])
        data = copy.deepcopy(DEFAULTS)
        cfg_file = root_path / "config.yaml"
        if cfg_file.exists():
            deep_merge(data, yaml.safe_load(cfg_file.read_text()) or {})
        return cls(data, root_path)

    def __getitem__(self, key: str):
        return self.data[key]

    def get(self, *keys, default=None):
        node = self.data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    # Paths -------------------------------------------------------------
    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def games_dir(self) -> Path:
        return self.data_dir / "games"

    @property
    def build_dir(self) -> Path:
        return self.data_dir / "build"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "codewars.db"

    @property
    def strategies_dir(self) -> Path:
        return self.root / "strategies"

    @property
    def clients_dir(self) -> Path:
        return self.root / "clients"

    @property
    def templates_dir(self) -> Path:
        return self.root / "templates"

    @property
    def engine_dir(self) -> Path:
        return self.root / "engine"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.games_dir, self.build_dir):
            d.mkdir(parents=True, exist_ok=True)
