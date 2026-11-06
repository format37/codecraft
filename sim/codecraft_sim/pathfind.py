"""Pathfinding for the rules: the A* of a move order and the search of a hunting auto-attack.

Both run on the positions at the start of the tick (`Grid`): every entity is an obstacle, the player's own units
included, and only the first step of a path is used; the path is planned anew every tick (fixtures `move-free`,
`move-train`). A search expands at most `max_pathfind_nodes` cells; when it cannot reach its goal it heads for the
closest cell it found, which is why `find_closest_position` changes nothing (`move-unreachable`, `move-node-cap`).

With `break_through`, a cell holding another player's entity or a resource can be entered too, at the price of
the hits it takes to destroy it, ceil(health / damage), in place of the 1 of a free cell (`move-break-through-*`).

Where several paths are equally short the official engine picks one at random, a new draw every tick: from the
same state a unit may take either of the first steps of two shortest paths (the `alternatives` of `move-free`).
Ours draws from the turn's random generator; which paths the official draws among in every case (with the node
cap, around obstacles, with break_through) is still being checked against the fixtures (step 4 of the plan).
"""
from __future__ import annotations

import heapq
import random
from collections import deque
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


def first_step(grid: Grid, start: Vec2, target: Vec2, max_nodes: int, cost: Callable[[int], int | None],
               rng: random.Random) -> Vec2 | None:
    """The first cell of the path from `start` towards `target`, or None to stay put.

    A* with the Manhattan distance as heuristic; `cost(i)` is the price of entering cell `i`, None when it cannot
    be entered. The path ends at the target when the search reaches it within `max_nodes` expanded cells, else at
    the cell closest to the target among those expanded (the shorter path first). Among cells of equal estimate
    a random one is expanded first, so any of the shortest paths may come out (`move-train`)."""
    n = grid.size
    tx, ty = target
    start_i = grid.index(start)
    target_i = ty * n + tx if grid.inside(tx, ty) else -1
    g = {start_i: 0}
    parent = {start_i: start_i}
    h0 = abs(start.x - tx) + abs(start.y - ty)
    heap = [(h0, 0.0, start_i)]
    closed: set[int] = set()
    best, best_key = start_i, (h0, 0)
    while heap:
        i = heapq.heappop(heap)[2]
        if i in closed:
            continue
        closed.add(i)
        x, y = i % n, i // n
        gi = g[i]
        key = (abs(x - tx) + abs(y - ty), gi)
        if key < best_key:
            best, best_key = i, key
        if i == target_i or len(closed) > max_nodes:
            break
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
                heapq.heappush(heap, (ng + abs(nx - tx) + abs(ny - ty), rng.random(), j))
    if best == start_i:
        return None
    j = _first_cell(parent, start_i, best)
    return Vec2(j % n, j // n)


def nearest_step(grid: Grid, start: Vec2, goals: set[int], budget: int, max_nodes: int,
                 rng: random.Random) -> Vec2 | None:
    """The first step towards the nearest goal cell at most `budget` steps away over free cells (breadth first,
    the neighbours of each cell in a random order), or None when there is none (or the unit stands on one)."""
    n = grid.size
    start_i = grid.index(start)
    if start_i in goals or budget <= 0:
        return None
    cells = grid.cells
    parent = {start_i: start_i}
    frontier = deque([(start_i, 0)])
    expanded = 0
    while frontier:
        i, d = frontier.popleft()
        expanded += 1
        if expanded > max_nodes:
            break
        x, y = i % n, i // n
        around = [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
        rng.shuffle(around)
        for nx, ny in around:
            if not (0 <= nx < n and 0 <= ny < n):
                continue
            j = ny * n + nx
            if j in parent or cells[j] != FREE:
                continue
            parent[j] = i
            if j in goals:
                k = _first_cell(parent, start_i, j)
                return Vec2(k % n, k // n)
            if d + 1 < budget:
                frontier.append((j, d + 1))
    return None
