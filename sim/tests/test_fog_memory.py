"""The arena's change to the 2020 rules (rules.FogMemory, the engines' default; `--rules 2020` plays without it): under
fog of war a path search also knows the resources and buildings of others its player has been shown, as last shown,
where the 2020 engine took every cell out of sight for free. What is remembered and forgotten, what a search makes of
it, the endless to-and-fro it ends, and the switch on the command line. The C++ engine plays the same games under
either rules (test_cpp.py)."""
from __future__ import annotations

import json
import random
import subprocess
from pathlib import Path

from codecraft_sim import replay as replay_io
from codecraft_sim import rules, view
from codecraft_sim.model import Entity, EntityType, Vec2, model_json
from oracle.state import State, move

from conftest import ROOT

SIM = ROOT / "sim" / "codecraft-sim"


def sight(m) -> dict:
    return {pid: view.sight_map(m, pid) for pid in m.players}


def put(m, eid: int, at: tuple[int, int]) -> None:
    e = m.entities[eid]
    m.entities[eid] = Entity(e.id, e.player_id, e.entity_type, Vec2(*at), e.health, e.active)


def scene():
    """Player 1's builder (sight 10) at (2, 2) sees a resource, player 2's house and player 2's unit; a resource far
    away it does not see."""
    s = State(map_size=40, players=2, ticks=10, fog=True)
    unit = s.add("BuilderUnit", 1, (2, 2))
    near, far = s.resources([(6, 2), (30, 30)])
    house = s.add("House", 2, (2, 8))
    enemy = s.add("BuilderUnit", 2, (4, 4))
    return s.model, unit, near, far, house, enemy


def test_what_is_remembered_and_forgotten():
    m, unit, near, far, house, enemy = scene()
    mem = rules.FogMemory()
    mem.update(m, sight(m))
    known = mem.known[1]
    assert set(known) == {near, house}                   # not the unseen resource, not the enemy unit
    assert known[near] == (None, 6, 2, 1, 30) and known[house] == (2, 2, 8, 3, 50)
    cells = mem.cells(1, 40)
    assert cells[2 * 40 + 6] == near and cells[10 * 40 + 4] == house and cells[30 * 40 + 30] == rules.FREE

    put(m, unit, (30, 25))                               # far away: the resource and the house out of sight
    m.entities[near].health = 7
    mem.update(m, sight(m))
    assert mem.known[1][near][4] == 30                    # what the player saw, not what it has become
    assert far in mem.known[1]                            # seen now

    del m.entities[near]                                  # mined out while nobody of player 1 looks
    mem.update(m, sight(m))
    assert near in mem.known[1]

    put(m, unit, (2, 2))                                  # back: its cell is seen, it is gone
    mem.update(m, sight(m))
    assert near not in mem.known[1] and house in mem.known[1]
    assert mem.cells(1, 40)[2 * 40 + 6] == rules.FREE


def test_a_search_takes_an_unseen_cell_for_what_is_remembered():
    m, unit, near, far, house, enemy = scene()
    mem = rules.FogMemory()
    rules._Turn(m, random.Random(1), None, mem).play()    # tick 0: player 1 sees and remembers
    put(m, unit, (30, 25))
    m.entities[near].health = 7
    i = 2 * 40 + 6
    ours = rules._Turn(m, random.Random(1), None, mem)
    ours.memory.update(m, {pid: ours.seen(pid) for pid in m.players})
    e = m.entities[unit]
    assert ours.entry_cost(e, False)(i) is None           # remembered: an obstacle
    assert ours.entry_cost(e, True)(i) == 30              # the hits it took when last seen (damage 1)
    assert ours.entry_cost(e, True)(10 * 40 + 3) == 50    # the house, as remembered
    old = rules._Turn(m, random.Random(1), None, None)   # the 2020 rules: out of sight is free
    assert old.entry_cost(e, False)(i) == 1 and old.entry_cost(e, True)(i) == 1


def test_nothing_is_remembered_without_fog():
    m, *_ = scene()
    m.fog_of_war = False
    mem = rules.FogMemory()
    rules.process_turn(m, random.Random(1), None, mem)
    assert mem.known == {}


def wall(ticks: int = 300):
    """A builder at (2, 20) told to go to (37, 20) behind a wall of resources at x = 12 from y = 0 to 36, far longer
    than the builder's sight: under the 2020 rules the edge of its sight moves with every step and the shortest path
    through the unseen cells flips from one end of the wall to the other."""
    s = State(map_size=40, players=2, ticks=ticks, fog=True)
    unit = s.add("BuilderUnit", 1, (2, 20))
    s.add("BuilderUnit", 2, (39, 39))
    s.resources([(12, y) for y in range(0, 37)])
    s.order(unit, move((37, 20)))
    return s.model, unit


def arrival(memory: bool, seed: int, ticks: int) -> int | None:
    m, unit = wall(ticks)
    rng, mem = random.Random(seed), rules.FogMemory() if memory else None
    for t in range(ticks):
        rules.process_turn(m, rng, None, mem)
        if m.entities[unit].position == Vec2(37, 20):
            return t + 1
    return None


def test_a_wall_wider_than_sight_is_walked_around():
    """With the memory the builder learns the wall and goes round it; under the 2020 rules it walks to and fro."""
    for seed in range(1, 9):
        assert arrival(True, seed, 150) is not None, seed
        assert arrival(False, seed, 300) is None, seed


def test_the_rules_on_the_command_line(tmp_path: Path):
    m, unit = wall(150)
    (tmp_path / "state.json").write_text(json.dumps(model_json(m)))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"seed": 3, "game": {"LoadFrom": str(tmp_path / "state.json")},
                                  "players": [{"Empty": None}, {"Empty": None}]}))

    def final(*args: str) -> Vec2:
        out = tmp_path / f"replay{len(args)}.dat"
        r = subprocess.run([str(SIM), "--config", str(config), "--batch-mode", "--save-replay", str(out), *args],
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr
        *_, last = replay_io.load(out).states()
        return last.entities[unit].position

    assert final() == Vec2(37, 20) == final("--rules", "codecraft")
    assert final("--rules", "2020") != Vec2(37, 20)
    bad = subprocess.run([str(SIM), "--config", str(config), "--batch-mode", "--rules", "1999"],
                         capture_output=True, text=True, timeout=60)
    assert bad.returncode == 2
