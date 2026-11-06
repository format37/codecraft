"""The fixture files: well formed, one per scenario, and (where the official engine runs) still what the engine
does. The Python engine's own fixture tests come with rules.py (step 3 of the kickoff)."""
from __future__ import annotations

import pytest

from oracle import fixture as fixture_io
from oracle.puppet import Puppet
from oracle.scenarios import SCENARIOS

from conftest import needs_engine

FIXTURES = fixture_io.all_fixtures()
NAMES = [fx["name"] for fx in FIXTURES]


def test_one_fixture_per_scenario():
    assert sorted(NAMES) == sorted(SCENARIOS)
    for fx in FIXTURES:
        assert fixture_io.path_of(fx["name"]).exists()


@pytest.mark.parametrize("fx", FIXTURES, ids=NAMES)
def test_fixture_is_well_formed(fx):
    assert set(fx) >= {"name", "about", "questions", "engine", "seed", "players", "state", "expected"}
    model = fixture_io.state_from_json(fx["state"])
    assert len(fx["players"]) == len(model.players)
    for p in fx["players"]:
        if isinstance(p, dict):
            Puppet.from_spec(p)          # parses
        else:
            assert p in ("Empty", "QuickStart")
    exp = fx["expected"]
    ticks = exp["ticks"]
    assert [t["tick"] for t in ticks] == list(range(model.current_tick, model.current_tick + len(ticks)))
    assert len(ticks) <= model.max_tick_count - model.current_tick + 1
    first = dict(ticks[0])
    assert first.pop("events") == []
    loaded = fixture_io.snapshot(model, [])
    loaded.pop("events")
    assert first == loaded, "the engine's first state is the state the fixture gives it"
    if exp["results"] is not None:
        assert len(exp["results"]["results"]) == len(model.players)
        final = {p["index"]: p["score"] for p in ticks[-1]["players"]}
        assert exp["results"]["results"] == [final[i] for i in range(len(model.players))]


@needs_engine
@pytest.mark.parametrize("name", NAMES)
def test_fixture_still_matches_the_official_engine(name):
    fresh = fixture_io.record(SCENARIOS[name])
    assert fixture_io.compare(fixture_io.load(name), fresh) == []
