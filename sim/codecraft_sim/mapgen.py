"""The starting state of a `Create` game: the presets and our own map generator.

    model = create(preset("Round2"), seats=4, rng=random.Random(seed))
    model = create(options_from_json(config["game"]["Create"]["Custom"]), seats, rng)

The presets, as measured on the official replays: an 80 x 80 map, 1000 ticks, `max_pathfind_nodes` 1000, no
passive income, no starting resource, the property table of docs/doc-en.md; Round1 without fog of war and with
every base given, Round2 and Finals with fog of war and a builder base and a builder only. `Custom` names these
values itself (`max_tick_count` and `entity_properties` may be left out), as the official engine reads it.

Seats: the starting entities are given for seat 0's corner and turned for the others as the seat's view is turned
(view.py: seat 1 by 180 degrees, seat 2 by 90, seat 3 by 270), so that every seat sees its own entities where seat
0 sees its own, as on the official replays (every starting entity of the 36 stored Round1 games). Ids: the players 1
to n (the seat + 1), then the starting entities seat after seat in the order given, then the resources by x, then
y, as the official engine numbers them. Every entity starts active and at full health.

Resources come from our generator, not the official one, whose maps are only symmetric under a half turn (decision
5 of the plan: maps symmetric under both mirrors, fairer for four seats):

- the map is the same under both mirrors and under the diagonal (the eight symmetries of the square), so it is the
  same under the quarter turns too: every seat sees exactly the same field around its own corner, in its own frame;
- no resource within 2 cells of the box around a seat's starting entities, as on the official maps (all four
  corners are kept free, whatever the number of seats, so the map does not depend on it);
- the rest is gradient noise (two octaves, lattices 8 and 4 cells apart) cut at a share of the free cells drawn
  per map from 30 to 55 %. Measured on 40 maps per preset against the official ones (Round1, Round2): resources
  33 and 41 % of the map (official 32 and 40), 0.45 and 0.43 of them on an edge (0.47 and 0.43), blobs of 59 and
  74 cells on average (54 and 55) (`docs/private/engine-probes/step6/map_stats.py`);
- every corner is joined to the others by free cells (the official maps fail it 9 times in 87): the way through
  the fewest resources is cleared, with its images.

Integer arithmetic and the game's random.Random only (`getrandbits`), in a fixed order, so the C++ engine makes the
same map from the same seed.
"""
from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field

from .model import (DEFAULT_PROPERTIES, Entity, EntityProperties, EntityType, Model, Player, Vec2, properties_from_json,
                    vec_from_json)

T = EntityType
ROUND1_ENTITIES = ((T.BUILDER_BASE, Vec2(5, 5)), (T.BUILDER_UNIT, Vec2(4, 4)), (T.MELEE_BASE, Vec2(5, 15)),
                   (T.MELEE_UNIT, Vec2(4, 14)), (T.RANGED_BASE, Vec2(15, 5)), (T.RANGED_UNIT, Vec2(14, 4)),
                   (T.TURRET, Vec2(15, 15)))
ROUND2_ENTITIES = ((T.BUILDER_BASE, Vec2(5, 5)), (T.BUILDER_UNIT, Vec2(4, 4)))
MARGIN = 2                                  # free cells around the box of a seat's starting entities
SHARE_MIN, SHARE_MAX = 30, 55               # % of the free cells that hold a resource, drawn per map
OCTAVES = ((8, 2), (4, 1))                  # (lattice spacing in cells, weight) of the noise
DIRECTIONS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))
ONE = 1 << 16                               # fixed point: 1.0


@dataclass
class Options:
    map_size: int = 80
    initial_resource: int = 0
    passive_income: int = 0
    max_pathfind_nodes: int = 1000
    fog_of_war: bool = False
    max_tick_count: int = 1000
    starting_entities: tuple[tuple[EntityType, Vec2], ...] = ROUND1_ENTITIES
    entity_properties: dict[EntityType, EntityProperties] = field(default_factory=lambda: dict(DEFAULT_PROPERTIES))


PRESETS = {
    "Round1": dict(fog_of_war=False, starting_entities=ROUND1_ENTITIES),
    "Round2": dict(fog_of_war=True, starting_entities=ROUND2_ENTITIES),
    "Finals": dict(fog_of_war=True, starting_entities=ROUND2_ENTITIES),
}


def preset(name: str) -> Options:
    if name not in PRESETS:
        raise ValueError(f"unknown variant `{name}`, expected one of {', '.join(PRESETS)}, `Custom`")
    return Options(**PRESETS[name])


def options_from_json(d: dict) -> Options:
    """The `Custom` options of a config, as the official engine reads them."""
    for key in ("map_size", "initial_resource", "passive_income", "max_pathfind_nodes", "fog_of_war",
                "starting_entities"):
        if key not in d:
            raise ValueError(f"missing field `{key}`")
    props = dict(DEFAULT_PROPERTIES)
    if d.get("entity_properties") is not None:
        props = {T.parse(k): properties_from_json(v) for k, v in d["entity_properties"].items()}
    return Options(int(d["map_size"]), int(d["initial_resource"]), int(d["passive_income"]),
                   int(d["max_pathfind_nodes"]), bool(d["fog_of_war"]),
                   int(d["max_tick_count"]) if d.get("max_tick_count") is not None else 1000,
                   tuple((T.parse(t), vec_from_json(p)) for t, p in d["starting_entities"]), props)


def place(pos: Vec2, size: int, n: int, seat: int) -> Vec2:
    """Where `seat` gets a square of `size` cells given at `pos` for seat 0: the square turned as the seat's view is
    (the inverse of view.to_view)."""
    x, y = pos
    return (Vec2(x, y), Vec2(n - x - size, n - y - size), Vec2(n - y - size, x), Vec2(y, n - x - size))[seat]


def images(x: int, y: int, n: int) -> set[tuple[int, int]]:
    """A cell and its images under the eight symmetries of the square map."""
    out = set()
    for a, b in ((x, y), (y, x)):
        out.update(((a, b), (n - 1 - a, b), (a, n - 1 - b), (n - 1 - a, n - 1 - b)))
    return out


def create(options: Options, seats: int, rng: random.Random) -> Model:
    """The model at tick 0 for `seats` players."""
    if not 1 <= seats <= 4:
        raise ValueError(f"{seats} players: a map has four corners")
    o, props, n = options, options.entity_properties, options.map_size
    m = Model(1, n, o.fog_of_war, props, o.max_tick_count, o.max_pathfind_nodes, 0, o.passive_income)
    for seat in range(seats):
        m.players[seat + 1] = Player(seat + 1, seat, 0, o.initial_resource)
    next_id = seats + 1
    for seat in range(seats):
        for t, pos in o.starting_entities:
            size = props[t].size
            m.entities[next_id] = Entity(next_id, seat + 1, t, place(pos, size, n, seat), props[t].max_health, True)
            next_id += 1
    for x, y in resource_cells(o, rng):
        m.entities[next_id] = Entity(next_id, None, T.RESOURCE, Vec2(x, y), props[T.RESOURCE].max_health, True)
        next_id += 1
    m.next_id = next_id
    return m


def below(rng: random.Random, n: int) -> int:
    """A number in [0, n), uniform: as CPython's `_randbelow`, written out so that another engine can copy it."""
    k = n.bit_length()
    r = rng.getrandbits(k)
    while r >= n:
        r = rng.getrandbits(k)
    return r


def seat0_box(options: Options) -> tuple[int, int, int, int] | None:
    """The cells (x0, y0, x1, y1), inclusive, kept free around seat 0's starting entities."""
    props, ents = options.entity_properties, options.starting_entities
    if not ents:
        return None
    x0 = min(p.x for _, p in ents) - MARGIN
    y0 = min(p.y for _, p in ents) - MARGIN
    x1 = max(p.x + props[t].size - 1 for t, p in ents) + MARGIN
    y1 = max(p.y + props[t].size - 1 for t, p in ents) + MARGIN
    return x0, y0, x1, y1


def noise(q: int, rng: random.Random) -> list[int]:
    """Gradient noise on a q x q quarter (index y * q + x), in fixed point: per octave, a lattice every `spacing`
    cells (shifted by a random offset) with one of eight directions drawn at each point; a cell's value is the
    smoothed blend of the four surrounding points' gradients dotted with its offset from them (measured at the
    cell's centre). The octaves are weighted and summed."""
    out = [0] * (q * q)
    for spacing, weight in OCTAVES:
        ox, oy = below(rng, spacing), below(rng, spacing)
        cells = (q + spacing) // spacing + 2
        grads = [DIRECTIONS[rng.getrandbits(3)] for _ in range(cells * cells)]
        d = 2 * spacing
        frac = [((2 * a + 1) * ONE) // d for a in range(spacing)]          # the cell centre, in the lattice square
        fade = [((3 * c * c * d - 2 * c * c * c) * ONE) // d ** 3 for c in range(1, d, 2)]     # smoothstep of frac
        for y in range(q):
            j, b = divmod(y + oy, spacing)
            v, wy = frac[b], fade[b]
            for x in range(q):
                i, a = divmod(x + ox, spacing)
                u, wx = frac[a], fade[a]
                k = j * cells + i
                g00, g10, g01, g11 = grads[k], grads[k + 1], grads[k + cells], grads[k + cells + 1]
                n00 = g00[0] * u + g00[1] * v
                n10 = g10[0] * (u - ONE) + g10[1] * v
                n01 = g01[0] * u + g01[1] * (v - ONE)
                n11 = g11[0] * (u - ONE) + g11[1] * (v - ONE)
                nx0 = n00 + (((n10 - n00) * wx) >> 16)                  # >> rounds down, negative values too
                nx1 = n01 + (((n11 - n01) * wx) >> 16)
                out[y * q + x] += (nx0 + (((nx1 - nx0) * wy) >> 16)) * weight
    return out


def resource_cells(options: Options, rng: random.Random) -> list[tuple[int, int]]:
    """The cells of the resources, by x then y."""
    n = options.map_size
    q = (n + 1) // 2
    box = seat0_box(options)

    def kept_free(x: int, y: int) -> bool:
        if box is None:
            return False
        x0, y0, x1, y1 = box
        return any(x0 <= a <= x1 and y0 <= b <= y1 for a, b in images(x, y, n))

    values = noise(q, rng)
    # the eighth y <= x < q of the map stands for itself and its seven images
    candidates = [(x, y) for x in range(q) for y in range(x + 1) if not kept_free(x, y)]
    ranked = sorted(candidates, key=lambda c: (-values[c[1] * q + c[0]], c))
    share = SHARE_MIN + below(rng, SHARE_MAX - SHARE_MIN + 1)
    cells: set[tuple[int, int]] = set()
    for x, y in ranked[:len(ranked) * share // 100]:
        cells.update(images(x, y, n))
    _carve(options, cells)
    return sorted(cells)


def seat_box(box: tuple[int, int, int, int], n: int, seat: int) -> tuple[int, int, int, int]:
    """Seat 0's box of cells, turned for `seat`."""
    a, b = place(Vec2(box[0], box[1]), 1, n, seat), place(Vec2(box[2], box[3]), 1, n, seat)
    return min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y)


def _carve(options: Options, cells: set[tuple[int, int]]) -> None:
    """Join seat 0's zone to the zones of the corners beside it (seats 2 and 3) by the way through the fewest
    resources, taking those resources (and their images) off the map; by symmetry every corner is then joined to
    every other. Starting entities are in the way, as resources are not."""
    n, props = options.map_size, options.entity_properties
    box = seat0_box(options)
    if box is None:
        return
    taken: set[tuple[int, int]] = set()
    for seat in range(4):
        for t, pos in options.starting_entities:
            s = props[t].size
            p = place(pos, s, n, seat)
            taken.update((p.x + dx, p.y + dy) for dx in range(s) for dy in range(s))
    zones = []
    for seat in range(4):
        x0, y0, x1, y1 = seat_box(box, n, seat)
        zones.append({(x, y) for x in range(max(x0, 0), min(x1 + 1, n))
                      for y in range(max(y0, 0), min(y1 + 1, n)) if (x, y) not in taken})
    for target in (zones[2], zones[3]):
        if not zones[0] or not target or zones[0] & target:
            continue
        # a 0-1 search: a step onto a resource costs 1, onto a free cell 0
        dist = {c: 0 for c in zones[0]}
        parent: dict[tuple[int, int], tuple[int, int] | None] = {c: None for c in zones[0]}
        todo = deque((0, c) for c in sorted(zones[0]))
        end = None
        while todo:
            d, c = todo.popleft()
            if d > dist[c]:
                continue
            if c in target:
                end = c
                break
            x, y = c
            for nb in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if not (0 <= nb[0] < n and 0 <= nb[1] < n) or nb in taken:
                    continue
                w = 1 if nb in cells else 0
                if d + w < dist.get(nb, 1 << 30):
                    dist[nb], parent[nb] = d + w, c
                    if w:
                        todo.append((d + 1, nb))
                    else:
                        todo.appendleft((d, nb))
        while end is not None:
            if end in cells:
                cells.difference_update(images(end[0], end[1], n))
            end = parent[end]
