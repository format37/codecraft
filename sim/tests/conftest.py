"""Shared paths and skips. Run from sim/: `../server/.venv/bin/python -m pytest` (SIM_SLOW=1 for the full corpus)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GAMES = ROOT / "data" / "games"
ENGINE = Path(os.environ.get("ORACLE_ENGINE", ROOT / "engine" / "aicup2020"))
SLOW = os.environ.get("SIM_SLOW") == "1"


def engine_runs() -> bool:
    try:
        return subprocess.run([str(ENGINE), "--version"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


needs_engine = pytest.mark.skipif(not engine_runs(), reason=f"the official engine {ENGINE} cannot run here")


def replays() -> list[Path]:
    return sorted(GAMES.glob("*/replay.dat")) if GAMES.is_dir() else []


needs_corpus = pytest.mark.skipif(not replays(), reason=f"no stored games under {GAMES}")
