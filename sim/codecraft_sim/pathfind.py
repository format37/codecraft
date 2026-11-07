"""Pathfinding for the rules: the search of a move order and of a hunting auto-attack.

Both are one A*, as the official engine runs it (aicup2020 1.3.0, `model::pathfind::pathfind`, read off its machine
code in step 4 of the engine plan):

- on the positions at the start of the tick (`Grid`); only the first step of the path is taken, and the path is
  planned anew every tick (fixtures `move-free`, `move-train`);
- the price of entering a cell (`cost`, from rules.py) is 1 for a free cell; an entity is an obstacle, the player's
  own units included, except that with `break_through` another player's entity or a resource costs the hits it takes
  to destroy it, ceil(health / damage) (`move-break-through-*`); under fog of war a cell the player does not see
  costs 1, whatever stands there;
- cells are expanded by the lowest estimate (cost so far + heuristic), a random draw deciding between equal
  estimates; a cell is re-priced only for a strictly cheaper path; at most `max_pathfind_nodes` cells are expanded;
- the path goes to the best cell the search reached (expanded or merely put on its heap): the lowest heuristic, then
  the cheapest, one at random among equals. For a move order the heuristic is the Manhattan distance to the target,
  so an unreachable target sends the unit to the closest cell it can get to and `find_closest_position` changes
  nothing (`move-unreachable`, `move-node-cap`); for a hunt it is 0 on a cell next to a valid enemy and 1 elsewhere,
  and cells beyond the hunt's reach are left out.

Every draw comes from the turn's random generator. `prefer` (used by the corpus check to follow a record) makes the
paths through a given first cell win every draw they take part in.
"""
from __future__ import annotations

import heapq
import random
from typing import Callable, Iterable

from .model import Vec2

FREE = -1


class Grid:
    """Which entity stands on each cell of the map (`FREE` when none). Cells are indexed `y * size + x`."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.cells = [FREE] * (size * size)

    @classmethod
    def of(cls, model) -> "Grid":
        g = cls(model.map_size)
        for e in model.entities.values():
            g.place(e.id, e.position, model.entity_properties[e.entity_type].size)
        return g

    def index(self, pos: Vec2) -> int:
        return pos.y * self.size + pos.x

    def inside(self, x: int, y: int) -> bool:
        return 0 <= x < self.size and 0 <= y < self.size

    def square(self, pos: Vec2, size: int) -> Iterable[int]:
        """The cells of a square, those on the map."""
        n = self.size
        for y in range(max(pos.y, 0), min(pos.y + size, n)):
            for x in range(max(pos.x, 0), min(pos.x + size, n)):
                yield y * n + x

    def place(self, eid: int, pos: Vec2, size: int) -> None:
        for i in self.square(pos, size):
            self.cells[i] = eid

    def clear(self, pos: Vec2, size: int) -> None:
        for i in self.square(pos, size):
            self.cells[i] = FREE

    def occupant(self, pos: Vec2) -> int:
        """The entity on a cell, `FREE` when none (or off the map)."""
        return self.cells[pos.y * self.size + pos.x] if self.inside(pos.x, pos.y) else FREE

    def is_free(self, pos: Vec2) -> bool:
        return self.inside(pos.x, pos.y) and self.cells[pos.y * self.size + pos.x] == FREE

    def free_square(self, pos: Vec2, size: int) -> bool:
        """A square entirely on the map with no entity on it."""
        if pos.x < 0 or pos.y < 0 or pos.x + size > self.size or pos.y + size > self.size:
            return False
        return all(self.cells[i] == FREE for i in self.square(pos, size))

    def around(self, pos: Vec2, size: int, r: int) -> Iterable[int]:
        """The cells (on the map) at Manhattan distance at most `r` from a square, the square included."""
        n = self.size
        for x in range(max(pos.x - r, 0), min(pos.x + size + r, n)):
            gap = max(0, pos.x - x, x - (pos.x + size - 1))
            rest = r - gap
            for y in range(max(pos.y - rest, 0), min(pos.y + size + rest, n)):
                yield y * n + x


def _first_cell(parent: dict[int, int], start: int, end: int) -> int:
    i = end
    while parent[i] != start:
        i = parent[i]
    return i


def _pick(cands: list[int], parent: dict[int, int], start_i: int, rng: random.Random, prefer_i: int) -> int:
    """One of equally good end cells, at random (the official engine keeps each new one with probability 1/count:
    uniformly); with `prefer`, one whose path starts there if there is one."""
    if prefer_i >= 0:
        for c in cands:
            if c != start_i and _first_cell(parent, start_i, c) == prefer_i:
                return c
    return cands[0] if len(cands) == 1 else rng.choice(cands)


def _step(n: int, parent: dict[int, int], start_i: int, end: int) -> Vec2 | None:
    if end == start_i:
        return None
    j = _first_cell(parent, start_i, end)
    return Vec2(j % n, j // n)


def first_step(grid: Grid, start: Vec2, target: Vec2, max_nodes: int, cost: Callable[[int], int | None],
               rng: random.Random, prefer: Vec2 | None = None) -> Vec2 | None:
    """The first cell of the path from `start` towards `target`, or None to stay put (see the module docstring)."""
    n = grid.size
    tx, ty = target
    start_i = grid.index(start)
    target_i = ty * n + tx if grid.inside(tx, ty) else -1
    prefer_i = grid.index(prefer) if prefer is not None and grid.inside(*prefer) else -1
    first = {start_i: start_i}              # the first step of the path to each cell (kept only with `prefer`)
    g = {start_i: 0}
    parent = {start_i: start_i}
    heap = [(abs(start.x - tx) + abs(start.y - ty), 1, 0.0, start_i)]
    closed: set[int] = set()
    while heap:
        i = heapq.heappop(heap)[3]
        if i in closed:
            continue
        closed.add(i)
        if i == target_i:                   # nothing can beat the target (the official search goes on, in vain)
            return _step(n, parent, start_i, i)
        if len(closed) > max_nodes:
            break
        x, y = i % n, i // n
        gi = g[i]
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if not (0 <= nx < n and 0 <= ny < n):
                continue
            j = ny * n + nx
            c = cost(j)
            if c is None:
                continue
            ng = gi + c
            if ng < g.get(j, 1 << 60):
                g[j] = ng
                parent[j] = i
                tie = 1
                if prefer_i >= 0:
                    fj = first[j] = j if i == start_i else first[i]
                    tie = 0 if fj == prefer_i else 1
                heapq.heappush(heap, (ng + abs(nx - tx) + abs(ny - ty), tie, rng.random(), j))
    # the target was not reached: the closest cell reached, the cheaper path first, one at random among equals
    best_key, cands = None, []
    for i, gi in g.items():
        key = (abs(i % n - tx) + abs(i // n - ty), gi)
        if best_key is None or key < best_key:
            best_key, cands = key, [i]
        elif key == best_key:
            cands.append(i)
    return _step(n, parent, start_i, _pick(cands, parent, start_i, rng, prefer_i))


def hunt_step(grid: Grid, start: Vec2, goals: set[int], reach: int, max_nodes: int,
              cost: Callable[[int], int | None], rng: random.Random, prefer: Vec2 | None = None) -> Vec2 | None:
    """The first step towards the nearest goal cell (one next to a valid enemy) at most `reach` away, or None when
    there is none within reach or the unit stands on one (see the module docstring: heuristic 0 on a goal, 1
    elsewhere; one of the nearest goals at random)."""
    n = grid.size
    start_i = grid.index(start)
    if start_i in goals:
        return None
    prefer_i = grid.index(prefer) if prefer is not None and grid.inside(*prefer) else -1
    first = {start_i: start_i}
    g = {start_i: 0}
    parent = {start_i: start_i}
    heap = [(1, 1, 0.0, start_i)]
    closed: set[int] = set()
    found = None                            # the distance of the nearest goal reached
    while heap:
        f, _, _, i = heapq.heappop(heap)
        if found is not None and f > found:
            break                           # what is left cannot reach a goal that near
        if i in closed:
            continue
        closed.add(i)
        if len(closed) > max_nodes:
            break
        gi = g[i]
        if i in goals and found is None:
            found = gi
        x, y = i % n, i // n
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if not (0 <= nx < n and 0 <= ny < n):
                continue
            j = ny * n + nx
            c = cost(j)
            if c is None:
                continue
            ng = gi + c
            if ng <= reach and ng < g.get(j, 1 << 60):
                g[j] = ng
                parent[j] = i
                tie = 1
                if prefer_i >= 0:
                    fj = first[j] = j if i == start_i else first[i]
                    tie = 0 if fj == prefer_i else 1
                heapq.heappush(heap, (ng + (0 if j in goals else 1), tie, rng.random(), j))
    reached = [i for i in g if i in goals]
    if not reached:
        return None
    nearest = min(g[i] for i in reached)
    cands = [i for i in reached if g[i] == nearest]
    return _step(n, parent, start_i, _pick(cands, parent, start_i, rng, prefer_i))
