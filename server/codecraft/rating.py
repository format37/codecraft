"""Placements, tournament points and the sandbox-style multiplayer Elo rating."""
from __future__ import annotations

from collections import Counter
from typing import Sequence


def placements(scores: Sequence[float]) -> list[int]:
    """1-based places by descending score; equal scores share the better place (1,1,3,4)."""
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    places = [0] * len(scores)
    place = 0
    for idx, i in enumerate(order):
        if idx == 0 or scores[i] != scores[order[idx - 1]]:
            place = idx + 1
        places[i] = place
    return places


def points(places: Sequence[int], table: Sequence[int]) -> list[int]:
    """Tournament points per place; tied players split the points of the shared places, rounded down."""
    count = Counter(places)
    result = []
    for place in places:
        k = count[place]
        total = sum(table[j] if j < len(table) else 0 for j in range(place - 1, place - 1 + k))
        result.append(total // k)
    return result


def k_factor(rating_cfg: dict, games_played: int) -> float:
    """K decays from `k` towards `k_min` with a half-life of `k_half_life_games` games."""
    k = float(rating_cfg.get("k", 32))
    k_min = float(rating_cfg.get("k_min", k))
    half_life = float(rating_cfg.get("k_half_life_games") or 0)
    if half_life <= 0:
        return k
    return max(k_min, k * 0.5 ** (games_played / half_life))


def elo_deltas(ratings: Sequence[float], places: Sequence[int], ks: Sequence[float]) -> list[float]:
    """Multiplayer Elo: every pair is scored as a separate duel, averaged over the n-1 opponents."""
    n = len(ratings)
    if n < 2:
        return [0.0] * n
    deltas = []
    for i in range(n):
        total = 0.0
        for j in range(n):
            if i == j:
                continue
            expected = 1.0 / (1.0 + 10 ** ((ratings[j] - ratings[i]) / 400.0))
            if places[i] < places[j]:
                actual = 1.0
            elif places[i] == places[j]:
                actual = 0.5
            else:
                actual = 0.0
            total += actual - expected
        deltas.append(ks[i] * total / (n - 1))
    return deltas
