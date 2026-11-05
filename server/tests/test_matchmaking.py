import random
from collections import Counter

from codewars.matchmaking import Candidate, choose_format, pick_players


def cands():
    return [Candidate("a", 1200, 10), Candidate("b", 1210, 3), Candidate("c", 1600, 10),
            Candidate("d", 800, 10), Candidate("e", 1190, 10)]


def test_pick_returns_distinct_names_of_requested_size():
    names = pick_players(cands(), 4, 200, random.Random(1))
    assert len(names) == 4 and len(set(names)) == 4
    assert "b" in names  # the least played strategy is always the anchor


def test_pick_prefers_close_ratings():
    counts = Counter()
    for seed in range(300):
        counts.update(pick_players(cands(), 2, 50, random.Random(seed)))
    assert counts["a"] + counts["e"] > counts["c"] + counts["d"]


def test_pick_with_small_pool_and_empty():
    assert pick_players([], 4, 200, random.Random(0)) == []
    assert sorted(pick_players(cands()[:2], 4, 200, random.Random(0))) == ["a", "b"]


def test_choose_format_respects_zero_weight():
    rng = random.Random(0)
    for _ in range(50):
        assert choose_format({"Round1": 0, "Round2": 1, "Finals": 0}, rng) == "Round2"
