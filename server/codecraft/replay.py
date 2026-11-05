"""Decoder for the official replay format of the CodeCraft 2020 engine (`aicup2020 --save-replay`).

The file is the engine's `trans` encoding (little-endian i32, bool = 1 byte, Option = tag byte + value,
Vec/map = i32 count + items):

    Model                      -- the full initial state (tick 0)
    repeat for every tick:
        Vec<Event>             -- the tick's attacks: (tag 0, attacker id, target id), mining included
        ModelDelta             -- batbox `Diff` of the model against the previous tick

`Model` = next_id, map_size, fog_of_war, entity_properties (map EntityType -> EntityProperties),
max_tick_count, max_pathfind_nodes, current_tick, passive_income, players, entities, entity_actions.
The three collections are maps id -> value. `ModelDelta` repeats the scalar fields (the properties
map is skipped) and carries a `CollectionDelta` per collection: added values, removed ids and
updated (id, value) pairs. See docs/replay-format.md.
"""
from __future__ import annotations

import json
import struct
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ENTITY_TYPES = ["WALL", "HOUSE", "BUILDER_BASE", "BUILDER_UNIT", "MELEE_BASE", "MELEE_UNIT",
                "RANGED_BASE", "RANGED_UNIT", "RESOURCE", "TURRET"]


class ReplayError(ValueError):
    pass


class Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def i32(self) -> int:
        try:
            v = struct.unpack_from("<i", self.data, self.pos)[0]
        except struct.error as exc:
            raise ReplayError(f"truncated at {self.pos}") from exc
        self.pos += 4
        return v

    def bool(self) -> bool:
        if self.pos >= len(self.data):
            raise ReplayError(f"truncated at {self.pos}")
        v = self.data[self.pos]
        if v > 1:
            raise ReplayError(f"bad bool {v} at {self.pos}")
        self.pos += 1
        return v == 1

    def opt(self, f):
        return f() if self.bool() else None

    def count(self) -> int:
        n = self.i32()
        if n < 0 or n > 10_000_000:
            raise ReplayError(f"bad count {n} at {self.pos - 4}")
        return n

    def vec(self, f) -> list:
        return [f() for _ in range(self.count())]

    def map(self, f) -> dict:
        out = {}
        for _ in range(self.count()):
            k = self.i32()
            out[k] = f()
        return out

    @property
    def eof(self) -> bool:
        return self.pos >= len(self.data)


# Model pieces -------------------------------------------------------------------------------
def read_properties(r: Reader) -> dict:
    p = dict(size=r.i32(), build_score=r.i32(), destroy_score=r.i32(), can_move=r.bool(),
             population_provide=r.i32(), population_use=r.i32(), max_health=r.i32(), initial_cost=r.i32(),
             sight_range=r.i32(), resource_per_health=r.i32())
    p["build"] = r.opt(lambda: dict(options=r.vec(r.i32), init_health=r.opt(r.i32)))
    p["attack"] = r.opt(lambda: dict(attack_range=r.i32(), damage=r.i32(), collect_resource=r.bool()))
    p["repair"] = r.opt(lambda: dict(valid_targets=r.vec(r.i32), power=r.i32()))
    return p


def read_player(r: Reader) -> list:
    # id, index (seat, 0-based), score, resource
    return [r.i32(), r.i32(), r.i32(), r.i32()]


def read_entity(r: Reader) -> list:
    # id, player_id (None for resources), entity_type, x, y, health, active
    return [r.i32(), r.opt(r.i32), r.i32(), r.i32(), r.i32(), r.i32(), r.bool()]


def read_action(r: Reader) -> dict:
    a: dict = {}
    move = r.opt(lambda: dict(target=[r.i32(), r.i32()], find_closest_position=r.bool(), break_through=r.bool()))
    build = r.opt(lambda: dict(entity_type=r.i32(), position=[r.i32(), r.i32()]))
    attack = r.opt(lambda: dict(target=r.opt(r.i32),
                                auto_attack=r.opt(lambda: dict(pathfind_range=r.i32(), valid_targets=r.vec(r.i32)))))
    repair = r.opt(lambda: dict(target=r.i32()))
    if move:
        a["move"] = move
    if build:
        a["build"] = build
    if attack:
        a["attack"] = attack
    if repair:
        a["repair"] = repair
    return a


def read_stored_action(r: Reader) -> dict:
    entity_id = r.i32()
    action = read_action(r)
    action["entity_id"] = entity_id
    return action


@dataclass
class CollectionDelta:
    removed: list
    added: list
    updated: list   # (id, value) pairs


def read_collection_delta(r: Reader, f) -> CollectionDelta:
    added = r.vec(f)
    removed = r.vec(r.i32)
    updated = [(r.i32(), f()) for _ in range(r.count())]
    return CollectionDelta(removed, added, updated)


@dataclass
class Model:
    next_id: int
    map_size: int
    fog_of_war: bool
    entity_properties: dict
    max_tick_count: int
    max_pathfind_nodes: int
    current_tick: int
    passive_income: int
    players: dict           # id -> [id, index, score, resource]
    entities: dict          # id -> [id, player_id, type, x, y, health, active]
    entity_actions: dict    # entity id -> action dict


@dataclass
class TickDelta:
    next_id: int
    current_tick: int
    passive_income: int
    players: CollectionDelta
    entities: CollectionDelta
    entity_actions: CollectionDelta
    events: list = field(default_factory=list)


def read_model(r: Reader) -> Model:
    next_id = r.i32()
    map_size = r.i32()
    fog = r.bool()
    props = r.map(lambda: read_properties(r))
    max_tick = r.i32()
    max_pf = r.i32()
    tick = r.i32()
    passive = r.i32()
    players = r.map(lambda: read_player(r))
    entities = r.map(lambda: read_entity(r))
    actions = r.map(lambda: read_stored_action(r))
    return Model(next_id, map_size, fog, props, max_tick, max_pf, tick, passive, players, entities, actions)


def read_event(r: Reader) -> list:
    # tag, then two ids; tag 0 = attack (attacker, target) - seen so far only with payload of two ints
    tag = r.i32()
    if tag not in KNOWN_EVENT_TAGS:
        raise ReplayError(f"unknown event tag {tag} at {r.pos - 4}")
    return [tag, r.i32(), r.i32()]


KNOWN_EVENT_TAGS = {0, 1, 2, 3}


def read_tick(r: Reader, map_size: int) -> TickDelta:
    events = r.vec(lambda: read_event(r))
    next_id = r.i32()
    if r.i32() != map_size:  # map_size is repeated in every delta: a cheap sync check
        raise ReplayError(f"lost sync at {r.pos - 4}")
    r.bool()       # fog_of_war
    r.i32()        # max_tick_count
    r.i32()        # max_pathfind_nodes
    tick = r.i32()
    passive = r.i32()
    players = read_collection_delta(r, lambda: read_player(r))
    entities = read_collection_delta(r, lambda: read_entity(r))
    actions = read_collection_delta(r, lambda: read_stored_action(r))
    return TickDelta(next_id, tick, passive, players, entities, actions, events)


# Public API ---------------------------------------------------------------------------------
@dataclass
class Replay:
    initial: Model
    ticks: list[TickDelta]

    @property
    def tick_count(self) -> int:
        return self.ticks[-1].current_tick if self.ticks else self.initial.current_tick

    def states(self):
        """Yield (tick, players, entities, actions) for every tick, applying the deltas in place.
        The dicts are shared between iterations: copy them if you keep them."""
        players = {k: list(v) for k, v in self.initial.players.items()}
        entities = {k: list(v) for k, v in self.initial.entities.items()}
        actions = dict(self.initial.entity_actions)
        yield self.initial.current_tick, players, entities, actions
        for d in self.ticks:
            _apply(players, d.players, 0)
            _apply(entities, d.entities, 0)
            for i in d.entity_actions.removed:
                actions.pop(i, None)
            for a in d.entity_actions.added:
                actions[a["entity_id"]] = a
            for i, a in d.entity_actions.updated:
                actions[i] = a
            yield d.current_tick, players, entities, actions


def _apply(target: dict, delta: CollectionDelta, id_index: int) -> None:
    for i in delta.removed:
        if i not in target:
            raise ReplayError(f"removed id {i} is not present")
        del target[i]
    for v in delta.added:
        target[v[id_index]] = v
    for i, v in delta.updated:
        if i not in target:
            raise ReplayError(f"updated id {i} is not present")
        target[i] = v


def parse(data: bytes) -> Replay:
    r = Reader(data)
    initial = read_model(r)
    ticks = []
    expected = initial.current_tick
    while not r.eof:
        d = read_tick(r, initial.map_size)
        expected += 1
        if d.current_tick != expected:
            raise ReplayError(f"tick {d.current_tick} after {expected - 1} at {r.pos}")
        ticks.append(d)
    return Replay(initial, ticks)


def load(path: str | Path) -> Replay:
    return parse(Path(path).read_bytes())


def to_json(replay: Replay) -> dict:
    """Compact JSON for the web viewer: the initial state plus one delta per tick."""
    init = replay.initial
    ticks = []
    for d in replay.ticks:
        t = {"t": d.current_tick}
        if d.players.updated or d.players.added or d.players.removed:
            t["p"] = [v for _, v in d.players.updated] + d.players.added
        if d.entities.removed:
            t["r"] = d.entities.removed
        if d.entities.added:
            t["a"] = d.entities.added
        if d.entities.updated:
            t["u"] = [v for _, v in d.entities.updated]
        acts = {str(a["entity_id"]): _strip_action(a) for a in d.entity_actions.added}
        acts.update({str(i): _strip_action(a) for i, a in d.entity_actions.updated})
        if acts:
            t["act"] = acts
        if d.entity_actions.removed:
            t["actr"] = d.entity_actions.removed
        if d.events:
            t["ev"] = d.events
        ticks.append(t)
    return {
        "map_size": init.map_size,
        "fog_of_war": init.fog_of_war,
        "max_tick_count": init.max_tick_count,
        "tick_count": replay.tick_count,
        "entity_types": ENTITY_TYPES,
        "properties": {ENTITY_TYPES[k]: v for k, v in init.entity_properties.items()},
        "players": sorted(init.players.values(), key=lambda p: p[1]),
        "entities": list(init.entities.values()),
        "actions": {str(i): _strip_action(a) for i, a in init.entity_actions.items()},
        "ticks": ticks,
    }


def _strip_action(a: dict) -> dict:
    return {k: v for k, v in a.items() if k != "entity_id"}


def final_scores(replay: Replay) -> list[int]:
    """Scores by seat (player index) at the last tick."""
    players = None
    for _, players, _, _ in replay.states():
        pass
    return [p[2] for p in sorted(players.values(), key=lambda p: p[1])]


UNIT_TYPES = {3, 5, 7}             # BUILDER_UNIT, MELEE_UNIT, RANGED_UNIT
BASE_TYPES = {2, 4, 6}             # BUILDER_BASE, MELEE_BASE, RANGED_BASE
TIMELINE_EVERY = (10, 200)


def _type_name(t: int) -> str:
    return ENTITY_TYPES[t] if 0 <= t < len(ENTITY_TYPES) else str(t)


def timeline(replay: Replay, every: int = 50) -> dict:
    """The game as an analyst reads it: one sample per seat at tick 0, every `every` ticks (clamped to
    10..200) and at the last tick, plus the events that decide a game.

    A seat's sample: `score`, `resource`, `population` [used, provided by active buildings], `units` and
    active `buildings` by type, and since the previous sample `built` (entities added), `lost` (own
    entities removed: a unit died, a building was destroyed) and `killed` (enemy entities this seat
    destroyed). Types with a zero count are left out. Events: `base_destroyed` (a builder, melee or ranged
    base removed) and `eliminated` (a seat's last entity removed), each with the tick, the seat and `by`,
    the seat that dealt the blow (None when unknown).

    Who killed what: the tick's attack events are replayed against the health before the tick, in the
    engine's order, and the first hit that takes a target to zero is the killing blow (the rules give
    the destroy score to that attacker); when the damage does not add up, the last attacker of that
    tick is taken. Several players hitting one entity in the same tick is the only ambiguous case."""
    every = max(TIMELINE_EVERY[0], min(TIMELINE_EVERY[1], int(every)))
    init = replay.initial
    props = init.entity_properties
    slot_of = {pid: p[1] for pid, p in init.players.items()}
    order = sorted(slot_of, key=slot_of.get)
    players = {k: list(v) for k, v in init.players.items()}
    ents = {k: list(v) for k, v in init.entities.items()}
    alive = Counter(e[1] for e in ents.values() if e[1] is not None)
    out = set()
    window: dict = {}
    samples: list[dict] = []
    events: list[dict] = []

    def reset(pid):
        window[pid] = {"built": Counter(), "lost": Counter(), "killed": Counter()}

    for pid in order:
        reset(pid)

    def damage(e) -> int:
        attack = (props.get(e[2]) or {}).get("attack")
        return int(attack["damage"]) if attack else 0

    def sample(tick: int) -> None:
        units = {pid: Counter() for pid in order}
        buildings = {pid: Counter() for pid in order}
        used, provided = Counter(), Counter()
        for e in ents.values():
            pid = e[1]
            if pid not in units:
                continue
            p = props.get(e[2]) or {}
            used[pid] += int(p.get("population_use") or 0)
            if e[6]:
                provided[pid] += int(p.get("population_provide") or 0)
            if e[2] in UNIT_TYPES:
                units[pid][_type_name(e[2])] += 1
            elif e[6]:
                buildings[pid][_type_name(e[2])] += 1
        seats = []
        for pid in order:
            pl = players.get(pid)
            row = {"slot": slot_of[pid], "score": pl[2] if pl else 0, "resource": pl[3] if pl else 0,
                   "population": [used[pid], provided[pid]], "units": dict(units[pid]),
                   "buildings": dict(buildings[pid])}
            for key, counts in window[pid].items():
                if counts:
                    row[key] = dict(counts)
            seats.append(row)
            reset(pid)
        samples.append({"tick": tick, "seats": seats})

    sample(init.current_tick)
    last = replay.tick_count
    for d in replay.ticks:
        hp, killer, hitter = {}, {}, {}
        for ev in d.events:
            if ev[0] != 0:
                continue
            a, b = ents.get(ev[1]), ents.get(ev[2])
            if a is None or b is None or b[1] is None:     # unknown ids, or mining a resource
                continue
            hitter[b[0]] = a[1]
            health = hp.get(b[0], b[5])
            if health > 0:
                health -= damage(a)
                hp[b[0]] = health
                if health <= 0:
                    killer.setdefault(b[0], a[1])
        last_blow = {}
        for i in d.entities.removed:
            e = ents.get(i)
            if e is None or e[1] is None:                    # a resource ran out
                continue
            pid, name = e[1], _type_name(e[2])
            by = killer.get(i, hitter.get(i))
            by = None if by == pid else by
            if pid in window:
                window[pid]["lost"][name] += 1
            if by in window:
                window[by]["killed"][name] += 1
            alive[pid] -= 1
            last_blow[pid] = by
            if e[2] in BASE_TYPES:
                events.append({"tick": d.current_tick, "event": "base_destroyed", "slot": slot_of.get(pid),
                               "base": name, "by": slot_of.get(by)})
        for e in d.entities.added:
            if e[1] is not None:
                alive[e[1]] += 1
                if e[1] in window:
                    window[e[1]]["built"][_type_name(e[2])] += 1
        _apply(ents, d.entities, 0)
        _apply(players, d.players, 0)
        for pid in order:
            if pid not in out and pid in last_blow and alive[pid] <= 0:
                out.add(pid)
                events.append({"tick": d.current_tick, "event": "eliminated", "slot": slot_of[pid],
                               "by": slot_of.get(last_blow[pid])})
        if d.current_tick % every == 0 or d.current_tick == last:
            sample(d.current_tick)
    return {"tick_count": last, "map_size": init.map_size, "fog_of_war": init.fog_of_war, "every": every,
            "samples": samples, "events": events}


def main(argv=None):
    args = argv or sys.argv[1:]
    if not args:
        sys.exit("usage: python -m codecraft.replay <replay.dat> [--json out.json]")
    replay = load(args[0])
    print(f"map {replay.initial.map_size}x{replay.initial.map_size}, fog={replay.initial.fog_of_war}, "
          f"ticks {replay.tick_count}, players {len(replay.initial.players)}, "
          f"initial entities {len(replay.initial.entities)}")
    print("final scores by seat:", final_scores(replay))
    if len(args) >= 3 and args[1] == "--json":
        Path(args[2]).write_text(json.dumps(to_json(replay), separators=(",", ":")))
        print("written", args[2])


if __name__ == "__main__":
    main()
