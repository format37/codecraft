"""The built-in QuickStart bot: the starter strategy of github.com/format37/codecraft-starter (python/my_strategy.py,
commit cd13533), asked in-process with the same view a client gets (in its seat's frame) and answering in that
frame. The official engine's own bot (`quickstart_player`) is closed; this is a different player.

Deliberately minimal: with no house, a builder base and the resource for a house, every builder goes to build one
at a fixed spot right of the base; while the house is below full health, every builder goes to repair it; otherwise
every builder steps right, mining a resource in that cell. Orders go to the builders only: the bases train nobody
and nothing fights. The starter's debug drawing and log are left out (they change no order). Deterministic: the
same view gives the same orders.
"""
from __future__ import annotations

from .model import BuildAction, EntityAction, EntityType, MoveAction, PlayerView, RepairAction, Vec2

T = EntityType


def get_action(view: PlayerView) -> dict[int, EntityAction]:
    me = view.my_id
    house_props = view.entity_properties[T.HOUSE]
    resources = next(p.resource for p in view.players if p.id == me)
    mine = [e for e in view.entities if e.player_id == me]
    builders = [e for e in mine if e.entity_type == T.BUILDER_UNIT]
    base = next((e for e in mine if e.entity_type == T.BUILDER_BASE), None)
    house = next((e for e in mine if e.entity_type == T.HOUSE), None)

    # one house, right of the builder base (which is 5x5)
    house_spot = None
    if house is None and base is not None and resources >= house_props.initial_cost:
        house_spot = Vec2(base.position.x + 5, base.position.y + 4)

    # of an entity's actions the first one possible this tick is done, in the order attack, build, repair, move:
    # "build (or repair), else move" walks a builder to the house and starts the job once it is next to it
    result: dict[int, EntityAction] = {}
    for b in builders:
        if house_spot is not None:
            result[b.id] = EntityAction(MoveAction(house_spot, True, False), BuildAction(T.HOUSE, house_spot), None,
                                        None)
        elif house is not None and house.health < house_props.max_health:
            result[b.id] = EntityAction(MoveAction(house.position, True, False), None, None, RepairAction(house.id))
        else:   # a step right: a move into a cell with a resource mines it
            result[b.id] = EntityAction(MoveAction(Vec2(b.position.x + 1, b.position.y), True, False), None, None,
                                        None)
    return result
