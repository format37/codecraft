"""The CodeCraft 2020 game state as plain data, and its JSON form.

Every type here mirrors one of the official engine's (`aicup2020` 1.3.0): the field names and orders are the
ones of the TCP protocol (`clients/Python/model/*.py`) and of the replay (`docs/replay-format.md`). The JSON
form is the engine's own serde JSON, the one a `LoadFrom` state file is written in:

- enum values are strings (`"BuilderUnit"`), an `Option` is `null` or the value, a position is `{"x", "y"}`;
- a collection (players, entities, stored actions) is `{"by_id": {"<id>": value}}` with string keys;
- the attack range is `range` in JSON but `attack_range` on the wire. A JSON key the engine does not know is
  ignored without a word, and a missing `Option` field reads as `null`: a misspelt optional field is silently
  dropped, so keep the names exactly as written here.

The binary encoding of these types is in trans.py.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import IntEnum
from typing import NamedTuple


class EntityType(IntEnum):
    WALL = 0
    HOUSE = 1
    BUILDER_BASE = 2
    BUILDER_UNIT = 3
    MELEE_BASE = 4
    MELEE_UNIT = 5
    RANGED_BASE = 6
    RANGED_UNIT = 7
    RESOURCE = 8
    TURRET = 9

    @property
    def json_name(self) -> str:
        return JSON_NAMES[self]

    @classmethod
    def parse(cls, value: "EntityType | int | str") -> "EntityType":
        """An entity type from its tag, its JSON name ("BuilderUnit") or its Python name ("BUILDER_UNIT")."""
        if isinstance(value, str):
            if value in JSON_NAMES:
                return cls(JSON_NAMES.index(value))
            return cls[value]
        return cls(value)


JSON_NAMES = ("Wall", "House", "BuilderBase", "BuilderUnit", "MeleeBase", "MeleeUnit", "RangedBase", "RangedUnit",
              "Resource", "Turret")


class Vec2(NamedTuple):
    x: int
    y: int


# Entity properties ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class AttackProperties:
    attack_range: int
    damage: int
    collect_resource: bool


@dataclass(frozen=True)
class BuildProperties:
    options: tuple[EntityType, ...]
    init_health: int | None


@dataclass(frozen=True)
class RepairProperties:
    valid_targets: tuple[EntityType, ...]
    power: int


@dataclass(frozen=True)
class EntityProperties:
    size: int
    build_score: int
    destroy_score: int
    can_move: bool
    population_provide: int
    population_use: int
    max_health: int
    initial_cost: int
    sight_range: int
    resource_per_health: int
    build: BuildProperties | None = None
    attack: AttackProperties | None = None
    repair: RepairProperties | None = None


def _props(size, build_score, destroy_score, can_move, population_provide, population_use, max_health, initial_cost,
           sight_range, resource_per_health, build=None, attack=None, repair=None) -> EntityProperties:
    return EntityProperties(size, build_score, destroy_score, can_move, population_provide, population_use,
                            max_health, initial_cost, sight_range, resource_per_health, build, attack, repair)


T = EntityType
_BASE = dict(size=5, build_score=500, destroy_score=5000, can_move=False, population_provide=5, population_use=0,
             max_health=300, initial_cost=500, sight_range=5, resource_per_health=0)

# docs/doc-en.md "Entity properties values"; every preset uses this table, in the doc's order. The official
# engine keeps its maps in Rust HashMaps with a per-process random hasher: every replay lists this map (and the
# entities) in a different order, and nothing may depend on that order.
DEFAULT_PROPERTIES: dict[EntityType, EntityProperties] = {
    T.HOUSE: _props(3, 50, 500, False, 5, 0, 50, 50, 5, 0),
    T.RANGED_UNIT: _props(1, 30, 300, True, 0, 1, 10, 30, 10, 0, attack=AttackProperties(5, 5, False)),
    T.BUILDER_UNIT: _props(
        1, 10, 100, True, 0, 1, 10, 10, 10, 0,
        build=BuildProperties((T.HOUSE, T.WALL, T.BUILDER_BASE, T.MELEE_BASE, T.RANGED_BASE, T.TURRET), 5),
        attack=AttackProperties(1, 1, True),
        repair=RepairProperties((T.HOUSE, T.WALL, T.BUILDER_UNIT, T.MELEE_UNIT, T.RANGED_UNIT, T.BUILDER_BASE,
                                 T.MELEE_BASE, T.RANGED_BASE, T.TURRET), 1)),
    T.MELEE_UNIT: _props(1, 20, 200, True, 0, 1, 50, 20, 10, 0, attack=AttackProperties(1, 5, False)),
    T.WALL: _props(1, 10, 10, False, 0, 0, 50, 10, 2, 0),
    T.RESOURCE: _props(1, 0, 0, False, 0, 0, 30, 0, 0, 1),
    T.TURRET: _props(2, 50, 500, False, 0, 0, 100, 50, 10, 0, attack=AttackProperties(5, 5, False)),
    T.BUILDER_BASE: _props(**_BASE, build=BuildProperties((T.BUILDER_UNIT,), None)),
    T.RANGED_BASE: _props(**_BASE, build=BuildProperties((T.RANGED_UNIT,), None)),
    T.MELEE_BASE: _props(**_BASE, build=BuildProperties((T.MELEE_UNIT,), None)),
}
del T, _BASE


# Actions -------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class MoveAction:
    target: Vec2
    find_closest_position: bool = True
    break_through: bool = False


@dataclass(frozen=True)
class BuildAction:
    entity_type: EntityType
    position: Vec2


@dataclass(frozen=True)
class AutoAttack:
    pathfind_range: int
    valid_targets: tuple[EntityType, ...] = ()


@dataclass(frozen=True)
class AttackAction:
    target: int | None = None
    auto_attack: AutoAttack | None = None


@dataclass(frozen=True)
class RepairAction:
    target: int


@dataclass(frozen=True)
class EntityAction:
    """What an entity is told to do; the engine tries attack, build, repair, move in this order."""
    move_action: MoveAction | None = None
    build_action: BuildAction | None = None
    attack_action: AttackAction | None = None
    repair_action: RepairAction | None = None


# State ---------------------------------------------------------------------------------------------------
@dataclass
class Player:
    id: int
    index: int          # the seat, 0-based (the replay has it; the client's Player does not)
    score: int = 0
    resource: int = 0


@dataclass
class Entity:
    id: int
    player_id: int | None
    entity_type: EntityType
    position: Vec2
    health: int
    active: bool = True


@dataclass
class Model:
    """The whole game state, as in a replay and in a `LoadFrom` state file. `entity_actions` maps an entity id to
    its stored action (a `StoredAction` = entity id + EntityAction in the official encoding)."""
    next_id: int
    map_size: int
    fog_of_war: bool
    entity_properties: dict[EntityType, EntityProperties]
    max_tick_count: int
    max_pathfind_nodes: int
    current_tick: int
    passive_income: int
    players: dict[int, Player] = field(default_factory=dict)
    entities: dict[int, Entity] = field(default_factory=dict)
    entity_actions: dict[int, EntityAction] = field(default_factory=dict)

    def copy(self) -> "Model":
        """A copy whose players and entities can be changed without touching this one (properties and actions are
        immutable and shared)."""
        out = copy.copy(self)
        out.players = {k: copy.copy(v) for k, v in self.players.items()}
        out.entities = {k: copy.copy(v) for k, v in self.entities.items()}
        out.entity_actions = dict(self.entity_actions)
        out.entity_properties = dict(self.entity_properties)
        return out


@dataclass
class ViewPlayer:
    """A player as a client sees it (no seat index)."""
    id: int
    score: int
    resource: int


@dataclass
class PlayerView:
    my_id: int
    map_size: int
    fog_of_war: bool
    entity_properties: dict[EntityType, EntityProperties]
    max_tick_count: int
    max_pathfind_nodes: int
    current_tick: int
    players: list[ViewPlayer]
    entities: list[Entity]


class Event(NamedTuple):
    """A replay event: tag 0 = attack (a = attacker id, b = target id), mining included. No other tag has been
    seen in a replay."""
    tag: int
    a: int
    b: int


ATTACK_EVENT = 0


# JSON (the engine's serde form) --------------------------------------------------------------------------
def vec_json(v: Vec2) -> dict:
    return {"x": v.x, "y": v.y}


def vec_from_json(d: dict) -> Vec2:
    return Vec2(int(d["x"]), int(d["y"]))


def _types_json(types) -> list[str]:
    return [EntityType(t).json_name for t in types]


def _types_from_json(names) -> tuple[EntityType, ...]:
    return tuple(EntityType.parse(n) for n in names)


def properties_json(p: EntityProperties) -> dict:
    return {
        "size": p.size, "build_score": p.build_score, "destroy_score": p.destroy_score, "can_move": p.can_move,
        "population_provide": p.population_provide, "population_use": p.population_use,
        "max_health": p.max_health, "initial_cost": p.initial_cost, "sight_range": p.sight_range,
        "resource_per_health": p.resource_per_health,
        "build": None if p.build is None else {"options": _types_json(p.build.options),
                                               "init_health": p.build.init_health},
        "attack": None if p.attack is None else {"range": p.attack.attack_range, "damage": p.attack.damage,
                                                 "collect_resource": p.attack.collect_resource},
        "repair": None if p.repair is None else {"valid_targets": _types_json(p.repair.valid_targets),
                                                 "power": p.repair.power},
    }


def properties_from_json(d: dict) -> EntityProperties:
    build, attack, repair = d.get("build"), d.get("attack"), d.get("repair")
    return EntityProperties(
        int(d["size"]), int(d["build_score"]), int(d["destroy_score"]), bool(d["can_move"]),
        int(d["population_provide"]), int(d["population_use"]), int(d["max_health"]), int(d["initial_cost"]),
        int(d["sight_range"]), int(d["resource_per_health"]),
        None if build is None else BuildProperties(_types_from_json(build["options"]), build.get("init_health")),
        None if attack is None else AttackProperties(int(attack["range"]), int(attack["damage"]),
                                                     bool(attack["collect_resource"])),
        None if repair is None else RepairProperties(_types_from_json(repair["valid_targets"]), int(repair["power"])),
    )


def action_json(a: EntityAction) -> dict:
    m, b, at, r = a.move_action, a.build_action, a.attack_action, a.repair_action
    return {
        "move_action": None if m is None else {"target": vec_json(m.target),
                                               "find_closest_position": m.find_closest_position,
                                               "break_through": m.break_through},
        "build_action": None if b is None else {"entity_type": b.entity_type.json_name,
                                                "position": vec_json(b.position)},
        "attack_action": None if at is None else {
            "target": at.target,
            "auto_attack": None if at.auto_attack is None else {
                "pathfind_range": at.auto_attack.pathfind_range,
                "valid_targets": _types_json(at.auto_attack.valid_targets)}},
        "repair_action": None if r is None else {"target": r.target},
    }


def action_from_json(d: dict) -> EntityAction:
    m, b, at, r = d.get("move_action"), d.get("build_action"), d.get("attack_action"), d.get("repair_action")
    auto = at.get("auto_attack") if at else None
    return EntityAction(
        None if m is None else MoveAction(vec_from_json(m["target"]), bool(m["find_closest_position"]),
                                          bool(m["break_through"])),
        None if b is None else BuildAction(EntityType.parse(b["entity_type"]), vec_from_json(b["position"])),
        None if at is None else AttackAction(
            at.get("target"),
            None if auto is None else AutoAttack(int(auto["pathfind_range"]), _types_from_json(auto["valid_targets"]))),
        None if r is None else RepairAction(int(r["target"])),
    )


def player_json(p: Player) -> dict:
    return {"id": p.id, "index": p.index, "score": p.score, "resource": p.resource}


def player_from_json(d: dict) -> Player:
    return Player(int(d["id"]), int(d["index"]), int(d["score"]), int(d["resource"]))


def entity_json(e: Entity) -> dict:
    return {"id": e.id, "player_id": e.player_id, "entity_type": e.entity_type.json_name,
            "position": vec_json(e.position), "health": e.health, "active": e.active}


def entity_from_json(d: dict) -> Entity:
    return Entity(int(d["id"]), d.get("player_id"), EntityType.parse(d["entity_type"]), vec_from_json(d["position"]),
                  int(d["health"]), bool(d["active"]))


def _collection(items: dict, f) -> dict:
    return {"by_id": {str(k): f(k, v) for k, v in items.items()}}


def _from_collection(d: dict, f) -> dict:
    return {int(k): f(v) for k, v in d["by_id"].items()}


def model_json(m: Model) -> dict:
    """The model as the engine reads it from a `LoadFrom` state file."""
    return {
        "next_id": m.next_id, "map_size": m.map_size, "fog_of_war": m.fog_of_war,
        "entity_properties": {t.json_name: properties_json(p) for t, p in m.entity_properties.items()},
        "max_tick_count": m.max_tick_count, "max_pathfind_nodes": m.max_pathfind_nodes,
        "current_tick": m.current_tick, "passive_income": m.passive_income,
        "players": _collection(m.players, lambda k, p: player_json(p)),
        "entities": _collection(m.entities, lambda k, e: entity_json(e)),
        "entity_actions": _collection(m.entity_actions, lambda k, a: {"entity_id": k, "action": action_json(a)}),
    }


def model_from_json(d: dict) -> Model:
    return Model(
        int(d["next_id"]), int(d["map_size"]), bool(d["fog_of_war"]),
        {EntityType.parse(k): properties_from_json(v) for k, v in d["entity_properties"].items()},
        int(d["max_tick_count"]), int(d["max_pathfind_nodes"]), int(d["current_tick"]), int(d["passive_income"]),
        _from_collection(d["players"], player_from_json),
        _from_collection(d["entities"], entity_from_json),
        {int(k): action_from_json(v["action"]) for k, v in d["entity_actions"]["by_id"].items()},
    )


def view_json(v: PlayerView) -> dict:
    """A PlayerView in the same JSON style (players and entities as lists, in the order received)."""
    return {
        "my_id": v.my_id, "map_size": v.map_size, "fog_of_war": v.fog_of_war,
        "max_tick_count": v.max_tick_count, "max_pathfind_nodes": v.max_pathfind_nodes,
        "current_tick": v.current_tick,
        "players": [{"id": p.id, "score": p.score, "resource": p.resource} for p in v.players],
        "entities": [entity_json(e) for e in v.entities],
    }
