from types import SimpleNamespace

import pytest

from codecraft.db import DB

RATING = {"initial": 1200, "prior_sd": 350, "version_sd": 200}
LEAGUES = {"count": 3, "settled_games": 2, "margin": 40, "min_size": 1}


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
    assert db.game(1)[0]["engine"] is None
    db.set_game_engine(1, "codecraft/sim-cpp:7390f3fc2f58")
    assert db.game(1)[0]["engine"] == "codecraft/sim-cpp:7390f3fc2f58"
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


def test_sandbox_games_since_counts_recent_sandbox_seats_only(tmp_path):
    from codecraft.db import since
    db = DB(tmp_path / "t.db")
    for n in ("a", "b"):
        db.upsert_strategy(strategy(n), 1200)
    seats = lambda: [dict(slot=0, strategy="a", image="img", score=1, crashed=False, comment=None, cpu_sec=None),
                     dict(slot=1, strategy="b", image="img", score=0, crashed=False, comment=None, cpu_sec=None)]
    gid = db.create_game("Finals", 1)
    db.finish_game(gid, "finished", None, 1.0, 1, seats(), RATING, [2, 0])
    custom = db.create_game("Finals", 2, kind="custom", rated=False)
    db.finish_game(custom, "finished", None, 1.0, 2, seats(), RATING, [2, 0])
    db.create_game("Finals", 3)                                   # running: counts, the seats are taken
    assert db.sandbox_games_since("a", since(24)) == 1
    assert db.sandbox_games_since("b", since(24)) == 1
    assert db.sandbox_games_since("a", since(-24)) == 0           # nothing in the future
    assert db.sandbox_games_since("c", since(24)) == 0


def play(db, fmt, seats, engine=None, leagues=None):
    """One rated game: seats are (strategy, image, score)."""
    gid = db.create_game(fmt, 1)
    if engine:
        db.set_game_engine(gid, engine)
    return db.finish_game(gid, "finished", None, 1.0, 1, [
        dict(slot=i, strategy=n, image=img, score=sc, crashed=False, comment=None, cpu_sec=None)
        for i, (n, img, sc) in enumerate(seats)], RATING, [8, 4, 2, 1] if len(seats) > 2 else [2, 0], leagues_cfg=leagues)


def test_a_new_build_carries_on_from_the_old_one_and_is_rated_by_its_own_games(tmp_path):
    db = DB(tmp_path / "t.db")
    db.upsert_strategy(strategy("q", builtin="QuickStart"), 1200)
    db.upsert_strategy(strategy("bot"), 1200)
    db.set_image("bot", "img:1", "1")
    for _ in range(6):
        play(db, "Finals", [("bot", "img:1", 0), ("q", None, 10)])
    old = db.strategy("bot")
    assert old["rating"] < 1200 and old["build_games"] == 6 and old["rating_sd"] < 350
    db.set_image("bot", "img:2", "2")                    # a new version: no game yet
    db.rerate(RATING)
    fresh = db.strategy("bot")
    assert abs(fresh["rating"] - old["rating"]) < 0.5 and fresh["build_games"] == 0 and abs(fresh["rating_sd"] - 200) < 1
    rows = play(db, "Finals", [("bot", "img:2", 10), ("q", None, 0)])
    assert abs(rows[0]["rating_before"] - old["rating"]) < 0.5 and rows[0]["rating_after"] > rows[0]["rating_before"] + 50
    for _ in range(5):
        play(db, "Finals", [("bot", "img:2", 10), ("q", None, 0)])
    assert db.strategy("bot")["rating"] > db.strategy("q")["rating"] and db.strategy("bot")["build_games"] == 6
    builds = {r["image"]: r for r in db.q("SELECT * FROM build_ratings WHERE strategy='bot'")}
    assert builds["img:1"]["rating"] < builds["img:2"]["rating"] and builds["img:1"]["games"] == 6
    assert db.strategy("bot")["games"] == 12              # the statistics are the strategy's, over all its builds


def test_rerate_with_history_gives_what_the_games_gave_as_they_were_played(tmp_path):
    db = DB(tmp_path / "t.db")
    for n in "abcd":
        db.upsert_strategy(strategy(n), 1200)
        db.set_image(n, f"img/{n}", n)
    scores = [(4, 3, 2, 1), (1, 4, 3, 2), (4, 4, 1, 0), (3, 1, 4, 2)]
    for sc in scores:
        play(db, "Round1", [(n, f"img/{n}", v) for n, v in zip("abcd", sc)])
    play(db, "Finals", [("a", "img/a", 5), ("d", "img/d", 1)])
    live = {r["name"]: r["rating"] for r in db.strategies()}
    seats = [(r["game_id"], r["slot"], r["rating_before"], r["rating_after"]) for r in
             db.q("SELECT * FROM game_players ORDER BY game_id, slot")]
    with db.tx() as c:
        c.execute("UPDATE game_players SET rating_before=1, rating_after=2")
        c.execute("UPDATE strategies SET rating=999")
        c.execute("DELETE FROM build_ratings")
        c.execute("DELETE FROM meta")
    assert db.ensure_ratings(RATING) is True and db.ensure_ratings(RATING) is False
    again = {r["name"]: r["rating"] for r in db.strategies()}
    assert all(abs(live[n] - again[n]) < 0.1 for n in live)
    assert live["a"] > live["b"] > live["d"] and abs(sum(live.values()) - 4800) < 1e-6
    for (g, slot, before, after), r in zip(seats, db.q("SELECT * FROM game_players ORDER BY game_id, slot")):
        assert abs(r["rating_before"] - before) < 0.1 and abs(r["rating_after"] - after) < 0.1


def test_the_builtin_bot_is_rated_per_engine_and_fills_seats_as_one_player(tmp_path):
    db = DB(tmp_path / "t.db")
    db.upsert_strategy(strategy("q", builtin="QuickStart"), 1200)
    db.upsert_strategy(strategy("bot"), 1200)
    db.set_image("bot", "img:1", "1")
    rows = play(db, "Round1", [("bot", "img:1", 5), ("q", None, 9), ("q", None, 3), ("q", None, 1)], engine="engine:1")
    # one game of two players: the bot stood above the engine's in two duels of three
    assert rows[0]["rating_after"] > 1200 > rows[1]["rating_after"] and rows[1]["rating_after"] == rows[2]["rating_after"]
    for _ in range(4):
        play(db, "Finals", [("bot", "img:1", 9), ("q", None, 1)], engine="engine:1")
    weak = db.strategy("q")["rating"]
    for _ in range(4):
        play(db, "Finals", [("bot", "img:1", 1), ("q", None, 9)], engine="engine:2")     # the engine's bot got better
    builds = {r["image"]: r for r in db.q("SELECT * FROM build_ratings WHERE strategy='q'")}
    assert set(builds) == {"engine:1", "engine:2"} and builds["engine:2"]["rating"] > builds["engine:1"]["rating"]
    assert db.strategy("q")["rating"] == builds["engine:2"]["rating"] > weak and db.strategy("q")["build_games"] == 4


def test_leagues_follow_the_settled_ratings_with_a_margin(tmp_path):
    from datetime import datetime, timezone
    db = DB(tmp_path / "t.db")
    names = ["top", "mid", "low", "new"]
    for n in names:
        db.upsert_strategy(strategy(n), 1200)
        db.set_image(n, f"img/{n}", n)
    with db.tx() as c:
        for n, rating, games in (("top", 1600, 30), ("mid", 1100, 30), ("low", 500, 30), ("new", 1650, 1)):
            c.execute("UPDATE strategies SET rating=?, build_games=? WHERE name=?", (rating, games, n))
    monday = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
    state = db.update_leagues(LEAGUES, at=monday)
    assert state["lines"] == [1350, 800] and state["week"] == "2026-W46"
    league = lambda n: db.strategy(n)["league"]
    assert [league(n) for n in names] == [0, 1, 2, None]         # a strategy still placing has no league
    with db.tx() as c:
        c.execute("UPDATE strategies SET rating=830 WHERE name='low'")
    db.update_leagues(LEAGUES, at=monday)
    assert league("low") == 2 and db.league_state()["lines"] == [1350, 800]   # over the line, within the margin
    with db.tx() as c:
        c.execute("UPDATE strategies SET rating=845, build_games=30 WHERE name='low'")
        c.execute("UPDATE strategies SET build_games=30 WHERE name='new'")
    db.update_leagues(LEAGUES, at=monday)
    assert league("low") == 1 and league("new") == 0             # past the margin; and the newcomer has settled
    assert db.league_state()["lines"] == [1350, 800]             # the lines stay for the week
    with db.tx() as c:
        c.execute("UPDATE strategies SET build_games=1 WHERE name='low'")     # a new version of a placed strategy
        c.execute("UPDATE strategies SET rating=400 WHERE name='low'")
    db.update_leagues(LEAGUES, at=monday)
    assert league("low") == 1                                    # keeps the league while it plays its placement games
    state = db.update_leagues(LEAGUES, at=datetime(2026, 11, 16, 0, 5, tzinfo=timezone.utc))
    assert state["week"] == "2026-W47" and state["lines"] != [1350, 800]      # the next Monday: drawn anew
    assert db.update_leagues(LEAGUES, force=True, at=monday)["week"] == "2026-W46"


def test_sandbox_state_placement_then_the_interval(tmp_path):
    db = DB(tmp_path / "t.db")
    for n in ("a", "b"):
        db.upsert_strategy(strategy(n), 1200)
        db.set_image(n, f"img/{n}", n)
    worker = {"sandbox_games_per_build": 3, "sandbox_interval_hours": 1, "sandbox_interval_max_hours": 24}
    state = db.sandbox_state(worker, "a", "img/a")
    assert state["placing"] == 3 and state["overdue"] is None and state["games"] == 0 and state["last_game_at"] is None
    for fmt in ("Finals", "Round1", "Finals"):
        play(db, fmt, [("a", "img/a", 2), ("b", "img/b", 1)])
    state = db.sandbox_state(worker, "a", "img/a")
    assert state["placing"] == 0 and state["games"] == 3 and state["formats"] == {"Finals": 2, "Round1": 1}
    assert state["interval_hours"] == 1 and -1.0 <= state["overdue"] < -0.99     # played just now: due in an hour
    with db.tx() as c:
        c.execute("UPDATE games SET created_at='2026-01-01 00:00:00'")
    state = db.sandbox_state(worker, "a", "img/a")
    assert state["interval_hours"] == 24 and state["overdue"] > 0                # an old build: once a day, and due
    assert db.sandbox_state({**worker, "sandbox_interval_hours": 0}, "a", "img/a")["overdue"] is None
    assert db.sandbox_state(worker, "a", "img/a2")["placing"] == 3               # a new build starts over
    assert db.sandbox_duels("a", "img/a", ["Finals"]) == {"b": 2} and db.sandbox_duels("a", "img/a", []) == {}


def test_reset_ratings_takes_the_played_games_out_of_the_fit(tmp_path):
    db = DB(tmp_path / "t.db")
    for n in ("a", "b"):
        db.upsert_strategy(strategy(n), 1200)
        db.set_image(n, f"img/{n}", n)
    play(db, "Finals", [("a", "img/a", 2), ("b", "img/b", 1)], leagues=LEAGUES)
    assert db.strategy("a")["rating"] > 1200
    db.reset_ratings(1200)
    db.rerate(RATING)
    assert db.strategy("a")["rating"] == 1200 and db.strategy("a")["games"] == 0 and db.strategy("a")["league"] is None
