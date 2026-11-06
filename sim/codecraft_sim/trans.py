"""The `trans` binary encoding of the official engine: the TCP protocol of the 15 clients and the replay file.

Little-endian `i32`; `bool` = one byte (0 or 1); `string` = i32 byte length + UTF-8; `Option<T>` = a bool tag +
the value; `Vec<T>` = i32 count + the items; `Map<K, V>` = i32 count + (key, value) pairs; an enum = its i32 tag
(+ its fields for a sum type). Field orders: `clients/Python/model/*.py` (wire) and docs/replay-format.md.

Session: the client writes its token (string); then, every
tick, the server writes `ServerMessage::GetAction(player_view, debug_available)` and the client answers
`ClientMessage::ActionMessage(action)`; at the end the server writes `ServerMessage::Finish`.
"""
from __future__ import annotations

import struct
from typing import BinaryIO, Callable

from .model import (AttackAction, AttackProperties, AutoAttack, BuildAction, BuildProperties, Entity, EntityAction,
                    EntityProperties, EntityType, MoveAction, PlayerView, RepairAction, RepairProperties, Vec2,
                    ViewPlayer)

_I32 = struct.Struct("<i")
_I64 = struct.Struct("<q")
_F32 = struct.Struct("<f")
_F64 = struct.Struct("<d")
MAX_COUNT = 10_000_000


class ProtocolError(ValueError):
    """Bytes that are not a valid message: a bad tag, a bad bool, a count out of range, a truncated stream."""


class Writer:
    __slots__ = ("buf",)

    def __init__(self) -> None:
        self.buf = bytearray()

    def i32(self, v: int) -> None:
        self.buf += _I32.pack(v)

    def i64(self, v: int) -> None:
        self.buf += _I64.pack(v)

    def f32(self, v: float) -> None:
        self.buf += _F32.pack(v)

    def f64(self, v: float) -> None:
        self.buf += _F64.pack(v)

    def bool(self, v: bool) -> None:
        self.buf.append(1 if v else 0)

    def string(self, s: str) -> None:
        data = s.encode("utf-8")
        self.i32(len(data))
        self.buf += data

    def opt(self, v, f: Callable) -> None:
        if v is None:
            self.buf.append(0)
        else:
            self.buf.append(1)
            f(v)

    def getvalue(self) -> bytes:
        return bytes(self.buf)


class Reader:
    """Reads from a bytes object (a replay file, a recorded message)."""
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def _unpack(self, s: struct.Struct):
        try:
            v = s.unpack_from(self.data, self.pos)[0]
        except struct.error as exc:
            raise ProtocolError(f"truncated at byte {self.pos}") from exc
        self.pos += s.size
        return v

    def i32(self) -> int:
        return self._unpack(_I32)

    def i64(self) -> int:
        return self._unpack(_I64)

    def f32(self) -> float:
        return self._unpack(_F32)

    def f64(self) -> float:
        return self._unpack(_F64)

    def bool(self) -> bool:
        if self.pos >= len(self.data):
            raise ProtocolError(f"truncated at byte {self.pos}")
        v = self.data[self.pos]
        if v > 1:
            raise ProtocolError(f"bad bool {v} at byte {self.pos}")
        self.pos += 1
        return v == 1

    def string(self) -> str:
        n = self.count()
        if self.pos + n > len(self.data):
            raise ProtocolError(f"truncated string at byte {self.pos}")
        s = self.data[self.pos:self.pos + n]
        self.pos += n
        return s.decode("utf-8")

    def count(self) -> int:
        n = self.i32()
        if n < 0 or n > MAX_COUNT:
            raise ProtocolError(f"bad count {n} at byte {self.pos - 4}")
        return n

    @property
    def eof(self) -> bool:
        return self.pos >= len(self.data)


class StreamReader:
    """Reads from a binary stream (`socket.makefile("rb")`); EOFError when the peer closes mid-message."""
    __slots__ = ("f",)

    def __init__(self, f: BinaryIO) -> None:
        self.f = f

    def _read(self, n: int) -> bytes:
        data = self.f.read(n)
        if data is None or len(data) != n:
            raise EOFError(f"stream closed ({0 if not data else len(data)} of {n} bytes)")
        return data

    def i32(self) -> int:
        return _I32.unpack(self._read(4))[0]

    def i64(self) -> int:
        return _I64.unpack(self._read(8))[0]

    def f32(self) -> float:
        return _F32.unpack(self._read(4))[0]

    def f64(self) -> float:
        return _F64.unpack(self._read(8))[0]

    def bool(self) -> bool:
        v = self._read(1)[0]
        if v > 1:
            raise ProtocolError(f"bad bool {v}")
        return v == 1

    def string(self) -> str:
        return self._read(self.count()).decode("utf-8")

    def count(self) -> int:
        n = self.i32()
        if n < 0 or n > MAX_COUNT:
            raise ProtocolError(f"bad count {n}")
        return n


def read_opt(r, f: Callable):
    return f() if r.bool() else None


def read_vec(r, f: Callable) -> list:
    return [f() for _ in range(r.count())]


def entity_type(r) -> EntityType:
    tag = r.i32()
    try:
        return EntityType(tag)
    except ValueError:
        raise ProtocolError(f"bad entity type {tag}") from None


# Shared types --------------------------------------------------------------------------------------------
def write_vec2(w: Writer, v: Vec2) -> None:
    w.i32(v.x)
    w.i32(v.y)


def read_vec2(r) -> Vec2:
    return Vec2(r.i32(), r.i32())


def write_types(w: Writer, types) -> None:
    w.i32(len(types))
    for t in types:
        w.i32(t)


def read_types(r) -> tuple[EntityType, ...]:
    return tuple(entity_type(r) for _ in range(r.count()))


def write_properties(w: Writer, p: EntityProperties) -> None:
    for v in (p.size, p.build_score, p.destroy_score):
        w.i32(v)
    w.bool(p.can_move)
    for v in (p.population_provide, p.population_use, p.max_health, p.initial_cost, p.sight_range,
              p.resource_per_health):
        w.i32(v)
    w.opt(p.build, lambda b: (write_types(w, b.options), w.opt(b.init_health, w.i32)))
    w.opt(p.attack, lambda a: (w.i32(a.attack_range), w.i32(a.damage), w.bool(a.collect_resource)))
    w.opt(p.repair, lambda rp: (write_types(w, rp.valid_targets), w.i32(rp.power)))


def read_properties(r) -> EntityProperties:
    size, build_score, destroy_score = r.i32(), r.i32(), r.i32()
    can_move = r.bool()
    pop_provide, pop_use, max_health, cost, sight, rph = r.i32(), r.i32(), r.i32(), r.i32(), r.i32(), r.i32()
    build = read_opt(r, lambda: BuildProperties(read_types(r), read_opt(r, r.i32)))
    attack = read_opt(r, lambda: AttackProperties(r.i32(), r.i32(), r.bool()))
    repair = read_opt(r, lambda: RepairProperties(read_types(r), r.i32()))
    return EntityProperties(size, build_score, destroy_score, can_move, pop_provide, pop_use, max_health, cost,
                            sight, rph, build, attack, repair)


def write_properties_map(w: Writer, props: dict) -> None:
    w.i32(len(props))
    for t, p in props.items():
        w.i32(t)
        write_properties(w, p)


def read_properties_map(r) -> dict[EntityType, EntityProperties]:
    out = {}
    for _ in range(r.count()):
        t = entity_type(r)
        out[t] = read_properties(r)
    return out


def write_entity(w: Writer, e: Entity) -> None:
    w.i32(e.id)
    w.opt(e.player_id, w.i32)
    w.i32(e.entity_type)
    write_vec2(w, e.position)
    w.i32(e.health)
    w.bool(e.active)


def read_entity(r) -> Entity:
    return Entity(r.i32(), read_opt(r, r.i32), entity_type(r), read_vec2(r), r.i32(), r.bool())


def write_entity_action(w: Writer, a: EntityAction) -> None:
    w.opt(a.move_action, lambda m: (write_vec2(w, m.target), w.bool(m.find_closest_position),
                                    w.bool(m.break_through)))
    w.opt(a.build_action, lambda b: (w.i32(b.entity_type), write_vec2(w, b.position)))
    w.opt(a.attack_action, lambda at: (w.opt(at.target, w.i32), w.opt(
        at.auto_attack, lambda aa: (w.i32(aa.pathfind_range), write_types(w, aa.valid_targets)))))
    w.opt(a.repair_action, lambda rp: w.i32(rp.target))


def read_entity_action(r) -> EntityAction:
    move = read_opt(r, lambda: MoveAction(read_vec2(r), r.bool(), r.bool()))
    build = read_opt(r, lambda: BuildAction(entity_type(r), read_vec2(r)))
    attack = read_opt(r, lambda: AttackAction(read_opt(r, r.i32),
                                              read_opt(r, lambda: AutoAttack(r.i32(), read_types(r)))))
    repair = read_opt(r, lambda: RepairAction(r.i32()))
    return EntityAction(move, build, attack, repair)


# Wire messages -------------------------------------------------------------------------------------------
def write_action(w: Writer, actions: dict[int, EntityAction]) -> None:
    """An `Action`: the map entity id -> EntityAction a client sends."""
    w.i32(len(actions))
    for entity_id, a in actions.items():
        w.i32(entity_id)
        write_entity_action(w, a)


def read_action(r) -> dict[int, EntityAction]:
    out = {}
    for _ in range(r.count()):
        entity_id = r.i32()
        out[entity_id] = read_entity_action(r)
    return out


def write_player_view(w: Writer, v: PlayerView) -> None:
    w.i32(v.my_id)
    w.i32(v.map_size)
    w.bool(v.fog_of_war)
    write_properties_map(w, v.entity_properties)
    w.i32(v.max_tick_count)
    w.i32(v.max_pathfind_nodes)
    w.i32(v.current_tick)
    w.i32(len(v.players))
    for p in v.players:
        w.i32(p.id)
        w.i32(p.score)
        w.i32(p.resource)
    w.i32(len(v.entities))
    for e in v.entities:
        write_entity(w, e)


def read_player_view(r) -> PlayerView:
    my_id, map_size, fog = r.i32(), r.i32(), r.bool()
    props = read_properties_map(r)
    max_tick, max_nodes, tick = r.i32(), r.i32(), r.i32()
    players = read_vec(r, lambda: ViewPlayer(r.i32(), r.i32(), r.i32()))
    entities = read_vec(r, lambda: read_entity(r))
    return PlayerView(my_id, map_size, fog, props, max_tick, max_nodes, tick, players, entities)


# ServerMessage tags
GET_ACTION, FINISH, DEBUG_UPDATE = 0, 1, 2
# ClientMessage tags
DEBUG_MESSAGE, ACTION_MESSAGE, DEBUG_UPDATE_DONE, REQUEST_DEBUG_STATE = 0, 1, 2, 3


def write_get_action(w: Writer, view: PlayerView, debug_available: bool = False) -> None:
    w.i32(GET_ACTION)
    write_player_view(w, view)
    w.bool(debug_available)


def write_finish(w: Writer) -> None:
    w.i32(FINISH)


def read_server_message(r) -> tuple:
    """("get_action", PlayerView, debug_available) | ("finish",) | ("debug_update", PlayerView)."""
    tag = r.i32()
    if tag == GET_ACTION:
        view = read_player_view(r)
        return "get_action", view, r.bool()
    if tag == FINISH:
        return ("finish",)
    if tag == DEBUG_UPDATE:
        return "debug_update", read_player_view(r)
    raise ProtocolError(f"bad server message tag {tag}")


def write_action_message(w: Writer, actions: dict[int, EntityAction]) -> None:
    w.i32(ACTION_MESSAGE)
    write_action(w, actions)


def read_client_message(r) -> dict[int, EntityAction]:
    """The client's answer to GetAction. The debug messages (tags 0, 2, 3) are not allowed in batch mode, where
    `debug_available` is false: they are a protocol error here."""
    tag = r.i32()
    if tag == ACTION_MESSAGE:
        return read_action(r)
    raise ProtocolError(f"unexpected client message tag {tag}")
