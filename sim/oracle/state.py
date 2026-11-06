"""States for the oracle: small hand-made ones and replay ticks, as `Model`s ready for a `LoadFrom` run.

    s = State(map_size=20, players=2, ticks=8)
    unit = s.add("BuilderUnit", player=1, at=(1, 1))
    s.order(unit, move((10, 10)))
    model = s.model

Players get ids 1..n and seats 0..n-1, like in a real game. Entity ids continue from n + 1 in the order of
`add`, and `next_id` follows them, so the ids the engine gives to new entities are predictable.
"""
from __future__ import annotations

from pathlib import Path

from codecraft_sim.model import (DEFAULT_PROPERTIES, AttackAction, AutoAttack, BuildAction, Entity, EntityAction,
                                 EntityType, Model, MoveAction, Player, RepairAction, Vec2)
from codecraft_sim import replay as replay_io


class State:
    def __init__(self, map_size: int = 20, players: int = 2, ticks: int = 10, fog: bool = False,
                 resource: int | list[int] = 0, score: int | list[int] = 0, max_pathfind_nodes: int = 1000,
                 passive_income: int = 0, current_tick: int = 0, properties: dict | None = None) -> None:
        res = resource if isinstance(resource, list) else [resource] * players
        sc = score if isinstance(score, list) else [score] * players
        self.model = Model(
            next_id=players + 1, map_size=map_size, fog_of_war=fog,
            entity_properties=dict(properties or DEFAULT_PROPERTIES),
            max_tick_count=current_tick + ticks, max_pathfind_nodes=max_pathfind_nodes,
            current_tick=current_tick, passive_income=passive_income,
            players={i + 1: Player(i + 1, i, sc[i], res[i]) for i in range(players)})

    def add(self, entity_type: EntityType | str, player: int | None, at: tuple[int, int], health: int | None = None,
            active: bool = True) -> int:
        """Place an entity (player None = no owner, as for resources); health defaults to the maximum."""
        t = EntityType.parse(entity_type)
        m = self.model
        eid = m.next_id
        m.next_id += 1
        m.entities[eid] = Entity(eid, player, t, Vec2(*at), m.entity_properties[t].max_health if health is None
                                 else health, active)
        return eid

    def resources(self, cells) -> list[int]:
        return [self.add(EntityType.RESOURCE, None, c) for c in cells]

    def order(self, entity_id: int, *actions: EntityAction) -> None:
        """Store an action for an entity, as if its player had sent it before the first tick."""
        self.model.entity_actions[entity_id] = combine(*actions)


def move(target: tuple[int, int], closest: bool = True, break_through: bool = False) -> EntityAction:
    return EntityAction(move_action=MoveAction(Vec2(*target), closest, break_through))


def attack(target: int | None = None, auto_range: int | None = None, valid=()) -> EntityAction:
    auto = None if auto_range is None else AutoAttack(auto_range, tuple(EntityType.parse(t) for t in valid))
    return EntityAction(attack_action=AttackAction(target, auto))


def build(entity_type: EntityType | str, at: tuple[int, int]) -> EntityAction:
    return EntityAction(build_action=BuildAction(EntityType.parse(entity_type), Vec2(*at)))


def repair(target: int) -> EntityAction:
    return EntityAction(repair_action=RepairAction(target))


NOTHING = EntityAction()


def combine(*actions: EntityAction) -> EntityAction:
    """One EntityAction with every part set in any of `actions` (the later one wins)."""
    out = EntityAction()
    for a in actions:
        out = EntityAction(a.move_action or out.move_action, a.build_action or out.build_action,
                           a.attack_action or out.attack_action, a.repair_action or out.repair_action)
    return out


def to_view(seat: int, pos: tuple[int, int], size: int, map_size: int) -> Vec2:
    """Where the seat sees a square of `size` cells at `pos`: the official engine rotates every PlayerView so that
    each seat sees its own corner at (0, 0) (seat 1 by 180 degrees, seat 2 by 90, seat 3 by 270; see the
    view-frames fixture), and turns the positions in the seat's actions back the same way."""
    x, y = pos
    n = map_size
    return Vec2(*((x, y), (n - x - size, n - y - size), (y, n - x - size), (n - y - size, x))[seat])


def from_view(seat: int, pos: tuple[int, int], size: int, map_size: int) -> Vec2:
    """The global position of a square the seat names at `pos` in its own view (the inverse of `to_view`)."""
    a, b = pos
    n = map_size
    return Vec2(*((a, b), (n - a - size, n - b - size), (n - b - size, a), (b, n - a - size))[seat])


def from_replay(path: str | Path, tick: int) -> Model:
    """The model of a stored game at `tick` (stored actions included), e.g. to replay one tick on the oracle."""
    for m in replay_io.load(path).states():
        if m.current_tick == tick:
            return m.copy()
    raise ValueError(f"{path} has no tick {tick}")
