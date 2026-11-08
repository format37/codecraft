"""Our engine as a whole (codecraft_sim/engine.py, players.py, cli.py), in place of the official binary: every fixture
is played again by `sim/codecraft-sim` through the same oracle tools (`loadfrom.run`, puppets over TCP), and what
comes out must be what the official engine gave: the exit code, the log lines, the crashes and their comments,
the views and the `Finish` each puppet got, and every tick, until a turn that the official engine plays differently
from seed to seed comes out as another of its outcomes (from there on our game is another game). Plus the
command line, `Create` games, determinism and results.json."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from codecraft_sim import replay as replay_io
from codecraft_sim.model import EntityType, view_json
from oracle import fixture, loadfrom
from oracle.puppet import Puppet

from conftest import ROOT, SLOW

SIM = ROOT / "sim" / "codecraft-sim"
os.environ.setdefault("SIM_PYTHON", sys.executable)
FIXTURES = fixture.all_fixtures()
NAMES = [fx["name"] for fx in FIXTURES]


def play_fixture(fx: dict, engine: Path = SIM, seed: int | None = None) -> loadfrom.EngineRun:
    """A fixture played by `engine` (its own seed unless given), its puppets over TCP."""
    players = [Puppet.from_spec(p) if isinstance(p, dict) else p for p in fx["players"]]
    model = fixture.state_from_json(fx["state"])
    return loadfrom.run(model, players, seed=fx["seed"] if seed is None else seed, timeout=fx.get("timeout", 1.0),
                        accept_timeout=fx.get("accept_timeout", 5.0), engine=engine)


def play_all(engine: Path) -> dict[str, loadfrom.EngineRun]:
    with ThreadPoolExecutor(os.cpu_count() or 4) as pool:
        return dict(zip(NAMES, pool.map(lambda fx: play_fixture(fx, engine), FIXTURES)))


@pytest.fixture(scope="module")
def runs() -> dict[str, loadfrom.EngineRun]:
    return play_all(SIM)


def follows(fx: dict, run: loadfrom.EngineRun) -> int:
    """How many states of our run, from the first, are the fixture's (asserting that the first one that is not is
    another outcome the official engine gives for that turn)."""
    exp = fx["expected"]
    ours = [fixture.snapshot(m, ev) for m, ev in zip(run.states, run.events)]
    alts = exp.get("alternatives", {})
    for i, (a, b) in enumerate(zip(exp["ticks"], ours)):
        if a == b:
            continue
        assert i > 0, "the first state is the state given"
        keys = {fixture.outcome_key(o) for o in alts.get(str(a["tick"]), [])}
        assert fixture.outcome_key(b) in keys, (f"tick {a['tick']}: not an outcome of the official engine\n"
                                                f"expected {fixture.outcome(a)}\ngot      {fixture.outcome(b)}")
        assert b["entity_actions"] == a["entity_actions"], f"tick {a['tick']}: the stored orders"
        return i
    assert len(ours) == len(exp["ticks"]), "the game ends at the same tick"
    return len(ours)


@pytest.mark.parametrize("name", NAMES)
def test_fixture_on_our_engine(name, runs):
    check_fixture_run(next(f for f in FIXTURES if f["name"] == name), runs[name])


def check_fixture_run(fx: dict, run: loadfrom.EngineRun) -> None:
    """What an engine made of a fixture is what the official engine made of it (see the module docstring)."""
    exp = fx["expected"]
    assert run.returncode == exp["returncode"], run.log
    assert fixture.notable_log(run.log) == exp["log"]
    n = follows(fx, run)
    same = n == len(exp["ticks"])           # the whole game as recorded
    if exp["results"] is None:
        assert run.results is None
    else:
        assert run.results["players"] == exp["results"]["players"]
        assert run.results["seed"] == exp["results"]["seed"]
        if same:
            assert run.results["results"] == exp["results"]["results"]
    last = exp["ticks"][n - 1]["tick"]          # the views of later ticks show another game
    for seat, p in exp.get("puppets", {}).items():
        pup = run.puppets[int(seat)]
        assert [v.current_tick for v in pup.views] == p["view_ticks"], f"seat {seat}"
        assert pup.finished == p["finished"], f"seat {seat}"
        for got, want in zip(pup.views, p.get("views", [])):
            if want["current_tick"] <= last:
                assert view_json(got) == want, f"seat {seat}, tick {want['current_tick']}"


def sim(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([str(SIM), *args], capture_output=True, text=True, cwd=cwd, timeout=300)


def test_command_line(tmp_path):
    v = sim("--version")
    assert v.returncode == 0 and v.stdout.startswith("codecraft-sim ")
    assert sim("--batch-mode").returncode == 101                 # no config: as the official runner
    bad = tmp_path / "bad.json"
    bad.write_text('{"seed": 1, "game": {"Create": "Round9"}, "players": ["Empty", "Empty"]}')
    r = sim("--config", str(bad), "--batch-mode")
    assert r.returncode == 101 and "Round9" in r.stderr


def create_game(tmp_path: Path, game, players: list, seed=5, name="game") -> tuple[subprocess.CompletedProcess, Path]:
    config = tmp_path / f"{name}.json"
    config.write_text(json.dumps({"seed": seed, "game": {"Create": game}, "players": players}))
    out = tmp_path / name
    out.mkdir()
    r = sim("--config", str(config), "--batch-mode", "--save-results", str(out / "results.json"),
            "--save-replay", str(out / "replay.dat"), "--log-level", "info", "--player-names", *map(str, players))
    return r, out


def custom(ticks: int, fog: bool = False, entities=(("BuilderBase", 5, 5), ("BuilderUnit", 4, 4))) -> dict:
    return {"Custom": {"map_size": 40, "initial_resource": 100, "passive_income": 0, "max_pathfind_nodes": 300,
                       "fog_of_war": fog, "max_tick_count": ticks,
                       "starting_entities": [[t, {"x": x, "y": y}] for t, x, y in entities]}}


def test_create_game_is_deterministic(tmp_path):
    """A seed and deterministic players replay a game exactly: the same replay, byte for byte, and results.json as
    the official engine writes it; the server's decoder reads the replay. (A seed whose map leaves the QuickStart
    house's fixed spot free: the bots build, repair and mine.)"""
    a, da = create_game(tmp_path, custom(60, fog=True), ["QuickStart"] * 4, seed=15, name="a")
    b, db = create_game(tmp_path, custom(60, fog=True), ["QuickStart"] * 4, seed=15, name="b")
    assert a.returncode == b.returncode == 0, a.stderr
    assert (da / "replay.dat").read_bytes() == (db / "replay.dat").read_bytes()
    text = (da / "results.json").read_text()
    res = json.loads(text)
    assert text == json.dumps(res, indent=2) and not text.endswith("\n")
    assert res["seed"] == 15 and len(res["players"]) == len(res["results"]) == 4
    assert res["players"] == [{"crashed": False, "comment": None}] * 4
    rep = replay_io.load(da / "replay.dat")
    assert rep.tick_count == 60
    final = list(rep.states())[-1]
    assert res["results"] == [p.score for p in sorted(final.players.values(), key=lambda p: p.index)]
    assert sum(e.entity_type == EntityType.HOUSE and e.active for e in final.entities.values()) == 4, \
        "every bot built its house"
    sys.path.insert(0, str(ROOT / "server"))
    try:
        from codecraft import replay as server_replay
        decoded = server_replay.parse((da / "replay.dat").read_bytes())
        assert server_replay.final_scores(decoded) == res["results"]
    finally:
        sys.path.remove(str(ROOT / "server"))


def test_null_seed_is_drawn_and_written(tmp_path):
    r, out = create_game(tmp_path, custom(3), ["Empty", "Empty"], seed=None)
    assert r.returncode == 0, r.stderr
    seed = json.loads((out / "results.json").read_text())["seed"]
    assert isinstance(seed, int) and 0 <= seed < 1 << 64


@pytest.mark.skipif(not SLOW, reason="SIM_SLOW=1: three games of 1000 ticks (~45 s)")
def test_presets_play(tmp_path):
    """Each preset plays its 1000 ticks with its seats and fog of war (Empty seats: nothing moves)."""
    for game, seats, fog in (("Round1", 4, False), ("Round2", 4, True), ("Finals", 2, True)):
        config = tmp_path / f"{game}.json"
        config.write_text(json.dumps({"seed": 3, "game": {"Create": game}, "players": ["Empty"] * seats}))
        out = tmp_path / f"{game}.dat"
        r = subprocess.run([str(SIM), "--config", str(config), "--batch-mode", "--save-replay", str(out)],
                           capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, r.stderr
        rep = replay_io.load(out)
        m = rep.initial
        assert (m.map_size, m.max_tick_count, m.max_pathfind_nodes, m.fog_of_war) == (80, 1000, 1000, fog)
        assert len(m.players) == seats and rep.tick_count == 1000
