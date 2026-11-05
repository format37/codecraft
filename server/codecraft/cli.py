"""Command line: build images, play games, run the worker, serve the UI, manage users."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import docker

from . import monitoring
from .auth import Auth, Mailer
from .builder import BuildError, Builder
from .config import SEATS, Config
from .db import DB
from .game import GameRunner, PlayerSpec
from .strategies import Strategy, load_strategies, strategy_from_row

log = logging.getLogger("codecraft")


def _context(args):
    cfg = Config.load(args.root)
    cfg.ensure_dirs()
    monitoring.init(cfg)
    handler = logging.FileHandler(cfg.data_dir / "codecraft.log")
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


def cmd_build(args):
    cfg, db, strategies = _context(args)
    client = _docker()
    builder = Builder(cfg, client)
    print(f"engine: {builder.build_engine(force=args.force)}")
    gpu_ok = cfg.resolve_gpu(client)
    names = args.names or [n for n, s in strategies.items() if s.enabled and not s.is_builtin]
    failed = []
    for name in names:
        s = strategies.get(name)
        if s is None:
            sys.exit(f"unknown strategy {name!r}; known: {', '.join(strategies)}")
        if s.is_builtin:
            print(f"{name}: built-in, nothing to build")
            continue
        if s.gpu and not gpu_ok and not args.names:
            print(f"{name}: skipped (needs a GPU, none available here)")
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


def _resolve_players(names: list[str], db: DB, seeds: dict[str, Strategy]) -> list[PlayerSpec]:
    players = []
    for name in names:
        if name.lower() == "empty":
            players.append(PlayerSpec(None))
            continue
        base, _, vnum = name.partition("@")
        row = db.strategy(base)
        if row is None:
            sys.exit(f"unknown strategy {name!r}; known: {', '.join(r['name'] for r in db.strategies())} (or 'empty')")
        strategy = strategy_from_row(row, seeds)
        image, version_id, label = row["image"], None, None
        if vnum:
            v = db.one("SELECT * FROM strategy_versions WHERE strategy=? AND number=?", base, int(vnum))
            if v is None or v["status"] != "ok":
                sys.exit(f"{name}: no successfully built version {vnum}")
            image, version_id, label = v["image"], v["id"], f"{base}-v{vnum}"
            strategy.gpu = bool(v["gpu"])
        players.append(PlayerSpec(strategy, None if strategy.is_builtin else image, version_id, label))
    return players


def cmd_play(args):
    cfg, db, seeds = _context(args)
    client = _docker()
    runner = GameRunner(cfg, client, db)
    players = _resolve_players(args.players, db, seeds)
    seats = SEATS[args.format]
    if len(players) > seats:
        sys.exit(f"{args.format} has {seats} seats")
    filler = None
    if cfg["matchmaking"].get("fill_with_builtin", True):
        row = db.strategy("quickstart")
        filler = PlayerSpec(strategy_from_row(row, seeds)) if row else None
    while len(players) < seats:
        players.append(filler or PlayerSpec(None))
    for _ in range(args.repeat):
        outcome = runner.run(args.format, players, seed=args.seed, tag=args.tag, kind="custom" if args.unrated else "sandbox",
                             rated=not args.unrated)
        print(f"game #{outcome.game_id}: {outcome.status}" + (f" ({outcome.error})" if outcome.error else ""))
        for row in outcome.rows:
            rating = "" if row["rating_after"] is None else f"  rating {row['rating_before']:.0f} -> {row['rating_after']:.0f}"
            flag = f"  CRASHED: {row['comment']}" if row["crashed"] else ""
            cpu = "" if row["cpu_sec"] is None else f"  cpu {row['cpu_sec']:.1f}s"
            print(f"  {row['slot'] + 1}. {row['label'] or row['strategy'] or 'empty':<16} score {row['score']:>7}  place {row['place']}  "
                  f"points {row['points']}{rating}{cpu}{flag}")
        print(f"  replay: {outcome.dir / 'replay.dat'}")


def cmd_worker(args):
    from .worker import Worker

    cfg, db, seeds = _context(args)
    client = _docker()
    Worker(cfg, db, client, seeds, sandbox=None if not args.no_sandbox else False,
           builds=None if not args.no_builds else False).run(max_games=args.max_games)


def cmd_serve(args):
    import uvicorn

    from .web import create_app

    cfg, db, _ = _context(args)
    uvicorn.run(create_app(cfg, db), host=args.host or cfg["web"]["host"], port=args.port or int(cfg["web"]["port"]),
                log_level="info", proxy_headers=True, forwarded_allow_ips="*")


def cmd_ladder(args):
    _, db, _ = _context(args)
    rows = db.leaderboard()
    print(f"{'#':>2} {'strategy':<16} {'rating':>7} {'games':>5} {'wins':>4} {'avg pl':>6} {'points':>6} {'crash':>5}  image")
    for i, r in enumerate(rows, 1):
        avg = f"{r['place_sum'] / r['games']:.2f}" if r["games"] else "-"
        image = (r["image"] or "").split(":")[-1] or ("built-in" if r["builtin"] else "not built")
        print(f"{i:>2} {r['name']:<16} {r['rating']:>7.0f} {r['games']:>5} {r['wins']:>4} {avg:>6} {r['points']:>6} {r['crashes']:>5}  {image}")


def cmd_games(args):
    _, db, _ = _context(args)
    rows = db.games(limit=args.limit)
    players = db.game_players_for([g["id"] for g in rows])
    for g in rows:
        line = ", ".join(f"{p['label'] or p['strategy'] or 'empty'}={p['score']}" + ("!" if p["crashed"] else "")
                         for p in players.get(g["id"], []))
        dur = f"{g['duration_sec']:.0f}s" if g["duration_sec"] else "-"
        print(f"#{g['id']:<5} {g['kind']:<8} {g['format']:<7} {g['status']:<9} {dur:>6}  {line}" + (f"  [{g['error']}]" if g["error"] else ""))


def cmd_cleanup(args):
    cfg, db, _ = _context(args)
    n = GameRunner(cfg, _docker(), db).cleanup_leftovers()
    print(f"removed {n} leftover container(s)/network(s)")
    print(f"marked {db.abort_stale_running()} stale running game(s) as error")
    print(f"marked {db.abort_stale_builds()} interrupted build(s) as failed")


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


def cmd_replay(args):
    from . import replay as R

    cfg, _, _ = _context(args)
    path = Path(args.path) if args.path else cfg.games_dir / f"{args.game_id:06d}" / "replay.dat"
    rep = R.load(path)
    print(f"{path}: map {rep.initial.map_size}, fog={rep.initial.fog_of_war}, ticks {rep.tick_count}, "
          f"players {len(rep.initial.players)}, initial entities {len(rep.initial.entities)}")
    print("final scores by seat:", R.final_scores(rep))
    if args.json:
        Path(args.json).write_text(json.dumps(R.to_json(rep), separators=(",", ":")))
        print("written", args.json)


def cmd_users(args):
    _, db, _ = _context(args)
    for u in db.users():
        flag = " (disabled)" if u["disabled"] else ""
        print(f"{u['id']:>3} {u['email']:<32} {u['role']:<6} {u['name'] or '':<20} strategies {u['n_strategies']}  "
              f"last login {u['last_login_at']}{flag}")


def cmd_admin(args):
    _, db, _ = _context(args)
    u = db.user_by_email(args.email.strip().lower())
    if u is None:
        sys.exit(f"no user {args.email}; they must sign in once first (or list them in config auth.admins)")
    db.update_user(u["id"], role="user" if args.revoke else "admin")
    print(f"{u['email']}: {'user' if args.revoke else 'admin'}")


def cmd_invite(args):
    cfg, db, _ = _context(args)
    auth = Auth(cfg, db, Mailer(cfg))
    code = auth.make_invite(None, args.note)
    base = str(cfg["auth"].get("base_url") or "").rstrip("/")
    print(f"{base}/login?invite={code}")


def cmd_submit(args):
    from .submissions import SubmissionError, submit

    cfg, db, _ = _context(args)
    user = None
    if args.owner:
        user = db.user_by_email(args.owner.strip().lower())
        if user is None:
            sys.exit(f"no user {args.owner}")
        user = dict(user)
    zip_data = None
    if args.zip:
        zip_data = Path(args.zip).read_bytes()
    elif args.dir:
        import io
        import zipfile

        buf = io.BytesIO()
        root = Path(args.dir)
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(root.rglob("*")):
                if p.is_file() and ".git" not in p.parts:
                    zf.write(p, str(p.relative_to(root)))
        zip_data = buf.getvalue()
    try:
        vid = submit(cfg, db, name=args.name, language=args.language, user=user or {"id": None, "role": "admin"},
                     zip_data=zip_data, git_url=args.git, git_ref=args.ref, gpu=args.gpu, merge=args.merge,
                     include=args.include or None, note=args.note, title=args.title, enforce_quota=False)
    except SubmissionError as exc:
        sys.exit(str(exc))
    v = db.version(vid)
    print(f"{v['strategy']} v{v['number']} queued (id {vid}); the worker builds it")


def cmd_rename(args):
    """Rename a strategy in the database (row, versions, past game seats). Unlike the other commands
    this does not sync the seed manifests first: after a manifest was renamed that sync would create
    the new name as an empty row, which the rename then folds in."""
    from .strategies import NAME_RE

    if not NAME_RE.match(args.new):
        sys.exit(f"invalid strategy name {args.new!r} (use [a-z0-9_-])")
    cfg = Config.load(args.root)
    db = DB(cfg.db_path)
    try:
        games = db.rename_strategy(args.old, args.new)
    except ValueError as exc:
        sys.exit(str(exc))
    dropped = 0
    for gid in games:
        for p in (cfg.games_dir / f"{gid:06d}").glob("preview*.png"):
            p.unlink()
            dropped += 1
    print(f"{args.old} -> {args.new}: {len(games)} games relabelled, {dropped} cached previews dropped "
          f"(the image keeps its old tag until the next build)")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="codecraft", description="CodeCraft 2020 competition server")
    parser.add_argument("--root", help="project root (default: $CW_ROOT or the repository)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build", help="build the engine image and the seed strategies (strategies/*.yaml)")
    p.add_argument("names", nargs="*", help="strategies to build (default: all enabled)")
    p.add_argument("--force", action="store_true", help="rebuild even if the image is up to date")
    p.add_argument("--no-cache", action="store_true", help="docker build without layer cache")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("play", help="play one game now")
    p.add_argument("--format", choices=sorted(SEATS), default="Round1")
    p.add_argument("--players", nargs="+", required=True,
                   help="strategy names (name@N = version N, or 'empty'); missing seats get QuickStart")
    p.add_argument("--seed", type=int)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--tag", default="manual")
    p.add_argument("--unrated", action="store_true", help="do not touch ratings (kind=custom)")
    p.set_defaults(func=cmd_play)

    p = sub.add_parser("worker", help="build submissions, play queued and sandbox games", aliases=["loop"])
    p.add_argument("--max-games", type=int)
    p.add_argument("--no-sandbox", action="store_true", help="only queued (custom) games")
    p.add_argument("--no-builds", action="store_true")
    p.set_defaults(func=cmd_worker)

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
    p = sub.add_parser("replay", help="decode a replay (validation / JSON export)")
    p.add_argument("game_id", type=int, nargs="?")
    p.add_argument("--path", help="replay.dat path instead of a game id")
    p.add_argument("--json", help="write the decoded replay to this JSON file")
    p.set_defaults(func=cmd_replay)

    sub.add_parser("users", help="list users").set_defaults(func=cmd_users)
    p = sub.add_parser("admin", help="grant (or revoke) the admin role")
    p.add_argument("email")
    p.add_argument("--revoke", action="store_true")
    p.set_defaults(func=cmd_admin)
    p = sub.add_parser("invite", help="create an invitation link")
    p.add_argument("--note")
    p.set_defaults(func=cmd_invite)
    p = sub.add_parser("rename", help="rename a strategy everywhere (row, versions, past games)")
    p.add_argument("old")
    p.add_argument("new")
    p.set_defaults(func=cmd_rename)
    p = sub.add_parser("submit", help="queue a strategy version from a local zip/dir or a git URL")
    p.add_argument("--name", required=True)
    p.add_argument("--language", required=True)
    p.add_argument("--zip")
    p.add_argument("--dir")
    p.add_argument("--git")
    p.add_argument("--ref")
    p.add_argument("--include", nargs="*")
    p.add_argument("--gpu", action="store_true")
    p.add_argument("--merge", action="store_true")
    p.add_argument("--owner", help="e-mail of the owning user (default: server-owned)")
    p.add_argument("--title")
    p.add_argument("--note")
    p.set_defaults(func=cmd_submit)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    args.func(args)
