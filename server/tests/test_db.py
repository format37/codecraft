from types import SimpleNamespace

import pytest

from codecraft.db import DB

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


def test_unrated_game_keeps_ratings_and_stats(tmp_path):
    db = DB(tmp_path / "t.db")
    db.upsert_strategy(strategy("a"), 1200)
    db.upsert_strategy(strategy("b"), 1200)
    gid = db.create_game("Finals", None, kind="custom", rated=False)
    rows = db.finish_game(gid, "finished", None, 1.0, 5, [
        dict(slot=0, strategy="a", image="img", score=10, crashed=False, comment=None, cpu_sec=None, label="a-v1"),
        dict(slot=1, strategy="b", image="img", score=5, crashed=False, comment=None, cpu_sec=None),
    ], RATING, [2, 0], rated=False)
    assert rows[0]["place"] == 1 and rows[0]["rating_after"] is None
    assert db.strategy("a")["rating"] == 1200 and db.strategy("a")["games"] == 0
    assert db.game(gid)[1][0]["label"] == "a-v1"


def test_migration_adds_columns_to_an_old_database(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE strategies (name TEXT PRIMARY KEY, title TEXT, language TEXT, gpu INTEGER DEFAULT 0, builtin TEXT,
            image TEXT, build_hash TEXT, built_at TEXT, enabled INTEGER DEFAULT 1, rating REAL, games INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0, crashes INTEGER DEFAULT 0, points INTEGER DEFAULT 0, place_sum INTEGER DEFAULT 0);
        CREATE TABLE games (id INTEGER PRIMARY KEY AUTOINCREMENT, format TEXT NOT NULL, seed INTEGER, status TEXT NOT NULL,
            error TEXT, tag TEXT, created_at TEXT, finished_at TEXT, duration_sec REAL);
        CREATE TABLE game_players (game_id INTEGER NOT NULL, slot INTEGER NOT NULL, strategy TEXT, image TEXT, score INTEGER,
            place INTEGER, points INTEGER, crashed INTEGER, comment TEXT, cpu_sec REAL, rating_before REAL, rating_after REAL,
            PRIMARY KEY (game_id, slot));
        INSERT INTO strategies(name, rating) VALUES ('old', 1234);
        INSERT INTO games(format, seed, status) VALUES ('Round1', 42, 'finished');
    """)
    con.commit(); con.close()
    db = DB(path)
    assert db.strategy("old")["kind"] == "seed" and db.game(1)[0]["kind"] == "sandbox" and db.game(1)[0]["seed"] == "42"
    assert db.users() == [] and db.next_queued_game() is None


def test_rename_strategy_touches_rows_versions_and_seats(tmp_path):
    db = DB(tmp_path / "t.db")
    for n in ("old", "q"):
        db.upsert_strategy(strategy(n, builtin="QuickStart" if n == "q" else None), 1200)
    db.set_image("old", "codecraft/strategy-old:abc", "abc")
    vid = db.create_version("old", None, "zip", "submissions/x", None, "python", False, False, None, "first")
    gid = db.create_game("Finals", None)
    db.finish_game(gid, "finished", None, 1.0, 1, [
        dict(slot=0, strategy="old", image="codecraft/strategy-old:abc", score=1, crashed=False, comment=None, cpu_sec=None),
        dict(slot=1, strategy="q", image=None, score=0, crashed=False, comment=None, cpu_sec=None),
    ], RATING, [2, 0])
    with db.tx() as c:
        c.execute("UPDATE game_players SET label='old-v2' WHERE game_id=? AND slot=0", (gid,))
    # a fresh, unused row with the new name (as the seed sync creates it) is folded in
    db.upsert_strategy(strategy("new"), 1200)
    assert db.rename_strategy("old", "new") == [gid]
    assert db.strategy("old") is None
    row = db.strategy("new")
    assert row["image"] == "codecraft/strategy-old:abc" and row["games"] == 1
    _, players = db.game(gid)
    assert players[0]["strategy"] == "new" and players[0]["label"] == "new-v2"
    assert db.version(vid)["strategy"] == "new"
    with pytest.raises(ValueError):
        db.rename_strategy("new", "q")      # q has history (a seat)

