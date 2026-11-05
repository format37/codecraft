from codewars import rating as R


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


def test_elo_is_zero_sum_for_equal_k_and_rewards_winner():
    ratings = [1200.0, 1200.0, 1200.0, 1200.0]
    deltas = R.elo_deltas(ratings, [1, 2, 3, 4], [32.0] * 4)
    assert abs(sum(deltas)) < 1e-9
    assert deltas[0] > deltas[1] > deltas[2] > deltas[3]
    assert deltas[0] == -deltas[3]


def test_elo_underdog_gains_more():
    deltas = R.elo_deltas([1000.0, 1400.0], [1, 2], [32.0, 32.0])
    assert deltas[0] > 16  # beating a much stronger opponent is worth more than half K


def test_k_factor_decays_to_min():
    cfg = {"k": 32, "k_min": 16, "k_half_life_games": 50}
    assert R.k_factor(cfg, 0) == 32
    assert abs(R.k_factor(cfg, 50) - 16) < 1e-9
    assert R.k_factor(cfg, 500) == 16
    assert R.k_factor({"k": 32, "k_min": 16, "k_half_life_games": 0}, 1000) == 32
