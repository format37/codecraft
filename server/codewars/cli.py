"""Command line: build images, play games, run the sandbox loop, serve the UI."""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys

import docker

from .builder import BuildError, Builder
from .config import SEATS, Config
from .db import DB
from .game import GameRunner
from .loop import Sandbox
from .strategies import Strategy, load_strategies

log = logging.getLogger("codewars")


def _context(args):
    cfg = Config.load(args.root)
    cfg.ensure_dirs()
    handler = logging.FileHandler(cfg.data_dir / "codewars.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    db = DB(cfg.db_path)
    strategies = load_strategies(cfg)
    for s in strategies.values():
        db.upsert_strategy(s, float(cfg["rating"]["initial"]))
    return cfg, db, strategies


def _docker() -> docker.DockerClient:
    try:
        client = docker.from_env()
        client.ping()
        return client
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"cannot talk to docker: {exc}")


def _images(db: DB) -> dict[str, str]:
    return {r["name"]: r["image"] for r in db.strategies() if r["image"]}


def cmd_build(args):
    cfg, db, strategies = _context(args)
    client = _docker()
    builder = Builder(cfg, client)
    print(f"engine: {builder.build_engine(force=args.force)}")
    names = args.names or [n for n, s in strategies.items() if s.enabled and not s.is_builtin]
    failed = []
    for name in names:
        s = strategies.get(name)
        if s is None:
            sys.exit(f"unknown strategy {name!r}; known: {', '.join(strategies)}")
        if s.is_builtin:
            print(f"{name}: built-in, nothing to build")
            continue
        try:
            image, build_hash = builder.build_strategy(s, force=args.force, nocache=args.no_cache)
            db.set_image(name, image, build_hash)
            print(f"{name}: {image}")
        except BuildError as exc:
            failed.append(name)
            print(f"{name}: BUILD FAILED\n{exc}", file=sys.stderr)
    if failed:
        sys.exit(f"failed: {', '.join(failed)}")


def _resolve_players(names: list[str], strategies: dict[str, Strategy]) -> list[Strategy | None]:
    players = []
    for name in names:
        if name.lower() == "empty":
            players.append(None)
        elif name in strategies:
            players.append(strategies[name])
        else:
            sys.exit(f"unknown strategy {name!r}; known: {', '.join(strategies)} (or 'empty')")
    return players


def cmd_play(args):
    cfg, db, strategies = _context(args)
    client = _docker()
    runner = GameRunner(cfg, client, db)
    players = _resolve_players(args.players, strategies)
    seats = SEATS[args.format]
    if len(players) > seats:
        sys.exit(f"{args.format} has {seats} seats")
    filler = strategies.get("quickstart") if cfg["matchmaking"].get("fill_with_builtin", True) else None
    while len(players) < seats:
        players.append(filler)
    images = _images(db)
    for _ in range(args.repeat):
        outcome = runner.run(args.format, players, images, seed=args.seed, tag=args.tag)
        print(f"game #{outcome.game_id}: {outcome.status}" + (f" ({outcome.error})" if outcome.error else ""))
        for row in outcome.rows:
            rating = "" if row["rating_after"] is None else f"  rating {row['rating_before']:.0f} -> {row['rating_after']:.0f}"
            flag = f"  CRASHED: {row['comment']}" if row["crashed"] else ""
            cpu = "" if row["cpu_sec"] is None else f"  cpu {row['cpu_sec']:.1f}s"
            print(f"  {row['slot'] + 1}. {row['strategy'] or 'empty':<14} score {row['score']:>7}  place {row['place']}  "
                  f"points {row['points']}{rating}{cpu}{flag}")
        print(f"  replay: {outcome.dir / 'replay.dat'}")


def cmd_loop(args):
    cfg, db, strategies = _context(args)
    client = _docker()
    runner = GameRunner(cfg, client, db)
    removed = runner.cleanup_leftovers()
    if removed:
        log.info("removed %d leftover container(s)/network(s)", removed)
    stale = db.abort_stale_running()
    if stale:
        log.info("marked %d stale running game(s) as error", stale)
    Sandbox(cfg, db, runner, strategies, _images(db)).run(max_games=args.max_games)


def cmd_serve(args):
    import uvicorn

    from .web import create_app

    cfg, db, _ = _context(args)
    uvicorn.run(create_app(cfg, db), host=args.host or cfg["web"]["host"], port=args.port or int(cfg["web"]["port"]),
                log_level="info")


def cmd_ladder(args):
    _, db, _ = _context(args)
    rows = db.leaderboard()
    print(f"{'#':>2} {'strategy':<14} {'rating':>7} {'games':>5} {'wins':>4} {'avg pl':>6} {'points':>6} {'crash':>5}  image")
    for i, r in enumerate(rows, 1):
        avg = f"{r['place_sum'] / r['games']:.2f}" if r["games"] else "-"
        image = (r["image"] or "").split(":")[-1] or ("built-in" if r["builtin"] else "not built")
        print(f"{i:>2} {r['name']:<14} {r['rating']:>7.0f} {r['games']:>5} {r['wins']:>4} {avg:>6} {r['points']:>6} {r['crashes']:>5}  {image}")


def cmd_games(args):
    _, db, _ = _context(args)
    rows = db.games(limit=args.limit)
    players = db.game_players_for([g["id"] for g in rows])
    for g in rows:
        line = ", ".join(f"{p['strategy'] or 'empty'}={p['score']}" + ("!" if p["crashed"] else "") for p in players.get(g["id"], []))
        dur = f"{g['duration_sec']:.0f}s" if g["duration_sec"] else "-"
        print(f"#{g['id']:<5} {g['format']:<7} {g['status']:<9} {dur:>6}  {line}" + (f"  [{g['error']}]" if g["error"] else ""))


def cmd_cleanup(args):
    cfg, db, _ = _context(args)
    n = GameRunner(cfg, _docker(), db).cleanup_leftovers()
    print(f"removed {n} leftover container(s)/network(s)")
    stale = db.abort_stale_running()
    print(f"marked {stale} stale running game(s) as error")


def cmd_reset_ratings(args):
    cfg, db, _ = _context(args)
    db.reset_ratings(float(cfg["rating"]["initial"]))
    print("ratings and statistics reset (games history kept)")


def cmd_watch(args):
    cfg, _, _ = _context(args)
    replay = cfg.games_dir / f"{args.game_id:06d}" / "replay.dat"
    if not replay.exists():
        sys.exit(f"no replay for game {args.game_id}: {replay}")
    viewer = cfg.root / cfg["engine"]["binary"]
    cmd = [str(viewer), "--replay", str(replay)]
    print(" ".join(cmd))
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        sys.exit("no display in this environment; run the command above on a desktop")
    os.execv(cmd[0], cmd)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="codewars", description="CodeCraft 2020 sandbox server")
    parser.add_argument("--root", help="project root (default: $CW_ROOT or the repository)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build", help="build the engine image and strategy images")
    p.add_argument("names", nargs="*", help="strategies to build (default: all enabled)")
    p.add_argument("--force", action="store_true", help="rebuild even if the image is up to date")
    p.add_argument("--no-cache", action="store_true", help="docker build without layer cache")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("play", help="play one game")
    p.add_argument("--format", choices=sorted(SEATS), default="Round1")
    p.add_argument("--players", nargs="+", required=True, help="strategy names (or 'empty'); missing seats get QuickStart")
    p.add_argument("--seed", type=int)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--tag", default="manual")
    p.set_defaults(func=cmd_play)

    p = sub.add_parser("loop", help="run the sandbox loop")
    p.add_argument("--max-games", type=int)
    p.set_defaults(func=cmd_loop)

    p = sub.add_parser("serve", help="serve the web UI")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_serve)

    sub.add_parser("ladder", help="print the leaderboard").set_defaults(func=cmd_ladder)
    p = sub.add_parser("games", help="list recent games")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_games)
    sub.add_parser("cleanup", help="remove leftover game containers/networks").set_defaults(func=cmd_cleanup)
    sub.add_parser("reset-ratings", help="reset ratings and statistics").set_defaults(func=cmd_reset_ratings)
    p = sub.add_parser("watch", help="open a replay in the official viewer (needs a display)")
    p.add_argument("game_id", type=int)
    p.set_defaults(func=cmd_watch)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    args.func(args)
