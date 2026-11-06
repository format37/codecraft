"""The model's JSON form and the property table."""
from __future__ import annotations

import json

from codecraft_sim import replay as replay_io
from codecraft_sim.model import (DEFAULT_PROPERTIES, EntityType, model_from_json, model_json, properties_from_json,
                                 properties_json)
from oracle.state import State, attack, build, combine, move, repair

from conftest import needs_corpus, replays


@needs_corpus
def test_default_properties_are_the_engines():
    """The table typed in from docs/doc-en.md equals the map the official engine wrote into a replay."""
    for path in replays()[:3]:
        assert replay_io.load(path).initial.entity_properties == DEFAULT_PROPERTIES     # dict equality: any order


def test_properties_json_uses_range_for_the_attack_range():
    d = properties_json(DEFAULT_PROPERTIES[EntityType.RANGED_UNIT])
    assert d["attack"] == {"range": 5, "damage": 5, "collect_resource": False}
    assert properties_from_json(d) == DEFAULT_PROPERTIES[EntityType.RANGED_UNIT]


def test_model_json_round_trip():
    s = State(map_size=30, players=3, ticks=7, fog=True, resource=[1, 2, 3], score=[4, 5, 6], passive_income=2,
              current_tick=40)
    a = s.add("BuilderUnit", 1, (1, 2))
    b = s.add("MeleeBase", 2, (10, 10), health=17, active=False)
    r = s.add("Resource", None, (5, 5))
    s.order(a, combine(move((3, 4), closest=False, break_through=True), attack(r, 10, ["Resource", "Wall"]),
                       build("House", (2, 2)), repair(b)))
    s.order(b, build("MeleeUnit", (15, 10)))
    d = model_json(s.model)
    again = model_from_json(json.loads(json.dumps(d)))
    assert again == s.model
    assert d["entities"]["by_id"][str(b)] == {"id": b, "player_id": 2, "entity_type": "MeleeBase",
                                              "position": {"x": 10, "y": 10}, "health": 17, "active": False}
    assert d["max_tick_count"] == 47


def test_entity_type_parse():
    assert EntityType.parse("BuilderUnit") is EntityType.BUILDER_UNIT
    assert EntityType.parse("BUILDER_UNIT") is EntityType.BUILDER_UNIT
    assert EntityType.parse(9) is EntityType.TURRET
    assert EntityType.TURRET.json_name == "Turret"
