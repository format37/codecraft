"""The worker's matchmaking against a real database: placement first, then a game an interval for those that are due."""
import random
from types import SimpleNamespace

from codecraft.config import SEATS, Config
from codecraft.db import DB
from codecraft.worker import Worker


def make(tmp_path, **worker):
    cfg = Config(Config.load(tmp_path).data, tmp_path)
    cfg["worker"].update({"sandbox_games_per_build": 3, "sandbox_interval_hours": 1, **worker})
    db = DB(tmp_path / "t.db")
    seed = lambda name, builtin=None: SimpleNamespace(name=name, title=name, language=None if builtin else "python",
                                                      gpu=False, builtin=builtin, enabled=True)
    db.upsert_strategy(seed("quickstart", "QuickStart"), 1200)
    for n in ("a", "b", "c"):
        db.upsert_strategy(seed(n), 1200)
        db.set_image(n, f"img/{n}:1", "1")
    w = object.__new__(Worker)              # the matchmaking needs neither docker nor a builder
    w.cfg, w.db, w.seeds, w.gpu, w.rng, w.sandbox = cfg, db, {}, False, random.Random(5), True
    return w, db, cfg


def play(w, db, cfg, fmt, players):
    gid = db.create_game(fmt, 1)
    seats = [dict(slot=i, strategy=p.strategy.name, image=p.image, score=w.rng.randrange(1000), crashed=False,
                  comment=None, cpu_sec=None) for i, p in enumerate(players)]
    db.finish_game(gid, "finished", None, 1.0, 1, seats, cfg["rating"], cfg["points"][fmt], leagues_cfg=cfg["leagues"])
    return [p.strategy.name for p in players]


def games_of(db, name):
    return db.sandbox_games_of_build(name, f"img/{name}:1")


def test_new_builds_play_their_placement_games_then_rest_until_they_are_due(tmp_path):
    w, db, cfg = make(tmp_path)
    played = 0
    for _ in range(20):
        fmt, players = w.make_match()
        if not players:
            break
        assert len(players) == SEATS[fmt] and len({p.strategy.name for p in players if not p.strategy.is_builtin}) >= 1
        play(w, db, cfg, fmt, players)
        played += 1
    assert 3 <= played <= 9 and all(games_of(db, n) >= 3 for n in "abc")      # every build has its placement games
    assert w.make_match()[1] == []                                            # and nobody is due: they just played
    assert all(db.strategy(n)["build_games"] >= 3 for n in "abc")
    # two hours on, everybody is due a game; a seat in another's game counts
    with db.tx() as c:
        c.execute("UPDATE games SET created_at=datetime(created_at, '-2 hours')")
    before = {n: games_of(db, n) for n in "abc"}
    wave = 0
    for _ in range(10):
        fmt, players = w.make_match()
        if not players:
            break
        play(w, db, cfg, fmt, players)
        wave += 1
    assert 1 <= wave <= 3 and all(games_of(db, n) > before[n] for n in "abc")
    assert db.strategy("quickstart")["games"] > 0                             # the engine's bot only filled seats


def test_a_new_version_plays_its_placement_games_again_and_formats_even_out(tmp_path):
    w, db, cfg = make(tmp_path, sandbox_games_per_build=6)
    while True:
        fmt, players = w.make_match()
        if not players:
            break
        play(w, db, cfg, fmt, players)
    db.set_image("a", "img/a:2", "2")                                         # version 2 of a
    formats = []
    for _ in range(6):
        fmt, players = w.make_match()
        assert "a" in [p.strategy.name for p in players]                      # only a is due anything
        assert next(p.image for p in players if p.strategy.name == "a") == "img/a:2"
        formats.append(fmt)
        play(w, db, cfg, fmt, players)
    assert sorted(formats) == ["Finals", "Finals", "Round1", "Round1", "Round2", "Round2"]
    assert w.make_match()[1] == []
    duels = db.sandbox_duels("a", "img/a:2", ["Finals"])
    assert sum(duels.values()) == 2 and max(duels.values()) == 1              # two games of two, two different opponents


def test_without_an_interval_a_placed_strategy_plays_no_more(tmp_path):
    w, db, cfg = make(tmp_path, sandbox_games_per_build=1, sandbox_interval_hours=0)
    for _ in range(6):
        fmt, players = w.make_match()
        if not players:
            break
        play(w, db, cfg, fmt, players)
    with db.tx() as c:
        c.execute("UPDATE games SET created_at=datetime(created_at, '-30 days')")
    assert w.make_match()[1] == []
