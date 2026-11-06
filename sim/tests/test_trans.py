"""The wire codec against the official Python client (clients/Python, generated from the engine's own schema):
what we write, the client reads back to the same values, and the other way round."""
from __future__ import annotations

import importlib.util
import io
import sys

import pytest

from codecraft_sim import trans
from codecraft_sim.model import (DEFAULT_PROPERTIES, AttackAction, AutoAttack, BuildAction, Entity, EntityAction,
                                 EntityType, MoveAction, PlayerView, RepairAction, Vec2, ViewPlayer)

from conftest import ROOT

CLIENT = ROOT / "clients" / "Python"


def _load(name: str, path, package: bool = False):
    spec = importlib.util.spec_from_file_location(
        name, path / "__init__.py" if package else path, submodule_search_locations=[str(path)] if package else None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


official = _load("official_model", CLIENT / "model", package=True)
StreamWrapper = _load("official_stream_wrapper", CLIENT / "stream_wrapper.py").StreamWrapper


def sample_view() -> PlayerView:
    T = EntityType
    return PlayerView(
        my_id=2, map_size=80, fog_of_war=True, entity_properties=dict(DEFAULT_PROPERTIES), max_tick_count=1000,
        max_pathfind_nodes=1000, current_tick=17,
        players=[ViewPlayer(1, 120, 35), ViewPlayer(2, 7, 0), ViewPlayer(3, -1, 2 ** 31 - 1)],
        entities=[Entity(5, 2, T.BUILDER_BASE, Vec2(70, 70), 300, True),
                  Entity(9, None, T.RESOURCE, Vec2(0, 79), 30, True),
                  Entity(2000, 1, T.HOUSE, Vec2(11, 12), 5, False)])


def sample_actions() -> dict[int, EntityAction]:
    T = EntityType
    return {
        5: EntityAction(build_action=BuildAction(T.BUILDER_UNIT, Vec2(75, 69))),
        6: EntityAction(move_action=MoveAction(Vec2(40, 41), False, True),
                        attack_action=AttackAction(None, AutoAttack(20, (T.RESOURCE, T.WALL)))),
        7: EntityAction(attack_action=AttackAction(1234, None), repair_action=RepairAction(5)),
        8: EntityAction(),
    }


def official_view(v: PlayerView):
    o = official
    props = {}
    for t, p in v.entity_properties.items():
        props[o.EntityType(int(t))] = o.EntityProperties(
            p.size, p.build_score, p.destroy_score, p.can_move, p.population_provide, p.population_use, p.max_health,
            p.initial_cost, p.sight_range, p.resource_per_health,
            None if p.build is None else o.BuildProperties([o.EntityType(int(x)) for x in p.build.options],
                                                           p.build.init_health),
            None if p.attack is None else o.AttackProperties(p.attack.attack_range, p.attack.damage,
                                                             p.attack.collect_resource),
            None if p.repair is None else o.RepairProperties([o.EntityType(int(x)) for x in p.repair.valid_targets],
                                                             p.repair.power))
    return o.PlayerView(v.my_id, v.map_size, v.fog_of_war, props, v.max_tick_count, v.max_pathfind_nodes,
                        v.current_tick, [o.Player(p.id, p.score, p.resource) for p in v.players],
                        [o.Entity(e.id, e.player_id, o.EntityType(int(e.entity_type)), o.Vec2Int(*e.position),
                                  e.health, e.active) for e in v.entities])


def official_action(a: EntityAction):
    o = official
    m, b, at, r = a.move_action, a.build_action, a.attack_action, a.repair_action
    return o.EntityAction(
        None if m is None else o.MoveAction(o.Vec2Int(*m.target), m.find_closest_position, m.break_through),
        None if b is None else o.BuildAction(o.EntityType(int(b.entity_type)), o.Vec2Int(*b.position)),
        None if at is None else o.AttackAction(at.target, None if at.auto_attack is None else o.AutoAttack(
            at.auto_attack.pathfind_range, [o.EntityType(int(t)) for t in at.auto_attack.valid_targets])),
        None if r is None else o.RepairAction(r.target))


def official_bytes(write) -> bytes:
    buf = io.BytesIO()
    write(StreamWrapper(buf))
    return buf.getvalue()


def official_read(data: bytes, read):
    return read(StreamWrapper(io.BytesIO(data)))


def test_get_action_matches_the_official_client_both_ways():
    view = sample_view()
    w = trans.Writer()
    trans.write_get_action(w, view)
    theirs = official_bytes(official.ServerMessage.GetAction(official_view(view), False).write_to)
    assert w.getvalue() == theirs
    msg = official_read(w.getvalue(), official.ServerMessage.read_from)
    assert msg.player_view.current_tick == 17 and len(msg.player_view.entities) == 3
    kind, back, debug = trans.read_server_message(trans.Reader(theirs))
    assert (kind, debug) == ("get_action", False)
    assert back == view


def test_action_message_matches_the_official_client_both_ways():
    actions = sample_actions()
    w = trans.Writer()
    trans.write_action_message(w, actions)
    theirs = official_bytes(official.ClientMessage.ActionMessage(
        official.Action({k: official_action(a) for k, a in actions.items()})).write_to)
    assert w.getvalue() == theirs
    assert trans.read_client_message(trans.Reader(theirs)) == actions
    msg = official_read(w.getvalue(), official.ClientMessage.read_from)
    assert sorted(msg.action.entity_actions) == [5, 6, 7, 8]


def test_finish_and_token():
    w = trans.Writer()
    trans.write_finish(w)
    assert w.getvalue() == official_bytes(official.ServerMessage.Finish().write_to)
    w = trans.Writer()
    w.string("0123abcd")
    assert w.getvalue() == official_bytes(lambda s: s.write_string("0123abcd"))
    assert trans.Reader(w.getvalue()).string() == "0123abcd"


def test_stream_reader_reads_what_the_writer_wrote():
    actions = sample_actions()
    w = trans.Writer()
    trans.write_action_message(w, actions)
    assert trans.read_client_message(trans.StreamReader(io.BytesIO(w.getvalue()))) == actions
    with pytest.raises(EOFError):
        trans.read_client_message(trans.StreamReader(io.BytesIO(w.getvalue()[:-3])))


@pytest.mark.parametrize("data, error", [
    (b"\x00\x00\x00\x00", "unexpected client message tag 0"),          # DebugMessage: not in batch mode
    (b"\x4d\x00\x00\x00", "unexpected client message tag 77"),
    (b"\x01\x00\x00\x00\xff\xff\xff\xff", "bad count -1"),
    (b"\x01\x00\x00\x00\x01\x00\x00\x00\x05\x00\x00\x00\x02", "bad bool 2"),
])
def test_bad_client_messages_are_protocol_errors(data, error):
    with pytest.raises(trans.ProtocolError, match=error):
        trans.read_client_message(trans.Reader(data))


def test_bad_entity_type_is_a_protocol_error():
    w = trans.Writer()
    w.i32(trans.ACTION_MESSAGE)
    w.i32(1)
    w.i32(5)
    w.bool(False)
    w.bool(True)          # build_action
    w.i32(10)             # no such EntityType
    with pytest.raises(trans.ProtocolError, match="bad entity type 10"):
        trans.read_client_message(trans.Reader(w.getvalue() + b"\x00" * 8))
