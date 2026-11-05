"""Sandbox matchmaking: a least-played anchor plus opponents of similar rating."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass
class Candidate:
    name: str
    rating: float
    games: int
    builtin: bool = False


def choose_format(weights: dict, rng: random.Random) -> str:
    names = [name for name, w in weights.items() if w and w > 0]
    if not names:
        raise ValueError("no game format has a positive weight")
    return rng.choices(names, weights=[weights[n] for n in names])[0]


def pick_players(candidates: list[Candidate], seats: int, sigma: float, rng: random.Random) -> list[str]:
    """Pick up to `seats` distinct names. The anchor is the candidate with the fewest games;
    the others are drawn with weight exp(-|rating difference| / sigma). Order is shuffled
    (the seat decides the spawn corner)."""
    pool = list(candidates)
    if not pool or seats <= 0:
        return []
    anchor = min(pool, key=lambda c: (c.games, rng.random()))
    chosen = [anchor]
    pool.remove(anchor)
    while len(chosen) < seats and pool:
        weights = [math.exp(-abs(c.rating - anchor.rating) / max(sigma, 1e-9)) + 1e-9 for c in pool]
        pick = rng.choices(pool, weights=weights)[0]
        chosen.append(pick)
        pool.remove(pick)
    names = [c.name for c in chosen]
    rng.shuffle(names)
    return names
