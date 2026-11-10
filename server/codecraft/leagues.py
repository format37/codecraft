"""Leagues: the ladder cut into groups of like strength, by the ratings alone.

Once a week the settled ratings are clustered into `leagues.count` groups, the split with the least spread inside the
groups (k-means, exact in one dimension). A line between two leagues is the middle between their mean ratings, and it
stays where it is until the next clustering. A strategy stands in the league its rating falls in and changes league
as soon as its rating is past a line by `leagues.margin`, so a rating that sits on a line does not go to and fro.
League 0 is the strongest."""
from __future__ import annotations

from typing import Sequence


def cluster(ratings: Sequence[float], count: int, min_size: int = 1) -> list[list[float]]:
    """The ratings in at most `count` groups, the strongest first, with the least sum of squared distances to the
    groups' means. Fewer groups when there are not `count` different ratings, or not `min_size` ratings for each."""
    xs = sorted((float(x) for x in ratings), reverse=True)
    n = len(xs)
    min_size = max(1, int(min_size))
    k = max(1, min(int(count), len(set(xs)), n // min_size)) if n else 0
    if k == 0:
        return []
    prefix, prefix2 = [0.0], [0.0]
    for x in xs:
        prefix.append(prefix[-1] + x)
        prefix2.append(prefix2[-1] + x * x)

    def spread(i: int, j: int) -> float:
        """Sum of squares of xs[i:j] around their mean."""
        s = prefix[j] - prefix[i]
        return prefix2[j] - prefix2[i] - s * s / (j - i)

    inf = float("inf")
    cost = [[inf] * (n + 1) for _ in range(k + 1)]     # cost[g][j]: the first j ratings in g groups
    cut = [[0] * (n + 1) for _ in range(k + 1)]
    cost[0][0] = 0.0
    for g in range(1, k + 1):
        for j in range(g * min_size, n + 1):
            for i in range((g - 1) * min_size, j - min_size + 1):
                if cost[g - 1][i] == inf:
                    continue
                if g > 1 and xs[i - 1] == xs[i]:
                    continue                            # equal ratings are never on two sides of a line
                c = cost[g - 1][i] + spread(i, j)
                if c < cost[g][j]:
                    cost[g][j], cut[g][j] = c, i
    while k > 1 and cost[k][n] == inf:                  # equal ratings left no place for that many lines
        k -= 1
    groups, j = [], n
    for g in range(k, 0, -1):
        i = cut[g][j]
        groups.append(xs[i:j])
        j = i
    return groups[::-1]


def lines(ratings: Sequence[float], count: int, min_size: int = 1) -> list[float]:
    """The lines between the leagues, the highest first: `count` - 1 of them, or fewer (see cluster)."""
    means = [sum(g) / len(g) for g in cluster(ratings, count, min_size)]
    return [(a + b) / 2 for a, b in zip(means, means[1:])]


def league_of(rating: float, lines: Sequence[float]) -> int:
    """The league a rating falls in: 0 above the first line."""
    return sum(1 for line in lines if rating < line)


def place(rating: float, lines: Sequence[float], current: int | None = None, margin: float = 0.0) -> int:
    """The league of a strategy that stands in `current` (None: in none yet): it moves when its rating is past a line
    by more than `margin`."""
    if current is None or not 0 <= current <= len(lines):
        return league_of(rating, lines)
    league = current
    while league > 0 and rating > lines[league - 1] + margin:
        league -= 1
    while league < len(lines) and rating < lines[league] - margin:
        league += 1
    return league


def standings(rows: Sequence, lines: Sequence[float], settled_games: int) -> list[dict]:
    """The ladder (rows best first, each with rating, league, build_games) as the leaderboard shows it: one group
    per league, the strongest first, then the strategies that have no league yet. A group:
    {league (None: not placed yet), above (the line to cross to move up; None in league 0), below (the line under
    it; None in the last league), rows: [{place, placing, row}]}. `place` counts within the league, `placing` says
    that the build has not played its `settled_games` yet (its rating may still move a lot)."""
    groups = [{"league": i, "above": lines[i - 1] if i else None, "below": lines[i] if i < len(lines) else None,
               "rows": []} for i in range(len(lines) + 1)]
    unplaced = {"league": None, "above": None, "below": None, "rows": []}
    for r in rows:
        placing = (r["build_games"] or 0) < settled_games
        league = r["league"]
        if league is None and not placing and r["rating"] is not None:
            league = league_of(r["rating"], lines)          # settled, the placement has not run yet
        group = unplaced if league is None else groups[min(league, len(lines))]
        group["rows"].append({"place": len(group["rows"]) + 1, "placing": placing, "row": r})
    return groups + ([unplaced] if unplaced["rows"] else [])
