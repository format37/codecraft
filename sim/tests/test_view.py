"""Our views (codecraft_sim/view.py) against the views the official engine sent to recording puppets: those stored in
the fixtures, and, where the engine runs, those of stored official games played on from some of their states. Plus
the way back: the positions in a seat's actions."""
from __future__ import annotations

import pytest

from codecraft_sim import replay as replay_io
from codecraft_sim.model import action_json, view_json
from codecraft_sim.view import action_from_view, from_view, player_view, to_view
from conftest import needs_corpus, needs_engine, replays
from oracle import fixture, loadfrom
from oracle.puppet import Puppet

ALL = fixture.all_fixtures()
WITH_VIEWS = [fx for fx in ALL if any(p.get("views") for p in fx["expected"].get("puppets", {}).values())]


def player_of(model, seat: int) -> int:
    return next(p.id for p in model.players.values() if p.index == seat)


@pytest.mark.parametrize("fx", WITH_VIEWS, ids=[fx["name"] for fx in WITH_VIEWS])
def test_views_of_the_fixtures(fx):
    index = {t["tick"]: i for i, t in enumerate(fx["expected"]["ticks"])}
    start = fixture.state_from_json(fx["state"])         # the entities in the order of the file (`view-order`)
    for seat, p in fx["expected"]["puppets"].items():
        for v in p.get("views", []):
            tick = v["current_tick"]
            m = start if tick == start.current_tick else fixture.model_at(fx, index[tick])
            assert view_json(player_view(m, player_of(m, int(seat)))) == v, f"seat {seat}, tick {tick}"


def test_actions_are_turned_back():
    """Every order a puppet sent in its own frame for one of its player's entities is stored as `action_from_view`
    gives it (in the frame of the replay); a late or garbled answer is not applied, and is left out."""
    checked = 0
    for fx in ALL:
        ticks = fx["expected"]["ticks"]
        index = {t["tick"]: i for i, t in enumerate(ticks)}
        model = fixture.state_from_json(fx["state"])
        for seat, spec in enumerate(fx["players"]):
            if not isinstance(spec, dict):
                continue
            pup = Puppet.from_spec(spec)
            seen = fx["expected"]["puppets"][str(seat)]["view_ticks"]
            pid = player_of(model, seat)
            for tick, actions in pup.script.items():
                late = (tick in pup.sleep_at or tick == pup.garbage_at
                        or (pup.crash_at is not None and tick >= pup.crash_at))
                if tick not in seen or tick + 1 not in index or late:
                    continue
                owner = {e["id"]: e["player_id"] for e in ticks[index[tick]]["entities"]}
                stored = {a["entity_id"]: a["action"] for a in ticks[index[tick + 1]]["entity_actions"]}
                for eid, action in actions.items():
                    if owner.get(eid) == pid:
                        assert stored[eid] == action_json(action_from_view(seat, action, model)), (fx["name"], eid)
                        checked += 1
    assert checked >= 15


@pytest.mark.parametrize("n", [20, 80])
def test_frames(n):
    """`from_view` undoes `to_view` for every seat and size, the view of a square stays on the map, and each seat sees
    its own corner at (0, 0) (the corners of seats 0 to 3: (0, 0), (n-1, n-1), (n-1, 0), (0, n-1))."""
    for seat in range(4):
        for size in (1, 2, 3, 5):
            for x in range(0, n - size + 1):
                for y in range(0, n - size + 1, 7):
                    v = to_view(seat, (x, y), size, n)
                    assert 0 <= v.x <= n - size and 0 <= v.y <= n - size
                    assert from_view(seat, v, size, n) == (x, y)
    for seat, corner in enumerate([(0, 0), (n - 1, n - 1), (n - 1, 0), (0, n - 1)]):
        assert to_view(seat, corner, 1, n) == (0, 0)


def stored_fog_games() -> list:
    """The first stored game with fog of war and four seats (Round2), and the first with two (Finals)."""
    found = {}
    for path in replays():
        rep = replay_io.load(path)
        if rep.initial.fog_of_war and len(rep.initial.players) not in found:
            found[len(rep.initial.players)] = rep
        if len(found) == 2:
            break
    return list(found.values())


@needs_engine
@needs_corpus
def test_views_of_stored_games_on_the_official_engine():
    """Stored official games with fog of war, played on by the official engine for 3 ticks from two of their states
    with a recording puppet in every seat (empty answers: the stored orders go on): every view is ours of the
    engine's own state at that tick, property table included. (A wider probe compared 12 460 views this way: every
    seat, several states of each of the 92 distinct stored games, with and without fog.)"""
    games = stored_fog_games()
    assert games, "no stored game with fog of war"
    views = 0
    for rep in games:
        starts = {rep.tick_count // 3, 2 * rep.tick_count // 3}
        for m in rep.states():
            if m.current_tick not in starts:
                continue
            m = m.copy()
            m.max_tick_count = m.current_tick + 3
            run = loadfrom.run(m, [Puppet() for _ in m.players], timeout=5.0, accept_timeout=30)
            states = {s.current_tick: s for s in run.states}
            for seat, p in run.puppets.items():
                assert p.views and p.finished, f"seat {seat}: {p.error}"
                for v in p.views:
                    ours = player_view(states[v.current_tick], player_of(m, seat))
                    assert view_json(v) == view_json(ours), f"seat {seat}, tick {v.current_tick}"
                    assert v.entity_properties == ours.entity_properties
                    views += 1
    assert views >= 30
