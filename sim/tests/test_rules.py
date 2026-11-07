"""Our rules (codecraft_sim/rules.py) against the fixtures recorded from the official engine, turn by turn.

Each turn is played from the recorded state before it, with the recorded orders, under many seeds (more for a turn
with rare outcomes). Every outcome we produce must be one the official engine produces (the recorded one, or one of
the fixture's `alternatives`), and where it has several we must produce them all: the same rules, with our own
random draws.
"""
from __future__ import annotations

import json
import random

import pytest

from codecraft_sim import rules
from conftest import needs_corpus, replays
from oracle import fixture

FIXTURES = {fx["name"]: fx for fx in fixture.all_fixtures()}

# Fixtures whose turns we are known to play differently, and why (empty when the rules are complete).
KNOWN: dict[str, str] = {}


def our_outcomes(fx: dict, i: int, seeds) -> dict[str, tuple[int, dict]]:
    """Our outcomes of the turn after snapshot `i`: outcome key -> (the first seed giving it, the snapshot)."""
    out: dict[str, tuple[int, dict]] = {}
    for seed in seeds:
        m = fixture.model_at(fx, i)
        events = rules.process_turn(m, random.Random(seed))
        snap = fixture.snapshot(m, events)
        out.setdefault(fixture.outcome_key(snap), (seed, snap))
    return out


def describe(expected: dict, got: dict) -> str:
    """What differs between two snapshots, briefly."""
    lines = []
    for key in ("next_id", "players", "events"):
        if expected[key] != got[key]:
            lines.append(f"{key}: expected {expected[key]}, got {got[key]}")
    a = {e["id"]: e for e in expected["entities"]}
    b = {e["id"]: e for e in got["entities"]}
    for eid in sorted(set(a) | set(b)):
        if a.get(eid) != b.get(eid):
            lines.append(f"entity {eid}: expected {json.dumps(a.get(eid))}, got {json.dumps(b.get(eid))}")
    return "; ".join(lines[:6])


def enough_outcomes(fx: dict, i: int, admissible: dict) -> dict[str, tuple[int, dict]]:
    """Our outcomes over at least 40 seeds per official outcome, and more until each official outcome has come out
    (some are rare: one path in a hundred), up to MAX_SEEDS."""
    ours: dict[str, tuple[int, dict]] = {}
    start, step = 0, max(16, 40 * len(admissible))
    while start < MAX_SEEDS:
        for key, value in our_outcomes(fx, i, range(start, start + step)).items():
            ours.setdefault(key, value)
        start += step
        if set(admissible) <= set(ours):
            break
    return ours


MAX_SEEDS = 4000


def check(fx: dict) -> list[str]:
    problems = []
    ticks = fx["expected"]["ticks"]
    alternatives = fx["expected"].get("alternatives", {})
    if len(ticks) == 1 and not rules.game_over(fixture.model_at(fx, 0)):
        problems.append("the game should be over at the start")
    for i in range(len(ticks) - 1):
        tick = ticks[i + 1]["tick"]
        admissible = {fixture.outcome_key(s): s for s in alternatives.get(str(tick), [ticks[i + 1]])}
        ours = enough_outcomes(fx, i, admissible)
        for key, (seed, snap) in ours.items():
            if key not in admissible:
                problems.append(f"tick {tick} (seed {seed}): {describe(ticks[i + 1], snap)}")
        missing = len(set(admissible) - set(ours))
        if missing:
            problems.append(f"tick {tick}: {missing} of the official engine's {len(admissible)} outcomes never "
                            f"happen with us")
        m = fixture.model_at(fx, i)
        rules.process_turn(m, random.Random(0))
        if rules.game_over(m) != (i + 2 == len(ticks)):
            problems.append(f"tick {tick}: game_over is {rules.game_over(m)}")
    return problems


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_fixture(name):
    if name in KNOWN:
        pytest.xfail(KNOWN[name])
    problems = check(FIXTURES[name])
    assert not problems, "\n".join(problems[:12])


def test_distance():
    from codecraft_sim.model import Vec2
    assert rules.distance(Vec2(0, 0), 1, Vec2(3, 4), 1) == 7
    assert rules.distance(Vec2(0, 0), 2, Vec2(3, 4), 1) == 5       # from the 2x2 square's nearest cell (1, 1)
    assert rules.distance(Vec2(10, 10), 5, Vec2(12, 12), 1) == 0   # inside
    assert rules.distance(Vec2(10, 10), 5, Vec2(15, 12), 3) == 1   # side by side
    assert rules.distance(Vec2(10, 10), 5, Vec2(15, 15), 1) == 2   # corner to corner


def _exact(path, turns) -> dict:
    from codecraft_sim import replay as replay_io
    from oracle import corpus
    rep = replay_io.load(path)
    states = list(rep.states())
    differ = {}
    for t in turns:
        if t + 1 < len(states):
            diffs, notes = corpus.check_turn(states[t], states[t + 1], rep.ticks[t].events)
            if diffs:
                differ[states[t].current_tick] = [d for _, d in diffs[:3]] + notes[:3]
    return differ


@needs_corpus
def test_opening_turns_of_a_stored_game():
    """The first 30 turns of a stored official game (builds, mining, units walking out) come out exactly as recorded
    when our rules make the record's random choices (oracle/corpus.py)."""
    assert not _exact(replays()[0], range(30))


@needs_corpus
def test_turns_of_a_stored_fog_game():
    """Every 40th turn of the first stored game played with fog of war, where path searches see only what their
    player sees: exactly as recorded."""
    from codecraft_sim import replay as replay_io
    fog = next((p for p in replays() if replay_io.load(p).initial.fog_of_war), None)
    if fog is None:
        pytest.skip("no stored game with fog of war")
    assert not _exact(fog, range(0, 1000, 40))
