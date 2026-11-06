"""The replay file of `aicup2020 --save-replay` (docs/replay-format.md): reader and writer.

    Model                      -- the full state at the first tick
    repeat for every tick:
        Vec<Event>             -- what happened during the tick (tag 0: attack, mining included)
        ModelDelta             -- the scalars, then added / removed / updated per collection

The reader keeps the order of every map and list as stored, so that `write_replay(parse(data)) == data` for
every file the official engine wrote (the order there comes from a Rust HashMap with a random hasher: it
differs from run to run and means nothing). `server/codecraft/replay.py` is the server's own decoder of the
same format; the two are checked against each other in sim/tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .model import EntityAction, Event, Model, Player
from .trans import (ProtocolError, Reader, Writer, read_entity, read_entity_action, read_properties_map,
                    write_entity, write_entity_action, write_properties_map)


@dataclass
class CollectionDelta:
    added: list = field(default_factory=list)       # whole items (Player, Entity, or (entity_id, EntityAction))
    removed: list = field(default_factory=list)     # ids
    updated: list = field(default_factory=list)     # (id, whole item) pairs

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.updated)


@dataclass
class ModelDelta:
    next_id: int
    map_size: int
    fog_of_war: bool
    max_tick_count: int
    max_pathfind_nodes: int
    current_tick: int
    passive_income: int
    players: CollectionDelta
    entities: CollectionDelta
    entity_actions: CollectionDelta     # items are (entity_id, EntityAction)


@dataclass
class TickRecord:
    events: list[Event]
    delta: ModelDelta


@dataclass
class Replay:
    initial: Model
    ticks: list[TickRecord]

    @property
    def tick_count(self) -> int:
        return self.ticks[-1].delta.current_tick if self.ticks else self.initial.current_tick

    def states(self) -> Iterator[Model]:
        """The model at the first tick, then after every tick. Each is a new Model with its own dicts; entities and
        players that did not change are shared between consecutive models, so never change one in place."""
        m = self.initial
        yield m
        for t in self.ticks:
            m = apply_delta(m, t.delta)
            yield m


# Reading -------------------------------------------------------------------------------------------------
def read_player(r) -> Player:
    return Player(r.i32(), r.i32(), r.i32(), r.i32())


def read_stored_action(r) -> tuple[int, EntityAction]:
    return r.i32(), read_entity_action(r)


def read_model(r) -> Model:
    next_id, map_size, fog = r.i32(), r.i32(), r.bool()
    props = read_properties_map(r)
    max_tick, max_nodes, tick, passive = r.i32(), r.i32(), r.i32(), r.i32()
    players = {}
    for _ in range(r.count()):
        key = r.i32()
        players[key] = read_player(r)
    entities = {}
    for _ in range(r.count()):
        key = r.i32()
        entities[key] = read_entity(r)
    actions = {}
    for _ in range(r.count()):
        key = r.i32()
        entity_id, action = read_stored_action(r)
        if entity_id != key:
            raise ProtocolError(f"stored action key {key} holds entity {entity_id}")
        actions[key] = action
    return Model(next_id, map_size, fog, props, max_tick, max_nodes, tick, passive, players, entities, actions)


def _read_collection_delta(r, read_item) -> CollectionDelta:
    added = [read_item(r) for _ in range(r.count())]
    removed = [r.i32() for _ in range(r.count())]
    updated = []
    for _ in range(r.count()):
        key = r.i32()
        updated.append((key, read_item(r)))
    return CollectionDelta(added, removed, updated)


def read_event(r) -> Event:
    return Event(r.i32(), r.i32(), r.i32())


def read_tick(r) -> TickRecord:
    events = [read_event(r) for _ in range(r.count())]
    delta = ModelDelta(r.i32(), r.i32(), r.bool(), r.i32(), r.i32(), r.i32(), r.i32(),
                       _read_collection_delta(r, read_player), _read_collection_delta(r, read_entity),
                       _read_collection_delta(r, read_stored_action))
    return TickRecord(events, delta)


def parse(data: bytes) -> Replay:
    r = Reader(data)
    initial = read_model(r)
    ticks = []
    expected = initial.current_tick
    while not r.eof:
        t = read_tick(r)
        expected += 1
        if t.delta.map_size != initial.map_size:
            raise ProtocolError(f"lost sync at byte {r.pos}: map size {t.delta.map_size}")
        if t.delta.current_tick != expected:
            raise ProtocolError(f"tick {t.delta.current_tick} after {expected - 1} at byte {r.pos}")
        ticks.append(t)
    return Replay(initial, ticks)


def load(path: str | Path) -> Replay:
    return parse(Path(path).read_bytes())


# Applying and computing deltas ---------------------------------------------------------------------------
def apply_delta(m: Model, d: ModelDelta) -> Model:
    out = Model(d.next_id, d.map_size, d.fog_of_war, m.entity_properties, d.max_tick_count, d.max_pathfind_nodes,
                d.current_tick, d.passive_income, dict(m.players), dict(m.entities), dict(m.entity_actions))
    _apply(out.players, d.players, lambda p: (p.id, p))
    _apply(out.entities, d.entities, lambda e: (e.id, e))
    _apply(out.entity_actions, d.entity_actions, lambda a: a)
    return out


def _apply(target: dict, delta: CollectionDelta, key_value) -> None:
    for key in delta.removed:
        if key not in target:
            raise ProtocolError(f"removed id {key} is not present")
        del target[key]
    for item in delta.added:
        key, value = key_value(item)
        target[key] = value
    for key, item in delta.updated:
        if key not in target:
            raise ProtocolError(f"updated id {key} is not present")
        target[key] = key_value(item)[1]


def diff(prev: Model, cur: Model) -> ModelDelta:
    """The delta from `prev` to `cur`: the scalars of `cur`; per collection the items only in `cur` (added, in
    `cur`'s order), the ids only in `prev` (removed, in `prev`'s order) and the items whose value changed
    (updated, in `cur`'s order)."""
    return ModelDelta(cur.next_id, cur.map_size, cur.fog_of_war, cur.max_tick_count, cur.max_pathfind_nodes,
                      cur.current_tick, cur.passive_income,
                      _diff(prev.players, cur.players, lambda k, v: v),
                      _diff(prev.entities, cur.entities, lambda k, v: v),
                      _diff(prev.entity_actions, cur.entity_actions, lambda k, v: (k, v)))


def _diff(prev: dict, cur: dict, item) -> CollectionDelta:
    d = CollectionDelta()
    for key, value in cur.items():
        old = prev.get(key)
        if old is None:
            d.added.append(item(key, value))
        elif old != value:
            d.updated.append((key, item(key, value)))
    d.removed = [key for key in prev if key not in cur]
    return d


# Writing -------------------------------------------------------------------------------------------------
def write_player(w: Writer, p: Player) -> None:
    w.i32(p.id)
    w.i32(p.index)
    w.i32(p.score)
    w.i32(p.resource)


def write_stored_action(w: Writer, item: tuple[int, EntityAction]) -> None:
    w.i32(item[0])
    write_entity_action(w, item[1])


def write_model(w: Writer, m: Model) -> None:
    w.i32(m.next_id)
    w.i32(m.map_size)
    w.bool(m.fog_of_war)
    write_properties_map(w, m.entity_properties)
    w.i32(m.max_tick_count)
    w.i32(m.max_pathfind_nodes)
    w.i32(m.current_tick)
    w.i32(m.passive_income)
    w.i32(len(m.players))
    for key, p in m.players.items():
        w.i32(key)
        write_player(w, p)
    w.i32(len(m.entities))
    for key, e in m.entities.items():
        w.i32(key)
        write_entity(w, e)
    w.i32(len(m.entity_actions))
    for key, a in m.entity_actions.items():
        w.i32(key)
        write_stored_action(w, (key, a))


def _write_collection_delta(w: Writer, d: CollectionDelta, write_item) -> None:
    w.i32(len(d.added))
    for item in d.added:
        write_item(w, item)
    w.i32(len(d.removed))
    for key in d.removed:
        w.i32(key)
    w.i32(len(d.updated))
    for key, item in d.updated:
        w.i32(key)
        write_item(w, item)


def write_tick(w: Writer, events: list[Event], d: ModelDelta) -> None:
    w.i32(len(events))
    for ev in events:
        w.i32(ev[0])
        w.i32(ev[1])
        w.i32(ev[2])
    for v in (d.next_id, d.map_size):
        w.i32(v)
    w.bool(d.fog_of_war)
    for v in (d.max_tick_count, d.max_pathfind_nodes, d.current_tick, d.passive_income):
        w.i32(v)
    _write_collection_delta(w, d.players, write_player)
    _write_collection_delta(w, d.entities, write_entity)
    _write_collection_delta(w, d.entity_actions, write_stored_action)


def write_replay(replay: Replay) -> bytes:
    w = Writer()
    write_model(w, replay.initial)
    for t in replay.ticks:
        write_tick(w, t.events, t.delta)
    return w.getvalue()


class ReplayWriter:
    """Writes a replay as a game runs: `start(model)` once, then `tick(events, model)` after every tick."""

    def __init__(self, path: str | Path) -> None:
        self.f = open(path, "wb")
        self.prev: Model | None = None

    def start(self, model: Model) -> None:
        w = Writer()
        write_model(w, model)
        self.f.write(w.buf)
        self.prev = model.copy()

    def tick(self, events: list[Event], model: Model) -> None:
        w = Writer()
        write_tick(w, events, diff(self.prev, model))
        self.f.write(w.buf)
        self.prev = model.copy()

    def close(self) -> None:
        self.f.close()

    def __enter__(self) -> "ReplayWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
