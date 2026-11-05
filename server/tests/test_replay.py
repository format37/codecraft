"""The replay decoder, checked against a tiny replay produced by an encoder of the same format."""
import struct

import pytest

from codewars import replay as R


class W:
    def __init__(self):
        self.buf = bytearray()

    def i32(self, v):
        self.buf += struct.pack("<i", v)

    def bool(self, v):
        self.buf.append(1 if v else 0)

    def opt(self, v, f):
        self.bool(v is not None)
        if v is not None:
            f(v)

    def vec(self, items, f):
        self.i32(len(items))
        for it in items:
            f(it)


def props(w, size, max_health):
    w.i32(size); w.i32(0); w.i32(0); w.bool(size == 1); w.i32(0); w.i32(0); w.i32(max_health); w.i32(0)
    w.i32(5); w.i32(0)
    w.opt(None, None); w.opt(None, None); w.opt(None, None)


def player(w, p):
    for v in p:
        w.i32(v)


def entity(w, e):
    w.i32(e[0]); w.opt(e[1], w.i32); w.i32(e[2]); w.i32(e[3]); w.i32(e[4]); w.i32(e[5]); w.bool(e[6])


def action(w, a):
    w.i32(a["entity_id"])
    w.opt(a.get("move"), lambda m: (w.i32(m["target"][0]), w.i32(m["target"][1]), w.bool(m["find_closest_position"]),
                                    w.bool(m["break_through"])))
    w.opt(a.get("build"), lambda b: (w.i32(b["entity_type"]), w.i32(b["position"][0]), w.i32(b["position"][1])))
    w.opt(a.get("attack"), lambda at: (w.opt(at["target"], w.i32),
                                       w.opt(at["auto_attack"], lambda aa: (w.i32(aa["pathfind_range"]),
                                                                            w.vec(aa["valid_targets"], w.i32)))))
    w.opt(a.get("repair"), lambda r: w.i32(r["target"]))


def collection(w, items, f):
    w.i32(len(items))
    for k, v in items:
        w.i32(k)
        f(w, v)


def delta(w, added, removed, updated, f):
    w.vec(added, lambda v: f(w, v))
    w.vec(removed, w.i32)
    w.i32(len(updated))
    for k, v in updated:
        w.i32(k)
        f(w, v)


def tick(w, t, next_id, events, players, entities, actions):
    w.vec(events, lambda e: (w.i32(e[0]), w.i32(e[1]), w.i32(e[2])))
    w.i32(next_id); w.i32(10); w.bool(False); w.i32(100); w.i32(1000); w.i32(t); w.i32(0)
    delta(w, *players, player)
    delta(w, *entities, entity)
    delta(w, *actions, action)


def make_replay():
    w = W()
    # Model: next_id, map_size, fog, properties, max_tick, max_pathfind, current_tick, passive_income
    w.i32(5); w.i32(10); w.bool(False)
    w.i32(2); w.i32(3); props(w, 1, 10); w.i32(8); props(w, 1, 30)   # BUILDER_UNIT, RESOURCE
    w.i32(100); w.i32(1000); w.i32(0); w.i32(0)
    collection(w, [(1, [1, 0, 0, 0]), (2, [2, 1, 0, 0])], player)
    collection(w, [(3, [3, 1, 3, 0, 0, 10, True]), (4, [4, None, 8, 5, 5, 30, True])], entity)
    collection(w, [], action)
    # tick 1: builder 3 moves, attacks resource 4; resource loses health
    tick(w, 1, 5, [[0, 3, 4]], ([], [], []),
         ([], [], [(3, [3, 1, 3, 1, 0, 10, True]), (4, [4, None, 8, 5, 5, 29, True])]),
         ([{"entity_id": 3, "move": {"target": [5, 5], "find_closest_position": True, "break_through": False},
            "attack": {"target": None, "auto_attack": {"pathfind_range": 10, "valid_targets": [8]}}}], [], []))
    # tick 2: a new unit appears, the resource is gone, player 1 scores
    tick(w, 2, 6, [], ([], [], [(1, [1, 0, 12, 3])]),
         ([[5, 2, 3, 9, 9, 10, True]], [4], []),
         ([], [3], []))
    return bytes(w.buf)


def test_decode_roundtrip():
    rep = R.parse(make_replay())
    assert rep.initial.map_size == 10 and rep.tick_count == 2 and len(rep.ticks) == 2
    import copy
    t0, t1, t2 = [copy.deepcopy(st) for st in rep.states()]
    assert t0[0] == 0 and sorted(t0[2]) == [3, 4]
    assert t1[2][3][3] == 1 and t1[2][4][5] == 29 and t1[3][3]["move"]["target"] == [5, 5]
    assert rep.ticks[0].events == [[0, 3, 4]]
    assert sorted(t2[2]) == [3, 5] and t2[1][1][2] == 12 and 3 not in t2[3]
    assert R.final_scores(rep) == [12, 0]
    j = R.to_json(rep)
    assert j["ticks"][0]["ev"] == [[0, 3, 4]] and j["ticks"][1]["r"] == [4] and j["ticks"][1]["a"][0][0] == 5
    assert j["properties"]["RESOURCE"]["max_health"] == 30


def test_truncated_and_garbage_replays_raise():
    data = make_replay()
    with pytest.raises(R.ReplayError):
        R.parse(data[:-7])
    with pytest.raises(R.ReplayError):
        R.parse(data + b"\xff" * 40)
