"""Sandbox matchmaking: which strategy is due a rated game, in which format, against whom.

A new build first plays its placement games, one after another. After them a strategy is due a game once an
interval: an hour while its build is fresh, longer for every week the build has been playing (as in 2020), a day at
most. The strategy that is due starts the game; the other seats go to strategies of similar rating, first of all to
those that are due a game themselves, so that in an hour everybody plays about once."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

WANTS_BOOST = 4.0       # a strategy that is due a game itself is this much likelier to get a seat in another's


@dataclass
class Candidate:
    name: str
    rating: float
    games: int = 0                  # sandbox games of the build that plays now
    builtin: bool = False
    gpu: bool = False
    placing: int = 0                # placement games the build still has to play
    overdue: float | None = None    # hours past its interval (>= 0: due a game); None: not due, or it never starts one

    @property
    def wants(self) -> bool:
        """Due a game of its own. The engine's bot never is: it only fills seats."""
        return not self.builtin and (self.placing > 0 or (self.overdue is not None and self.overdue >= 0))


def interval_hours(base: float, cap: float, weeks: int) -> float:
    """Hours between a strategy's rated games once its build is placed. The 2020 rule: about one game an hour, and
    for every N-th full week since the strategy's last version the interval grows by N base intervals (1, 2, 4, 7,
    11, 16, 22 hours), but never past `cap`."""
    w = max(0, int(weeks))
    return min(float(cap), float(base) * (1 + w * (w + 1) / 2))


def choose_format(weights: dict, rng: random.Random, played: dict | None = None) -> str:
    """A format with a positive weight. Without `played`, drawn by the weights. With it (the games a build has in
    each format), the one the build is furthest behind in, for its weight: a build's games are spread over the
    formats as the weights say, not as the dice fall."""
    names = [name for name, w in weights.items() if w and w > 0]
    if not names:
        raise ValueError("no game format has a positive weight")
    if played is None:
        return rng.choices(names, weights=[weights[n] for n in names])[0]
    return min(names, key=lambda n: (played.get(n, 0) / weights[n], rng.random()))


def pick_anchor(candidates: list[Candidate], rng: random.Random) -> Candidate | None:
    """Who starts the next game: the build furthest behind in its placement games, else the strategy longest
    overdue. None when nobody is due anything."""
    placing = [c for c in candidates if c.wants and c.placing > 0]
    if placing:
        return min(placing, key=lambda c: (c.games, rng.random()))
    due = [c for c in candidates if c.wants]
    if due:
        return max(due, key=lambda c: (c.overdue, rng.random()))
    return None


def pick_opponents(anchor: Candidate, candidates: list[Candidate], seats: int, sigma: float, rng: random.Random,
                   max_gpu: int | None = None, duels: dict | None = None) -> list[str]:
    """Up to `seats` - 1 distinct opponents for the anchor. `duels`: how often the anchor's build has met each
    strategy one on one (used in a game of two).

    A game of two in placement goes to the opponent met least so far, the closest rating first among equals: on a
    small ladder a new build so meets everybody. Otherwise opponents are drawn with weight
    exp(-|rating difference| / sigma), times WANTS_BOOST for one that is due a game itself, and in a game of two
    divided by 1 + the meetings so far. At most `max_gpu` GPU strategies share a game (None = unlimited)."""
    pool = [c for c in candidates if c.name != anchor.name]
    duels = duels or {}
    chosen: list[Candidate] = []
    gpu_used = int(anchor.gpu)
    while len(chosen) < seats - 1 and pool:
        if max_gpu is not None and gpu_used >= max_gpu:
            pool = [c for c in pool if not c.gpu]
            if not pool:
                break
        if seats == 2 and anchor.placing > 0:
            pick = min(pool, key=lambda c: (duels.get(c.name, 0), abs(c.rating - anchor.rating), rng.random()))
        else:
            weights = [math.exp(-abs(c.rating - anchor.rating) / max(sigma, 1e-9)) * (WANTS_BOOST if c.wants else 1.0)
                       / (1 + duels.get(c.name, 0) if seats == 2 else 1) + 1e-9 for c in pool]
            pick = rng.choices(pool, weights=weights)[0]
        chosen.append(pick)
        pool.remove(pick)
        gpu_used += int(pick.gpu)
    return [c.name for c in chosen]


def pick_players(candidates: list[Candidate], seats: int, sigma: float, rng: random.Random,
                 max_gpu: int | None = None, duels: dict | None = None, anchor: Candidate | None = None) -> list[str]:
    """The anchor (pick_anchor, or the one given) and its opponents, in a shuffled order (the seat decides the spawn
    corner). [] when nobody may start a game."""
    if seats <= 0:
        return []
    anchor = anchor or pick_anchor(candidates, rng)
    if anchor is None:
        return []
    names = [anchor.name] + pick_opponents(anchor, candidates, seats, sigma, rng, max_gpu, duels)
    rng.shuffle(names)
    return names
