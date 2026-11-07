"""What a player is shown: the PlayerView of its seat, and the way back for the positions in its actions.

As the official engine 1.3.0 does it (`player_view`, `sight_range_map`, `convert_action`; fixtures `view-*`):

- the scalars of the game, the property table, and every player's id, score and resource, fog of war or not;
- without fog of war every entity; with it the player's own entities and those (resources included) with any cell
  within the sight range of one of them: the Manhattan distance between the nearest cells of the two squares,
  inclusive; inactive entities see too (`view-fog`, `view-fog-building-edge`, `view-fog-inactive`). The same sight
  map is what the player's path searches see (rules.py). A player owning nothing sees nothing (`view-no-entities`);
- the entities by increasing id, whatever the order of the state (`view-order`);
- in the seat's own frame: each seat sees its corner at (0, 0), seat 1 turned by 180 degrees, seat 2 by 90, seat 3
  by 270 (`view-frames`); the positions in the seat's actions, a move target and a build site, are turned back
  (`action_from_view`), and the game, the replay and the rules stay in the global frame.
"""
from __future__ import annotations

from .model import BuildAction, Entity, EntityAction, Model, MoveAction, PlayerView, Vec2, ViewPlayer
from .pathfind import Grid


def to_view(seat: int, pos: tuple[int, int], size: int, map_size: int) -> Vec2:
    """Where the seat sees a square of `size` cells at the global position `pos`."""
    x, y = pos
    n = map_size
    return Vec2(*((x, y), (n - x - size, n - y - size), (y, n - x - size), (n - y - size, x))[seat])


def from_view(seat: int, pos: tuple[int, int], size: int, map_size: int) -> Vec2:
    """The global position of a square the seat names at `pos` in its own frame (the inverse of `to_view`)."""
    a, b = pos
    n = map_size
    return Vec2(*((a, b), (n - a - size, n - b - size), (n - b - size, a), (b, n - a - size))[seat])


def sight_map(model: Model, player_id: int | None) -> bytearray:
    """1 for each cell (index y * map_size + x) the player sees: within the sight range of one of its entities,
    inactive ones included."""
    grid = Grid(model.map_size)
    seen = bytearray(model.map_size * model.map_size)
    props = model.entity_properties
    for e in model.entities.values():
        if e.player_id == player_id:
            p = props[e.entity_type]
            for i in grid.around(e.position, p.size, p.sight_range):
                seen[i] = 1
    return seen


def visible(model: Model, player_id: int, seen: bytearray | None = None) -> list[Entity]:
    """The entities the player is shown, by id: all of them without fog of war, else its own and those with a cell
    it sees (`seen`: its sight map, computed when not given)."""
    m = model
    if not m.fog_of_war:
        return [m.entities[i] for i in sorted(m.entities)]
    if seen is None:
        seen = sight_map(m, player_id)
    n = m.map_size
    out = []
    for i in sorted(m.entities):
        e = m.entities[i]
        if e.player_id == player_id:
            out.append(e)
            continue
        s = m.entity_properties[e.entity_type].size
        x0, y0 = e.position
        if any(seen[y * n + x] for y in range(max(y0, 0), min(y0 + s, n)) for x in range(max(x0, 0), min(x0 + s, n))):
            out.append(e)
    return out


def player_view(model: Model, player_id: int, seen: bytearray | None = None) -> PlayerView:
    """The view the player's seat is sent before the turn of `model.current_tick`."""
    m = model
    seat = m.players[player_id].index
    n = m.map_size
    entities = []
    for e in visible(m, player_id, seen):
        size = m.entity_properties[e.entity_type].size
        entities.append(Entity(e.id, e.player_id, e.entity_type, to_view(seat, e.position, size, n), e.health,
                               e.active))
    players = [ViewPlayer(p.id, p.score, p.resource) for _, p in sorted(m.players.items())]
    return PlayerView(player_id, n, m.fog_of_war, dict(m.entity_properties), m.max_tick_count,
                      m.max_pathfind_nodes, m.current_tick, players, entities)


def action_from_view(seat: int, action: EntityAction, model: Model) -> EntityAction:
    """A seat's EntityAction in the global frame: the move target turned back as a cell, the build site as a square
    of the size of the type to build; the rest (target ids, flags, types) as sent."""
    n = model.map_size
    mv, b = action.move_action, action.build_action
    if mv is not None:
        mv = MoveAction(from_view(seat, mv.target, 1, n), mv.find_closest_position, mv.break_through)
    if b is not None:
        size = model.entity_properties[b.entity_type].size
        b = BuildAction(b.entity_type, from_view(seat, b.position, size, n))
    return EntityAction(mv, b, action.attack_action, action.repair_action)
