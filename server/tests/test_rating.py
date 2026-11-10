import pytest

from codecraft import rating as R


def test_placements_with_ties():
    assert R.placements([50, 40, 50, 10]) == [1, 3, 1, 4]
    assert R.placements([0, 0]) == [1, 1]
    assert R.placements([5]) == [1]


def test_points_round1_table():
    table = [8, 4, 2, 1]
    assert R.points([1, 2, 3, 4], table) == [8, 4, 2, 1]
    # two players share 1st: (8+4)//2 = 6 each; the others get 3rd and 4th place points
    assert R.points([1, 1, 3, 4], table) == [6, 6, 2, 1]
    # everybody tied: (8+4+2+1)//4 = 3
    assert R.points([1, 1, 1, 1], table) == [3, 3, 3, 3]


def test_points_finals_table():
    assert R.points([1, 2], [2, 0]) == [2, 0]
    assert R.points([1, 1], [2, 0]) == [1, 1]


CFG = {"initial": 1200, "prior_sd": 350, "version_sd": 200}


def test_pair_weight_counts_a_duel_of_a_big_game_for_less():
    assert R.pair_weight(2) == 1.0
    assert round(R.pair_weight(3), 3) == 0.778 and round(R.pair_weight(4), 3) == 0.639
    assert R.pair_weight(1) == 0.0 and R.pair_weight(0) == 0.0


def test_game_duels_pairs_every_two_players_and_folds_repeated_seats():
    duels = R.game_duels([("a", 1), ("b", 2), ("c", 2), ("d", 4)])
    assert len(duels) == 6 and all(abs(w - R.pair_weight(4)) < 1e-12 for *_, w in duels)
    got = {(a, b): round(score / w, 2) for a, b, score, w in duels}
    assert got[("a", "b")] == 1.0 and got[("b", "c")] == 0.5 and got[("c", "d")] == 1.0
    # the built-in bot in three seats is one player: a game of two, its duels with the other averaged
    duels = R.game_duels([("me", 2), ("q", 1), ("q", 3), ("q", 4)])
    assert len(duels) == 1 and duels[0][3] == 1.0 and abs(duels[0][2] - 2 / 3) < 1e-12
    assert R.game_duels([("q", 1), ("q", 2)]) == [] and R.game_duels([("a", 1)]) == []


def test_fit_puts_the_winner_above_and_the_first_builds_around_the_initial_rating():
    rating, sd = R.fit([("a", "b", 3, 4)], [["a"], ["b"]], CFG)
    assert rating["a"] > 1200 > rating["b"] and abs(rating["a"] + rating["b"] - 2400) < 1e-6
    assert abs(sd["a"] - sd["b"]) < 1e-9 and sd["a"] < 350
    # the expected share at the fitted difference is the share won, pulled a little towards even by the prior
    assert 0.6 < R.expected(rating["a"], rating["b"]) < 0.75
    assert R.fit([], [], CFG) == ({}, {})
    rating, sd = R.fit([], [["x"]], CFG)
    assert rating == {"x": 1200} and abs(sd["x"] - 350) < 1e-9


def test_fit_does_not_depend_on_the_order_of_the_games_nor_on_where_it_starts():
    duels = [("a", "b", 7, 10), ("b", "c", 6, 9), ("a", "c", 9, 10), ("c", "d", 2.5, 5), ("a", "d", 5, 5)]
    chains = [["a"], ["b"], ["c"], ["d"]]
    one, _ = R.fit(duels, chains, CFG)
    two, _ = R.fit(duels[::-1], chains[::-1], CFG)
    warm, _ = R.fit(duels, chains, CFG, start={"a": 2000.0, "b": 300.0, "c": 1200.0, "d": 1200.0})
    assert all(abs(one[k] - two[k]) < 0.05 and abs(one[k] - warm[k]) < 0.05 for k in "abcd")
    assert one["a"] > one["b"] > one["c"] > one["d"]


def test_more_games_make_a_rating_surer_and_a_clean_sweep_stays_finite():
    _, few = R.fit([("a", "b", 3, 6)], [["a"], ["b"]], CFG)
    _, many = R.fit([("a", "b", 30, 60)], [["a"], ["b"]], CFG)
    assert many["a"] < few["a"] / 2
    rating, _ = R.fit([("a", "b", 30, 30)], [["a"], ["b"]], CFG)
    assert 400 < rating["a"] - rating["b"] < 1500


def test_a_new_build_starts_from_the_one_before_it_and_its_own_games_move_it():
    old = [("v1", "q", 2, 20), ("x", "q", 10, 20)]
    rating, sd = R.fit(old, [["v1", "v2"], ["q"], ["x"]], CFG)
    assert abs(rating["v2"] - rating["v1"]) < 0.05           # no games yet: where the strategy stands
    assert abs(sd["v2"] - 200) < 1e-6                         # give or take version_sd
    rating, sd = R.fit(old + [("v2", "q", 19, 20)], [["v1", "v2"], ["q"], ["x"]], CFG)
    assert rating["v2"] > rating["q"] + 200 and rating["q"] > rating["v1"] + 200     # the new build's games are its own
    assert sd["v2"] < 200
    with pytest.raises(ValueError):
        R.fit([], [["a"], ["a"]], CFG)
