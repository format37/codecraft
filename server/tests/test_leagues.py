from codecraft import leagues as L

LADDER = [1702, 1627, 1529, 1461, 1421, 1304, 1078, 538, 355]


def test_cluster_finds_the_split_with_the_least_spread():
    assert L.cluster(LADDER, 3) == [[1702, 1627, 1529, 1461, 1421], [1304, 1078], [538, 355]]
    assert L.cluster(LADDER, 2) == [[1702, 1627, 1529, 1461, 1421, 1304, 1078], [538, 355]]
    assert [len(g) for g in L.cluster(LADDER, 3, min_size=3)] == [3, 3, 3]
    assert [len(g) for g in L.cluster(LADDER, 3, min_size=4)] == [5, 4]      # not enough ratings for three of four


def test_cluster_with_few_or_equal_ratings():
    assert L.cluster([], 3) == [] and L.lines([], 3) == []
    assert L.cluster([5], 3) == [[5]] and L.lines([5], 3) == []
    assert L.cluster([5, 5, 5], 3) == [[5, 5, 5]]                             # equal ratings are never split
    assert L.cluster([5, 7, 5], 3) == [[7], [5, 5]]
    assert L.cluster([3, 1, 2], 3) == [[3], [2], [1]]


def test_lines_are_halfway_between_the_leagues_means():
    lines = L.lines(LADDER, 3)
    assert [round(x, 1) for x in lines] == [1369.5, 818.8]
    assert [L.league_of(x, lines) for x in LADDER] == [0, 0, 0, 0, 0, 1, 1, 2, 2]


def test_place_moves_a_strategy_only_past_the_margin():
    lines = [1369.5, 818.75]
    assert L.place(840, lines, current=2, margin=40) == 2      # over the line, within the margin: it stays
    assert L.place(870, lines, current=2, margin=40) == 1
    assert L.place(800, lines, current=1, margin=40) == 1
    assert L.place(770, lines, current=1, margin=40) == 2
    assert L.place(2000, lines, current=2, margin=40) == 0     # two leagues at once
    assert L.place(840, lines, current=None, margin=40) == 1   # no league yet: where the rating falls
    assert L.place(900, lines, current=7, margin=40) == 1      # a league that is no more
    assert L.place(1000, [], current=0, margin=40) == 0 and L.place(1000, [], None) == 0


def test_standings_group_the_ladder_by_league_with_places_of_their_own():
    def row(name, rating, league, build_games=30):
        return {"name": name, "rating": rating, "league": league, "build_games": build_games}
    rows = [row("new", 1650, None, 4), row("a", 1600, 0), row("b", 1500, 0), row("re", 900, 1, 12), row("c", 500, 2),
            row("fresh", 480, None)]
    groups = L.standings(rows, [1369.5, 818.75], 30)
    assert [g["league"] for g in groups] == [0, 1, 2, None]
    assert [(e["row"]["name"], e["place"], e["placing"]) for e in groups[0]["rows"]] == [("a", 1, False), ("b", 2, False)]
    assert groups[0]["above"] is None and groups[0]["below"] == 1369.5 and groups[1]["above"] == 1369.5
    assert [(e["row"]["name"], e["placing"]) for e in groups[1]["rows"]] == [("re", True)]     # a new version keeps the league
    assert [e["row"]["name"] for e in groups[2]["rows"]] == ["c", "fresh"]     # settled, not placed yet: by its rating
    assert [e["row"]["name"] for e in groups[3]["rows"]] == ["new"]
    assert [g["league"] for g in L.standings([row("a", 1600, 0)], [], 30)] == [0]
