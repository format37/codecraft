"""The starting states of `Create` games (codecraft_sim/mapgen.py): the presets' players and starting entities are
the official ones, id for id; our resources are the same under the eight symmetries of the square (so every seat
sees the same field from its corner), keep the corners free, and join them."""
from __future__ import annotations

import json
import random
from collections import deque

import pytest

from codecraft_sim import mapgen
from codecraft_sim import replay as replay_io
from codecraft_sim.model import DEFAULT_PROPERTIES, EntityType, Vec2
from codecraft_sim.view import to_view

from conftest import GAMES, needs_corpus

T = EntityType


def official_start(fmt: str):
    for path in sorted(GAMES.glob("*/replay.dat")):
        if json.loads((path.parent / "config.json").read_text())["game"] == {"Create": fmt}:
            return replay_io.load(path).initial
    pytest.skip(f"no stored {fmt} game")


def placed(m) -> list[tuple]:
    return sorted((e.id, e.player_id, e.entity_type, e.position, e.health, e.active)
                  for e in m.entities.values() if e.entity_type != T.RESOURCE)


@needs_corpus
@pytest.mark.parametrize("fmt", ["Round1", "Round2", "Finals"])
def test_presets_start_as_the_official_ones(fmt):
    off = official_start(fmt)
    ours = mapgen.create(mapgen.preset(fmt), len(off.players), random.Random(1))
    assert placed(ours) == placed(off)
    assert sorted(map(vars, ours.players.values()), key=str) == sorted(map(vars, off.players.values()), key=str)
    for key in ("map_size", "fog_of_war", "max_tick_count", "max_pathfind_nodes", "current_tick", "passive_income"):
        assert getattr(ours, key) == getattr(off, key), key
    assert ours.entity_properties == off.entity_properties
    first = min(e.id for e in ours.entities.values() if e.entity_type == T.RESOURCE)
    assert first == min(e.id for e in off.entities.values() if e.entity_type == T.RESOURCE)


def test_custom_as_the_official_engine_reads_it():
    """A Custom game of the official engine (30 x 30, three seats; probe of step 6): the same players and entities."""
    o = mapgen.options_from_json({"map_size": 30, "initial_resource": 7, "passive_income": 1, "max_pathfind_nodes": 100,
                                  "fog_of_war": False, "max_tick_count": 3,
                                  "starting_entities": [["BuilderBase", {"x": 5, "y": 5}],
                                                        ["BuilderUnit", {"x": 4, "y": 4}]]})
    m = mapgen.create(o, 3, random.Random(3))
    players = [(p.id, p.index, p.score, p.resource) for p in m.players.values()]
    assert players == [(1, 0, 0, 7), (2, 1, 0, 7), (3, 2, 0, 7)]
    assert [(e.id, e.player_id, e.entity_type, tuple(e.position)) for e in m.entities.values()][:6] == [
        (4, 1, T.BUILDER_BASE, (5, 5)), (5, 1, T.BUILDER_UNIT, (4, 4)), (6, 2, T.BUILDER_BASE, (20, 20)),
        (7, 2, T.BUILDER_UNIT, (25, 25)), (8, 3, T.BUILDER_BASE, (20, 5)), (9, 3, T.BUILDER_UNIT, (25, 4))]
    assert (m.map_size, m.max_tick_count, m.max_pathfind_nodes, m.passive_income) == (30, 3, 100, 1)
    assert m.next_id == 10 + sum(e.entity_type == T.RESOURCE for e in m.entities.values())
    with pytest.raises(ValueError, match="map_size"):
        mapgen.options_from_json({})
    assert mapgen.options_from_json({**{k: 0 for k in ("map_size", "initial_resource", "passive_income",
                                                         "max_pathfind_nodes")}, "fog_of_war": True,
                                     "starting_entities": []}).max_tick_count == 1000


def resources(m) -> set[tuple[int, int]]:
    return {(e.position.x, e.position.y) for e in m.entities.values() if e.entity_type == T.RESOURCE}


def joined(m) -> bool:
    """Whether every builder can walk to every other one's side over free cells."""
    n, taken = m.map_size, set()
    for e in m.entities.values():
        s = DEFAULT_PROPERTIES[e.entity_type].size
        taken.update((e.position.x + dx, e.position.y + dy) for dx in range(s) for dy in range(s))
    units = [e.position for e in m.entities.values() if e.entity_type == T.BUILDER_UNIT]
    start = (units[0].x + 1, units[0].y)
    seen, todo = {start}, deque([start])
    while todo:
        x, y = todo.popleft()
        for c in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= c[0] < n and 0 <= c[1] < n and c not in seen and c not in taken:
                seen.add(c)
                todo.append(c)
    return all(any((u.x + dx, u.y + dy) in seen for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))) for u in units)


@pytest.mark.parametrize("fmt", ["Round1", "Round2", "Finals"])
def test_our_maps(fmt):
    seats = 2 if fmt == "Finals" else 4
    box = mapgen.seat0_box(mapgen.preset(fmt))
    shares, maps = [], set()
    for seed in range(1, 31):
        m = mapgen.create(mapgen.preset(fmt), seats, random.Random(seed))
        cells = resources(m)
        n = m.map_size
        assert all((n - 1 - x, y) in cells and (x, n - 1 - y) in cells and (y, x) in cells for x, y in cells), \
            "both mirrors and the diagonal"
        x0, y0, x1, y1 = box
        for x, y in cells:
            assert not any(x0 <= a <= x1 and y0 <= b <= y1 for a, b in mapgen.images(x, y, n)), \
                f"seed {seed}: a resource at {(x, y)} in a corner's zone"
        for seat in range(seats):
            assert {tuple(to_view(seat, c, 1, n)) for c in cells} == cells, "every seat sees the same field"
        res = sorted((e for e in m.entities.values() if e.entity_type == T.RESOURCE), key=lambda e: e.id)
        assert [tuple(e.position) for e in res] == sorted(cells), "resources numbered by x, then y"
        assert all(e.health == 30 and e.active and e.player_id is None for e in res)
        assert m.next_id == max(m.entities) + 1
        assert joined(m), f"seed {seed}: the corners are not joined"
        shares.append(len(cells) / (n * n))
        maps.add(frozenset(cells))
    assert len(maps) == 30, "every seed its own map"
    assert 0.2 < min(shares) and max(shares) < 0.55 and 0.3 < sum(shares) / len(shares) < 0.45


def test_same_seed_same_map_whatever_the_seats():
    a = mapgen.create(mapgen.preset("Round2"), 4, random.Random(11))
    b = mapgen.create(mapgen.preset("Round2"), 4, random.Random(11))
    assert placed(a) == placed(b) and resources(a) == resources(b)
    finals = mapgen.create(mapgen.preset("Finals"), 2, random.Random(11))
    assert resources(finals) == resources(a), "the map does not depend on the number of seats"


def test_place():
    """Seat 0's square turned for each seat, as the official Round1 places them; the inverse of the seat's view."""
    for seat, want in enumerate([(5, 15), (70, 60), (60, 5), (15, 70)]):     # the MeleeBase
        assert mapgen.place(Vec2(5, 15), 5, 80, seat) == want
        assert to_view(seat, want, 5, 80) == (5, 15)
    assert mapgen.place(Vec2(15, 15), 2, 80, 1) == (63, 63)            # the Turret of seat 1


@pytest.mark.parametrize("fmt", ["Round1", "Round2", "Finals"])
def test_every_seat_sees_the_same_start(fmt):
    """In its own frame every seat is sent the same first view, up to the ids: its entities, the resources, and the
    other players' (the one across the diagonal and those beside it), fog of war or not."""
    from codecraft_sim.view import player_view
    seats = 2 if fmt == "Finals" else 4
    m = mapgen.create(mapgen.preset(fmt), seats, random.Random(7))
    across = {0: 1, 1: 0, 2: 3, 3: 2}
    seat_of = {p.id: p.index for p in m.players.values()}
    views = []
    for p in m.players.values():
        def who(e):
            if e.player_id is None:
                return "resource"
            if e.player_id == p.id:
                return "own"
            return "across" if seat_of[e.player_id] == across[p.index] else "beside"
        views.append(sorted((who(e), e.entity_type, tuple(e.position), e.health, e.active)
                            for e in player_view(m, p.id).entities))
    assert all(v == views[0] for v in views)
