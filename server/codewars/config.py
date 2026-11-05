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
    "gpu": {
        "enabled": "auto",          # true / false / auto (nvidia runtime visible to docker)
        "max_per_game": 1,          # how many GPU strategies may share one game (whole GPU each)
    },
    "worker": {
        "parallel_games": 1,
        "pause_sec": 5,
        "sandbox": True,            # play rated sandbox games when no custom game is queued
        "sandbox_games_per_build": 10,  # each build plays this many sandbox games, then rests (0 = endless)
        "builds": True,             # build submitted strategy versions
        "build_timeout_sec": 1200,
    },
    "submissions": {
        "max_zip_mb": 20,
        "max_files": 2000,
        "max_unpacked_mb": 200,
        "languages": None,          # null = every template/ and client/ language; or a list of names
        "allow_git": True,
        "per_user_queued": 1,       # versions waiting/building at once
        "per_user_per_day": 20,
        "custom_games_queued": 3,   # custom games a user may have waiting at once
        "custom_games_per_day": 50,
    },
    "auth": {
        "registration": "open",     # open | invite | closed
        "admins": [],               # e-mails that get the admin role on sign-in
        "base_url": "http://localhost:8080",
        "cookie_secure": "auto",    # auto = true when base_url is https
        "session_ttl_days": 90,
        "code_ttl_minutes": 10,
        "code_attempts": 5,
    },
    "mail": {
        "provider": "auto",         # auto (resend if CW_RESEND_API_KEY, else smtp if host, else console)
        "from": "CodeWars <onboarding@resend.dev>",
        "resend_api_key": None,     # or env CW_RESEND_API_KEY
        "smtp": {"host": None, "port": 587, "user": None, "password": None, "starttls": True},
    },
    "sentry": {"dsn": None, "environment": "production"},   # or env CW_SENTRY_DSN; unset = monitoring off
    "web": {"host": "0.0.0.0", "port": 8080, "title": "CodeWars"},
    "docker": {"prefix": "cw", "label": "codewars.game"},
    "hardening": {"no_new_privileges": True, "cap_drop_all": True},
}

# Seats per game format; the engine supports at most 4 players.
SEATS = {"Round1": 4, "Round2": 4, "Finals": 2}
MAX_TICKS = 1000
LIMIT_KEYS = {"tick_timeout_sec", "cpu_time_sec", "memory_mb", "cpus", "pids", "accept_timeout_sec"}


def _tri(value):
    """true / false / auto from a config or environment value."""
    if value in (True, False):
        return value
    v = str(value).strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return "auto"


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
        if "loop" in data:  # v0.1 name of the worker section
            deep_merge(data["worker"], data.pop("loop"))
        env = os.environ
        if env.get("CW_RESEND_API_KEY"):
            data["mail"]["resend_api_key"] = env["CW_RESEND_API_KEY"]
        if env.get("CW_MAIL_FROM"):
            data["mail"]["from"] = env["CW_MAIL_FROM"]
        if env.get("CW_BASE_URL"):
            data["auth"]["base_url"] = env["CW_BASE_URL"]
        if env.get("CW_ADMINS"):
            data["auth"]["admins"] = [a.strip() for a in env["CW_ADMINS"].split(",") if a.strip()]
        if env.get("CW_SMTP_PASSWORD"):
            data["mail"]["smtp"]["password"] = env["CW_SMTP_PASSWORD"]
        if env.get("CW_REGISTRATION"):
            data["auth"]["registration"] = env["CW_REGISTRATION"].strip().lower()
        if env.get("CW_GPU_ENABLED"):
            data["gpu"]["enabled"] = _tri(env["CW_GPU_ENABLED"])
        if env.get("CW_SENTRY_DSN", "").strip():
            data["sentry"]["dsn"] = env["CW_SENTRY_DSN"].strip()
        if env.get("CW_SENTRY_ENVIRONMENT", "").strip():
            data["sentry"]["environment"] = env["CW_SENTRY_ENVIRONMENT"].strip()
        data["gpu"]["enabled"] = _tri(data["gpu"].get("enabled", "auto"))
        if str(data["auth"].get("cookie_secure", "auto")).lower() == "auto":
            data["auth"]["cookie_secure"] = str(data["auth"].get("base_url") or "").lower().startswith("https")
        return cls(data, root_path)

    # GPU ---------------------------------------------------------------
    def resolve_gpu(self, client) -> bool:
        """Decide once whether GPU strategies can run here: `gpu.enabled` true/false, or auto = the nvidia
        runtime is registered in docker. Remembered in `gpu.available` for the UI and submissions."""
        mode = self.data["gpu"].get("enabled", "auto")
        if mode in (True, False):
            ok = bool(mode)
        elif client is None:
            ok = False
        else:
            try:
                ok = "nvidia" in (client.info().get("Runtimes") or {})
            except Exception:  # noqa: BLE001
                ok = False
        self.data["gpu"]["available"] = ok
        return ok

    @property
    def gpu_ok(self) -> bool:
        """Resolved GPU availability; before resolve_gpu() an `auto` setting counts as available."""
        v = self.data["gpu"].get("available")
        if v is None:
            v = self.data["gpu"].get("enabled", "auto")
            return v is not False
        return bool(v)

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
    def submissions_dir(self) -> Path:
        return self.data_dir / "submissions"

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
        for d in (self.data_dir, self.games_dir, self.build_dir, self.submissions_dir):
            d.mkdir(parents=True, exist_ok=True)

    def languages(self, gpu: bool | None = None) -> list[str]:
        """Languages a user may submit: templates/<lang> first, then official clients. The CUDA templates
        (`*-cuda`) only when the GPU is available (`gpu` overrides the resolved value)."""
        allowed = self.get("submissions", "languages")
        gpu = self.gpu_ok if gpu is None else gpu
        found: list[str] = []
        if self.templates_dir.exists():
            found += sorted(p.name for p in self.templates_dir.iterdir() if (p / "Dockerfile").is_file())
        if self.clients_dir.exists():
            for p in sorted(self.clients_dir.iterdir()):
                if (p / "Dockerfile").is_file() and p.name.lower() not in found:
                    found.append(p.name.lower())
        if allowed:
            found = [l for l in found if l in allowed]
        if not gpu:
            found = [l for l in found if not l.endswith("-cuda")]
        return found
