"""Run the official engine (`engine/aicup2020`, the oracle) from a state and read back what it did.

    run = loadfrom.run(state.model, ["Empty", "Empty"])                 # stored actions only, no TCP
    run = loadfrom.run(model, [Puppet({0: {...}}), "Empty"], seed=7)    # a scripted TCP player in seat 0
    run.states[t]       # the Model at tick t (decoded from the replay the engine wrote)
    run.events[t]       # the events of the tick that led from states[t - 1] to states[t] (events[0] = [])
    run.results         # results.json, or None (the engine writes none for a game that is over at the start)

The engine runs on this host directly, with `"game": {"LoadFrom": <state file>}`; a seat is "Empty",
"QuickStart" or a `Puppet` (a TCP seat on 127.0.0.1 with a free port and a random token). It runs
`max_tick_count - current_tick` ticks, or fewer if at most one player still owns entities.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from codecraft_sim import replay as replay_io
from codecraft_sim.model import Event, Model, model_json

from .puppet import Puppet

ROOT = Path(__file__).resolve().parents[2]
ENGINE = Path(os.environ.get("ORACLE_ENGINE", ROOT / "engine" / "aicup2020"))
WAITING_RE = re.compile(r"Waiting for connection on port (\d+)")


@dataclass
class EngineRun:
    returncode: int
    log: str
    results: dict | None
    replay: replay_io.Replay | None
    states: list[Model] = field(default_factory=list)
    events: list[list[Event]] = field(default_factory=list)
    puppets: dict[int, Puppet] = field(default_factory=dict)     # seat -> puppet
    workdir: Path | None = None

    @property
    def final(self) -> Model:
        return self.states[-1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(model: Model, players: list, seed: int | None = 1, timeout: float | None = 1.0,
        accept_timeout: float | None = 5.0, engine: Path = ENGINE, keep: bool = False,
        wall_timeout: float = 120.0) -> EngineRun:
    """Play `model` on the official engine. `timeout` is the per-tick reply budget of the TCP seats (seconds,
    None = unlimited), `accept_timeout` how long the engine waits for them to connect. `keep` leaves the work
    directory (config, state, replay, results) in place and on the result."""
    workdir = Path(tempfile.mkdtemp(prefix="oracle-", dir=os.environ.get("ORACLE_TMP")))
    state_path = workdir / "state.json"
    state_path.write_text(json.dumps(model_json(model)))
    seats, puppets, tokens = [], {}, {}
    for seat, p in enumerate(players):
        if isinstance(p, Puppet):
            port, token = free_port(), secrets.token_hex(8)
            seats.append({"Tcp": {"host": "127.0.0.1", "port": port, "accept_timeout": accept_timeout,
                                  "timeout": timeout, "token": token}})
            puppets[seat], tokens[port] = p, token
        elif p == "Empty":
            seats.append({"Empty": None})
        else:
            seats.append(p)     # "QuickStart", or a raw seat config
    config = {"seed": seed, "game": {"LoadFrom": str(state_path)}, "players": seats}
    (workdir / "config.json").write_text(json.dumps(config, indent=1))
    results_path, replay_path = workdir / "results.json", workdir / "replay.dat"
    cmd = [str(engine), "--config", str(workdir / "config.json"), "--batch-mode", "--save-results",
           str(results_path), "--save-replay", str(replay_path), "--log-level", "info"]
    proc = subprocess.Popen(cmd, cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    lines: list[str] = []
    listening: dict[int, threading.Event] = {int(port): threading.Event() for port in tokens}

    def pump() -> None:
        for raw in proc.stdout:
            line = raw.decode(errors="replace")
            lines.append(line)
            m = WAITING_RE.search(line)
            if m and int(m.group(1)) in listening:
                listening[int(m.group(1))].set()
        for ev in listening.values():       # the engine is gone: nobody waits any longer
            ev.set()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        for seat, p in puppets.items():
            port = seats[seat]["Tcp"]["port"]
            listening[port].wait(30)
            p.start("127.0.0.1", port, tokens[port])
        returncode = proc.wait(wall_timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        returncode = proc.wait()
        lines.append(f"[oracle] killed after {wall_timeout} s\n")
    reader.join(10)
    for p in puppets.values():
        p.join(10)
    results = json.loads(results_path.read_text()) if results_path.exists() else None
    rep = replay_io.load(replay_path) if replay_path.exists() and replay_path.stat().st_size else None
    out = EngineRun(returncode, "".join(lines), results, rep, puppets=puppets, workdir=workdir if keep else None)
    if rep is not None:
        out.states = list(rep.states())
        out.events = [[]] + [t.events for t in rep.ticks]
    if not keep:
        shutil.rmtree(workdir, ignore_errors=True)
    return out
