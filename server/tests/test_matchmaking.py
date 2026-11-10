import random
from collections import Counter

from codecraft.matchmaking import Candidate, choose_format, interval_hours, pick_anchor, pick_opponents, pick_players


def cands():
    return [Candidate("a", 1200, 10, overdue=0.5), Candidate("b", 1210, 3, placing=7), Candidate("c", 1600, 10),
            Candidate("d", 800, 10), Candidate("e", 1190, 10)]


def test_interval_grows_with_the_weeks_as_in_2020():
    assert [interval_hours(1, 24, w) for w in range(8)] == [1, 2, 4, 7, 11, 16, 22, 24]
    assert interval_hours(1, 24, 50) == 24 and interval_hours(2, 24, 1) == 4 and interval_hours(1, 24, -3) == 1


def test_anchor_is_a_placing_build_first_then_the_longest_overdue():
    rng = random.Random(1)
    assert pick_anchor(cands(), rng).name == "b"                  # in placement: before anybody's hourly game
    pool = [Candidate("p1", 1200, 12, placing=18), Candidate("p2", 1200, 5, placing=25)]
    assert pick_anchor(pool, rng).name == "p2"                    # the build furthest behind
    pool = [Candidate("a", 1200, 40, overdue=0.2), Candidate("b", 1200, 40, overdue=3.0),
            Candidate("c", 1200, 40, overdue=-0.5), Candidate("d", 1200, 40)]
    assert pick_anchor(pool, rng).name == "b"
    assert pick_anchor(pool[2:], rng) is None                     # nobody is due
    assert pick_anchor([Candidate("q", 1000, 0, builtin=True, placing=30, overdue=5)], rng) is None   # the engine bot never starts


def test_pick_returns_distinct_names_of_requested_size():
    names = pick_players(cands(), 4, 200, random.Random(1))
    assert len(names) == 4 and len(set(names)) == 4 and "b" in names
    assert pick_players([Candidate("a", 1200, 40), Candidate("b", 1200, 40)], 4, 200, random.Random(0)) == []
    assert pick_players([], 4, 200, random.Random(0)) == []
    assert sorted(pick_players(cands()[:2], 4, 200, random.Random(0))) == ["a", "b"]


def test_opponents_prefer_close_ratings_and_those_due_a_game():
    anchor = Candidate("me", 1200, 40, overdue=1.0)
    pool = [anchor, Candidate("near", 1210, 40), Candidate("far", 1900, 40), Candidate("low", 500, 40)]
    counts = Counter()
    for seed in range(300):
        counts.update(pick_opponents(anchor, pool, 2, 100, random.Random(seed)))
    assert counts["near"] > 280
    pool = [anchor, Candidate("rested", 1200, 40), Candidate("due", 1200, 40, overdue=0.1)]
    counts = Counter()
    for seed in range(400):
        counts.update(pick_opponents(anchor, pool, 2, 200, random.Random(seed)))
    assert counts["due"] > 2.5 * counts["rested"]                 # so that in an hour everybody plays about once


def test_a_placement_game_of_two_goes_to_the_opponent_met_least():
    anchor = Candidate("new", 1200, 3, placing=27)
    pool = [anchor, Candidate("near", 1250, 40), Candidate("far", 1900, 40), Candidate("q", 500, 90, builtin=True)]
    rng = random.Random(0)
    assert pick_opponents(anchor, pool, 2, 200, rng, duels={}) == ["near"]                 # nobody met: the closest
    assert pick_opponents(anchor, pool, 2, 200, rng, duels={"near": 1}) == ["q"]           # then the next closest
    assert pick_opponents(anchor, pool, 2, 200, rng, duels={"near": 1, "q": 1}) == ["far"]  # everybody once
    assert pick_opponents(anchor, pool, 2, 200, rng, duels={"near": 1, "q": 1, "far": 1}) == ["near"]
    # once placed, a game of two is drawn again: close ratings, fewer meetings
    placed = Candidate("new", 1200, 30, overdue=0.0)
    counts = Counter()
    for seed in range(300):
        counts.update(pick_opponents(placed, pool, 2, 200, random.Random(seed), duels={"near": 9}))
    assert counts["near"] > counts["far"] and counts["near"] < 280


def test_choose_format_respects_zero_weight_and_evens_out_a_builds_formats():
    rng = random.Random(0)
    for _ in range(50):
        assert choose_format({"Round1": 0, "Round2": 1, "Finals": 0}, rng) == "Round2"
    weights = {"Round1": 1, "Round2": 1, "Finals": 1}
    assert choose_format(weights, rng, played={"Round1": 4, "Round2": 4, "Finals": 3}) == "Finals"
    assert choose_format(weights, rng, played={"Round1": 2, "Finals": 2}) == "Round2"
    assert choose_format({"Round1": 2, "Finals": 1}, rng, played={"Round1": 3, "Finals": 2}) == "Round1"
    played = Counter()
    for _ in range(30):
        played[choose_format(weights, rng, played=played)] += 1
    assert played == {"Round1": 10, "Round2": 10, "Finals": 10}


def test_gpu_cap_limits_gpu_players_per_game():
    pool = [Candidate("g1", 1200, 0, gpu=True, placing=5), Candidate("g2", 1200, 1, gpu=True), Candidate("c1", 1200, 1),
            Candidate("c2", 1200, 1), Candidate("c3", 1200, 1)]
    for seed in range(50):
        names = pick_players(pool, 4, 200, random.Random(seed), max_gpu=1)
        assert len(names) == 4 and sum(n.startswith("g") for n in names) == 1
    names = pick_players(pool[:2], 4, 200, random.Random(0), max_gpu=1)
    assert len(names) == 1  # nobody else fits beside the GPU anchor
    assert len(pick_players(pool, 4, 200, random.Random(0), max_gpu=0)) == 4  # anchor g1 still plays, others CPU


def test_strategies_that_are_not_due_only_fill_seats():
    pool = [Candidate("a", 1200, 40), Candidate("b", 1200, 40, overdue=-0.2), Candidate("q", 1200, 0, builtin=True)]
    assert pick_players(pool, 4, 200, random.Random(0)) == []
    pool.append(Candidate("new", 1200, 0, placing=30))
    names = pick_players(pool, 4, 200, random.Random(0))
    assert "new" in names and len(names) == 4
