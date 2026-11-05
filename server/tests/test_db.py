from types import SimpleNamespace

from codewars.db import DB

RATING = {"initial": 1200, "k": 32, "k_min": 16, "k_half_life_games": 50}


def strategy(name, builtin=None):
    return SimpleNamespace(name=name, title=name, language="python" if not builtin else None, gpu=False,
                           builtin=builtin, enabled=True)


def test_finish_game_updates_ratings_and_stats(tmp_path):
    db = DB(tmp_path / "t.db")
    for n in ("a", "b", "c"):
        db.upsert_strategy(strategy(n), 1200)
    gid = db.create_game("Round1", 11187186611345202053)  # a real engine seed, >= 2**63
    rows = db.finish_game(gid, "finished", None, 12.5, 11187186611345202053, [
        dict(slot=0, strategy="a", image="img", score=100, crashed=False, comment=None, cpu_sec=1.0),
        dict(slot=1, strategy="b", image="img", score=50, crashed=False, comment=None, cpu_sec=None),
        dict(slot=2, strategy="c", image="img", score=0, crashed=True, comment="boom", cpu_sec=None),
        dict(slot=3, strategy=None, image=None, score=0, crashed=False, comment=None, cpu_sec=None),
    ], RATING, [8, 4, 2, 1])
    assert [r["place"] for r in rows] == [1, 2, 3, 3]
    assert [r["points"] for r in rows] == [8, 4, 1, 1]
    a, b, c = (db.strategy(n) for n in "abc")
    assert a["rating"] > 1200 > c["rating"]
    assert a["games"] == b["games"] == c["games"] == 1
    assert a["wins"] == 1 and c["crashes"] == 1
    assert abs((a["rating"] + b["rating"] + c["rating"]) - 3600) < 1e-6
    game, players = db.game(gid)
    assert game["status"] == "finished" and game["seed"] == "11187186611345202053" and len(players) == 4
    assert players[3]["strategy"] is None and players[3]["rating_after"] is None


def test_aborted_game_changes_nothing(tmp_path):
    db = DB(tmp_path / "t.db")
    db.upsert_strategy(strategy("a"), 1200)
    db.upsert_strategy(strategy("q", builtin="QuickStart"), 1200)
    gid = db.create_game("Finals", None)
    db.finish_game(gid, "aborted", "engine died", 1.0, None, [
        dict(slot=0, strategy="a", image="img", score=0, crashed=False, comment=None, cpu_sec=None),
        dict(slot=1, strategy="q", image=None, score=0, crashed=False, comment=None, cpu_sec=None),
    ], RATING, [2, 0])
    assert db.strategy("a")["rating"] == 1200 and db.strategy("a")["games"] == 0
    assert db.game(gid)[0]["status"] == "aborted"
