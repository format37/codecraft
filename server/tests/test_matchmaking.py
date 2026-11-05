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


def test_gpu_cap_limits_gpu_players_per_game():
    pool = [Candidate("g1", 1200, 0, gpu=True), Candidate("g2", 1200, 1, gpu=True), Candidate("c1", 1200, 1),
            Candidate("c2", 1200, 1), Candidate("c3", 1200, 1)]
    for seed in range(50):
        names = pick_players(pool, 4, 200, random.Random(seed), max_gpu=1)
        assert len(names) == 4 and sum(n.startswith("g") for n in names) == 1
    names = pick_players(pool[:2], 4, 200, random.Random(0), max_gpu=1)
    assert len(names) == 1  # nobody else fits beside the GPU anchor
    assert len(pick_players(pool, 4, 200, random.Random(0), max_gpu=0)) == 4  # anchor g1 still plays, others CPU


def test_only_candidates_with_remaining_games_start_a_game():
    pool = [Candidate("a", 1200, 10, remaining=0), Candidate("b", 1200, 10, remaining=0), Candidate("q", 1200, 0, builtin=True, remaining=0)]
    assert pick_players(pool, 4, 200, random.Random(0)) == []
    pool.append(Candidate("new", 1200, 0, remaining=3))
    names = pick_players(pool, 4, 200, random.Random(0))
    assert "new" in names and len(names) == 4   # exhausted strategies still fill the seats
