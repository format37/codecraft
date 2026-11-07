"""The C++ engine (sim-cpp/) against the official one and against the Python engine.

- Against the official engine: every fixture, as tests/test_engine.py checks the Python engine.
- Against the Python engine, which it copies seed for seed (the same rules, the same draws from CPython's generator,
  in the same order): from the same config and seed, with the same players, the same bytes: the replay, results.json
  and the log lines. On every fixture (puppets over TCP included) with other seeds, on generated maps, on games of
  QuickStart bots, and from states of stored official games.

`make -C sim-cpp` builds the engine; these tests are skipped when that cannot be done (no compiler)."""
from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from codecraft_sim import replay as replay_io
from codecraft_sim.model import model_json
from oracle import fixture

from conftest import ROOT, SLOW, needs_corpus, replays
from test_engine import FIXTURES, NAMES, SIM, check_fixture_run, play_all, play_fixture

CPP = ROOT / "sim-cpp" / "build" / "codecraft-sim-cpp"


def built() -> bool:
    try:
        subprocess.run(["make", "-s", "-C", str(ROOT / "sim-cpp")], capture_output=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        pass
    return CPP.exists()


needs_cpp = pytest.mark.skipif(not built(), reason="the C++ engine is not built (make -C sim-cpp)")
pytestmark = needs_cpp
WORKERS = os.cpu_count() or 4


@pytest.fixture(scope="module")
def cpp_runs():
    return play_all(CPP)


@pytest.mark.parametrize("name", NAMES)
def test_fixture_on_the_cpp_engine(name, cpp_runs):
    check_fixture_run(next(f for f in FIXTURES if f["name"] == name), cpp_runs[name])


def outcome(run) -> tuple:
    return (run.returncode, run.results, fixture.notable_log(run.log),
            [fixture.snapshot(m, ev) for m, ev in zip(run.states, run.events)],
            {seat: [v.current_tick for v in p.views] for seat, p in run.puppets.items()})


def test_same_games_as_python_on_the_fixtures():
    """Every fixture with other seeds than its own, both engines: the same states tick by tick, results and log."""
    seeds = range(2, 11 if SLOW else 4)
    jobs = [(fx, seed) for fx in FIXTURES for seed in seeds]

    def differs(job):
        fx, seed = job
        a, b = outcome(play_fixture(fx, SIM, seed)), outcome(play_fixture(fx, CPP, seed))
        return None if a == b else f"{fx['name']} seed {seed}"

    with ThreadPoolExecutor(WORKERS) as pool:
        bad = [d for d in pool.map(differs, jobs) if d]
    assert not bad, bad


def engine_files(engine: Path, config: dict, out: Path) -> tuple:
    out.mkdir(parents=True, exist_ok=True)
    (out / "config.json").write_text(json.dumps(config))
    r = subprocess.run([str(engine), "--config", str(out / "config.json"), "--batch-mode", "--save-results",
                        str(out / "results.json"), "--save-replay", str(out / "replay.dat"), "--log-level", "info"],
                       capture_output=True, text=True, timeout=1800)
    res = out / "results.json"
    return (r.returncode, r.stderr, (out / "replay.dat").read_bytes(), res.read_text() if res.exists() else None)


def same_files(configs: list[dict], tmp_path: Path) -> list[str]:
    def differs(k):
        a = engine_files(SIM, configs[k], tmp_path / f"py{k}")
        b = engine_files(CPP, configs[k], tmp_path / f"cpp{k}")
        return None if a == b else json.dumps(configs[k])[:200]

    with ThreadPoolExecutor(WORKERS) as pool:
        return [d for d in pool.map(differs, range(len(configs))) if d]


ROUND1 = [["BuilderBase", {"x": 5, "y": 5}], ["BuilderUnit", {"x": 4, "y": 4}], ["MeleeBase", {"x": 5, "y": 15}],
          ["MeleeUnit", {"x": 4, "y": 14}], ["RangedBase", {"x": 15, "y": 5}], ["RangedUnit", {"x": 14, "y": 4}],
          ["Turret", {"x": 15, "y": 15}]]


def custom(seed: int, entities: list, seats: int, fog: bool, ticks: int, players: str = "QuickStart",
           size: int = 80) -> dict:
    return {"seed": seed, "game": {"Create": {"Custom": {
        "map_size": size, "initial_resource": 0, "passive_income": 0, "max_pathfind_nodes": 1000, "fog_of_war": fog,
        "max_tick_count": ticks, "starting_entities": entities}}}, "players": [players] * seats}


def test_same_maps(tmp_path):
    """The presets' maps (a Custom game of 0 ticks writes just the first state), odd and small sizes too."""
    configs = [custom(seed, ents, seats, False, 0, "Empty", size) for seed in range(1, 9)
               for ents, seats in ((ROUND1, 4), (ROUND1[:2], 4), (ROUND1[:2], 2)) for size in (80, 41)]
    assert same_files(configs, tmp_path) == []


def test_same_quickstart_games(tmp_path):
    """Games of QuickStart bots, with and without fog of war, two and four seats; full presets with SIM_SLOW=1."""
    ticks = 300 if SLOW else 80
    configs = [custom(seed, ents, seats, fog, ticks) for seed in (1, 2)
               for ents, seats, fog in ((ROUND1, 4, False), (ROUND1[:2], 4, True), (ROUND1[:2], 2, True))]
    if SLOW:
        configs += [{"seed": seed, "game": {"Create": fmt}, "players": ["QuickStart"] * seats}
                    for seed in (1, 2, 3) for fmt, seats in (("Round1", 4), ("Round2", 4), ("Finals", 2))]
    assert same_files(configs, tmp_path) == []


@needs_corpus
def test_same_games_from_stored_states(tmp_path):
    """States of stored official games (the orders stored with them drive every entity), some ticks on."""
    every, ticks = (7, 15) if SLOW else (40, 6)
    configs = []
    for k, path in enumerate(replays()[::every]):
        for m in replay_io.load(path).states():
            if m.current_tick in (100, 400, 800) and len({e.player_id for e in m.entities.values()} - {None}) > 1:
                m = m.copy()
                m.max_tick_count = min(m.current_tick + ticks, 1000)
                state = tmp_path / f"state{k}_{m.current_tick}.json"
                state.write_text(json.dumps(model_json(m)))
                configs.append({"seed": m.current_tick, "game": {"LoadFrom": str(state)},
                                "players": [{"Empty": None}] * len(m.players)})
    assert configs
    assert same_files(configs, tmp_path) == []


def test_command_line_and_speed(tmp_path):
    """The C++ engine answers the official command line as the Python one does, and is much faster."""
    v = subprocess.run([str(CPP), "--version"], capture_output=True, text=True)
    assert v.returncode == 0 and v.stdout.startswith("codecraft-sim-cpp ")
    assert subprocess.run([str(CPP), "--batch-mode"], capture_output=True).returncode == 101
    import time
    t = time.time()
    rc, _, data, results = engine_files(CPP, {"seed": 3, "game": {"Create": "Round1"}, "players": ["QuickStart"] * 4},
                                        tmp_path / "speed")
    took = time.time() - t
    assert rc == 0 and replay_io.parse(data).tick_count == 1000 and json.loads(results)["seed"] == 3
    assert took < 30, f"a QuickStart Round1 game took {took:.1f} s"
