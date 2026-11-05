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
    gpu: bool = False
    remaining: int | None = None    # sandbox games this build may still start (None = unlimited)


def choose_format(weights: dict, rng: random.Random) -> str:
    names = [name for name, w in weights.items() if w and w > 0]
    if not names:
        raise ValueError("no game format has a positive weight")
    return rng.choices(names, weights=[weights[n] for n in names])[0]


def pick_players(candidates: list[Candidate], seats: int, sigma: float, rng: random.Random,
                 max_gpu: int | None = None) -> list[str]:
    """Pick up to `seats` distinct names. The anchor is the candidate with the fewest games;
    the others are drawn with weight exp(-|rating difference| / sigma). Only a candidate with games
    `remaining` (or unlimited) can be the anchor, i.e. start a game; the others only fill seats. At most
    `max_gpu` GPU strategies share a game (None = unlimited). Order is shuffled (the seat decides the
    spawn corner). Returns [] when nobody may start a game."""
    pool = list(candidates)
    if not pool or seats <= 0:
        return []
    eligible = [c for c in pool if c.remaining is None or c.remaining > 0]
    if not eligible:
        return []   # everybody has played their share; nobody starts a game
    anchor = min(eligible, key=lambda c: (c.games, rng.random()))
    chosen = [anchor]
    pool.remove(anchor)
    gpu_used = int(anchor.gpu)
    while len(chosen) < seats and pool:
        if max_gpu is not None and gpu_used >= max_gpu:
            pool = [c for c in pool if not c.gpu]
            if not pool:
                break
        weights = [math.exp(-abs(c.rating - anchor.rating) / max(sigma, 1e-9)) + 1e-9 for c in pool]
        pick = rng.choices(pool, weights=weights)[0]
        chosen.append(pick)
        pool.remove(pick)
        gpu_used += int(pick.gpu)
    names = [c.name for c in chosen]
    rng.shuffle(names)
    return names
