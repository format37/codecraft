"""The replay decoder, checked against a tiny replay produced by an encoder of the same format."""
import struct

import pytest

from codecraft import replay as R


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


def props_full(w, size, max_health, provide=0, use=0, damage=None, destroy=0):
    w.i32(size); w.i32(0); w.i32(destroy); w.bool(size == 1); w.i32(provide); w.i32(use); w.i32(max_health); w.i32(0)
    w.i32(5); w.i32(0)
    w.opt(None, None)
    w.opt(None if damage is None else damage, lambda d: (w.i32(1), w.i32(d), w.bool(False)))
    w.opt(None, None)


def make_battle_replay(last_tick=25):
    """Two players. Slot 0 (player 1): a builder base and a melee unit. Slot 1 (player 2): a builder base,
    a builder and an unfinished house. The melee unit takes the base down in two hits (ticks 1-2) while
    slot 0's base produces a second melee unit (tick 2); at tick 3 the two melee units kill the builder
    and the house, and slot 1 is out. Then nothing happens until `last_tick`."""
    w = W()
    w.i32(30); w.i32(10); w.bool(False)
    w.i32(4)
    w.i32(1); props_full(w, 3, 5, provide=5, destroy=500)                       # HOUSE
    w.i32(2); props_full(w, 5, 20, provide=5, destroy=5000)                     # BUILDER_BASE
    w.i32(3); props_full(w, 1, 5, use=1, damage=1, destroy=100)                 # BUILDER_UNIT
    w.i32(5); props_full(w, 1, 10, use=1, damage=5, destroy=200)                # MELEE_UNIT
    w.i32(1000); w.i32(1000); w.i32(0); w.i32(0)
    collection(w, [(1, [1, 0, 0, 0]), (2, [2, 1, 0, 0])], player)
    collection(w, [(10, [10, 1, 2, 0, 0, 20, True]), (11, [11, 1, 5, 5, 5, 10, True]),
                   (20, [20, 2, 2, 5, 0, 10, True]), (21, [21, 2, 3, 9, 9, 5, True]),
                   (22, [22, 2, 1, 7, 7, 3, False])], entity)
    collection(w, [], action)
    tick(w, 1, 23, [[0, 11, 20]], ([], [], []), ([], [], [(20, [20, 2, 2, 5, 0, 5, True])]), ([], [], []))
    tick(w, 2, 24, [[0, 11, 20]], ([], [], [(1, [1, 0, 5000, 0])]),
         ([[12, 1, 5, 6, 6, 10, True]], [20], []), ([], [], []))
    tick(w, 3, 24, [[0, 11, 21], [0, 12, 22]], ([], [], [(1, [1, 0, 5600, 0])]), ([], [21, 22], []), ([], [], []))
    for t in range(4, last_tick + 1):
        tick(w, t, 24, [], ([], [], []), ([], [], []), ([], [], []))
    return bytes(w.buf)


def test_timeline_samples_counts_and_who_destroyed_what():
    tl = R.timeline(R.parse(make_battle_replay()), every=3)       # clamped to 10
    assert tl["every"] == 10 and tl["tick_count"] == 25 and tl["map_size"] == 10
    assert [s["tick"] for s in tl["samples"]] == [0, 10, 20, 25]
    start = {r["slot"]: r for r in tl["samples"][0]["seats"]}
    assert start[0]["units"] == {"MELEE_UNIT": 1} and start[0]["buildings"] == {"BUILDER_BASE": 1}
    assert start[1]["population"] == [1, 5]                     # the unfinished house provides nothing
    assert start[1]["buildings"] == {"BUILDER_BASE": 1} and "lost" not in start[1]
    s10 = {r["slot"]: r for r in tl["samples"][1]["seats"]}
    assert s10[0]["score"] == 5600 and s10[0]["units"] == {"MELEE_UNIT": 2} and s10[0]["population"] == [2, 5]
    assert s10[0]["built"] == {"MELEE_UNIT": 1}
    assert s10[0]["killed"] == {"BUILDER_BASE": 1, "BUILDER_UNIT": 1, "HOUSE": 1}
    assert s10[1]["lost"] == {"BUILDER_BASE": 1, "BUILDER_UNIT": 1, "HOUSE": 1}
    assert s10[1]["units"] == {} and s10[1]["buildings"] == {}
    assert "built" not in tl["samples"][2]["seats"][0]            # the window starts afresh
    assert tl["events"] == [
        {"tick": 2, "event": "base_destroyed", "slot": 1, "base": "BUILDER_BASE", "by": 0},
        {"tick": 3, "event": "eliminated", "slot": 1, "by": 0}]


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
