"""The built-in QuickStart is the starter strategy (python/my_strategy.py of format37/codecraft-starter; the copy in
starter/ names the commit): played by the official Python client in a seat, the starter gives the game that the
built-in bot gives in that seat, byte for byte (the replay and results.json), on the C++ engine; test_cpp.py has the
Python engine play the C++ engine's QuickStart games. When the starter changes, copy it here and port it again to
sim/codecraft_sim/quickstart.py and sim-cpp/src/quickstart.cpp."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT, SLOW
from oracle.loadfrom import WAITING_RE, free_port
from test_cpp import CPP, needs_cpp

STARTER = Path(__file__).parent / "starter" / "my_strategy.py"
GAMES = [("Round2", 1, 4, 0), ("Round2", 7, 4, 2)] + ([("Round1", 3, 4, 1), ("Finals", 5, 2, 1)] if SLOW else [])


def play(out: Path, config: dict, client: Path | None = None) -> tuple[bytes, bytes]:
    """The replay and results.json of a game; `client` runs in the TCP seat once the engine listens."""
    out.mkdir()
    (out / "config.json").write_text(json.dumps(config))
    engine = subprocess.Popen([str(CPP), "--config", str(out / "config.json"), "--batch-mode", "--log-level", "info",
                               "--save-replay", str(out / "replay.dat"), "--save-results", str(out / "results.json")],
                              stderr=subprocess.PIPE, text=True)
    strategy = None
    try:
        if client is not None:
            for line in engine.stderr:
                if m := WAITING_RE.search(line):
                    strategy = subprocess.Popen([sys.executable, "main.py", "127.0.0.1", m.group(1)], cwd=client)
                    break
        log = engine.communicate(timeout=300)[1]
        assert engine.returncode == 0, log
        if strategy is not None:
            assert strategy.wait(30) == 0
    finally:
        for p in (engine, strategy):
            if p is not None and p.poll() is None:
                p.kill()
    return (out / "replay.dat").read_bytes(), (out / "results.json").read_bytes()


@needs_cpp
@pytest.mark.parametrize("fmt,seed,seats,seat", GAMES)
def test_quickstart_plays_the_starter(tmp_path, fmt, seed, seats, seat):
    client = tmp_path / "client"
    shutil.copytree(ROOT / "clients" / "Python", client, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(STARTER, client / "my_strategy.py")
    players = ["QuickStart"] * seats
    tcp = {"Tcp": {"host": "127.0.0.1", "port": free_port(), "accept_timeout": 60, "timeout": None, "token": None}}
    starter = play(tmp_path / "starter", {"seed": seed, "game": {"Create": fmt},
                                          "players": players[:seat] + [tcp] + players[seat + 1:]}, client)
    builtin = play(tmp_path / "builtin", {"seed": seed, "game": {"Create": fmt}, "players": players})
    assert starter == builtin
