"""Placements, tournament points and the ladder's rating: one fit over every rated game.

A strategy does not change between two builds, so its games are not a history to follow but a sample to explain: the
ratings are the numbers that explain every rated game best, all at once and each at full weight, whatever the order
they were played in (Bradley-Terry, as the chess engine rating lists do). The model is the Elo curve: a pair of
players in a game is a duel, and 400 points of difference expect 10 to 1. What is rated is a build (a strategy's
image): the first build of a strategy starts from `initial`, give or take `prior_sd`, and each later build from the
one before it, give or take `version_sd`, so a new version carries on from the old one and moves as fast as its own
games say."""
from __future__ import annotations

import math
from collections import Counter
from typing import Hashable, Iterable, Sequence

SCALE = 400 / math.log(10)      # rating points per unit of log-odds: 400 points = 10 to 1
MAX_STEP = 3 * SCALE            # a coordinate moves at most this far in one sweep (a lone 30:0 would jump otherwise)
TOLERANCE = 0.005               # rating points: the fit stops when no rating moves more in a sweep
MAX_SWEEPS = 5000


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


def pair_weight(players: int) -> float:
    """What one duel weighs in a game of `players`. The duels of one game are not independent (six of them in a game
    of four, read off a single order of finish), so each counts for less than a game of two: by as much as makes the
    game tell about a player what its order of finish tells (the information of a ranking of equals,
    (n - H_n) / n, over that of n - 1 independent duels). 1 for two players, 0.78 for three, 0.64 for four."""
    n = int(players)
    if n < 2:
        return 0.0
    harmonic = sum(1.0 / m for m in range(1, n + 1))
    return 4.0 * (n - harmonic) / (n * (n - 1))


def game_duels(seats: Sequence[tuple[Hashable, int]]) -> list[tuple[Hashable, Hashable, float, float]]:
    """The duels of one rated game: `seats` are (player, place) of its seats that hold a strategy. Returns
    (a, b, score, weight) per pair of different players, as `fit` takes them. A player in several seats (the built-in
    bot filling a table) is one player: its duels with another are averaged, and the game counts as one of as many
    players as it had different ones."""
    by_player: dict[Hashable, list[int]] = {}
    for player, place in seats:
        by_player.setdefault(player, []).append(place)
    players = list(by_player)
    weight = pair_weight(len(players))
    out = []
    for x, a in enumerate(players):
        for b in players[x + 1:]:
            results = [1.0 if pa < pb else 0.5 if pa == pb else 0.0 for pa in by_player[a] for pb in by_player[b]]
            out.append((a, b, weight * sum(results) / len(results), weight))
    return out


def expected(rating: float, opponent: float) -> float:
    """The share of duels `rating` is expected to win against `opponent` (the Elo curve)."""
    return 1.0 / (1.0 + math.exp((opponent - rating) / SCALE))


def fit(duels: Iterable[tuple[Hashable, Hashable, float, float]], chains: Iterable[Sequence[Hashable]],
        cfg: dict, start: dict | None = None) -> tuple[dict, dict]:
    """The ratings that explain the duels best, and how far each can be trusted.

    `duels`: (a, b, score, weight) per pair of players: `weight` is what their duels weigh together (pair_weight per
    game) and `score` the part of it that went to `a` (a win in full, a tie by half). `chains`: per strategy its
    builds, the first one first; every player of a duel is in exactly one chain. `cfg`: initial, prior_sd,
    version_sd. `start`: ratings to start from (the last fit: a few sweeps then do).

    Returns (rating, sd) by player. `sd` is one standard error of the rating given the others' ratings: what the
    player's own games and its place in the chain leave open."""
    mu0 = float(cfg.get("initial", 1200))
    prior_var = float(cfg.get("prior_sd", 350)) ** 2
    version_var = float(cfg.get("version_sd", 200)) ** 2
    chains = [list(c) for c in chains if c]
    keys = [k for chain in chains for k in chain]
    if not keys:
        return {}, {}
    index = {k: i for i, k in enumerate(keys)}
    if len(index) != len(keys):
        raise ValueError("a build is in two chains")
    n = len(keys)
    opponents: list[list[tuple[int, float, float]]] = [[] for _ in range(n)]     # (opponent, own score, weight)
    for a, b, score, weight in duels:
        if a == b or weight <= 0:
            continue                                        # a strategy against itself tells nothing
        i, j = index[a], index[b]
        opponents[i].append((j, float(score), float(weight)))
        opponents[j].append((i, float(weight) - float(score), float(weight)))
    before = [-1] * n            # the build before this one in its chain, the one after it
    after = [-1] * n
    firsts = []
    for chain in chains:
        firsts.append(index[chain[0]])
        for prev, cur in zip(chain, chain[1:]):
            before[index[cur]], after[index[prev]] = index[prev], index[cur]
    r = [float((start or {}).get(k, mu0)) for k in keys]
    for i in range(n):           # a build never fitted starts where its chain stands
        if keys[i] not in (start or {}) and before[i] >= 0:
            r[i] = r[before[i]]

    def centre() -> None:
        """The duels and the chains know differences only, so the whole ladder slides freely: at the best fit the
        first builds average `initial` exactly. Put there after every sweep, or the slide takes hundreds of them."""
        shift = mu0 - sum(r[i] for i in firsts) / len(firsts)
        for i in range(n):
            r[i] += shift

    def curvature(i: int) -> tuple[float, float]:
        grad = hess = 0.0
        for j, score, weight in opponents[i]:
            p = 1.0 / (1.0 + math.exp((r[j] - r[i]) / SCALE))
            grad += (score - weight * p) / SCALE
            hess += weight * p * (1.0 - p) / (SCALE * SCALE)
        if before[i] < 0:
            grad -= (r[i] - mu0) / prior_var
            hess += 1.0 / prior_var
        else:
            grad -= (r[i] - r[before[i]]) / version_var
            hess += 1.0 / version_var
        if after[i] >= 0:
            grad -= (r[i] - r[after[i]]) / version_var
            hess += 1.0 / version_var
        return grad, hess

    centre()
    for _ in range(MAX_SWEEPS):
        moved = 0.0
        for i in range(n):
            grad, hess = curvature(i)
            step = max(-MAX_STEP, min(MAX_STEP, grad / hess))
            r[i] += step
            moved = max(moved, abs(step))
        centre()
        if moved < TOLERANCE:
            break
    return ({k: r[index[k]] for k in keys},
            {k: 1.0 / math.sqrt(curvature(index[k])[1]) for k in keys})
