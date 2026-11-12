"""The hub: the site, the database and the ladder on one machine, the building and the playing on others.

An executor (remote.py, worker.py) is a machine with Docker. It asks the hub for work over HTTPS with the shared
token (`hub.token`): the queue, the strategies and the results are calls to the hub's database (`/internal/rpc`,
the methods in RPC_METHODS), and what a game or a build leaves behind is sent up (`/internal/games/<id>/files/`,
`/internal/versions/<id>/log`). The hub never calls an executor, so an executor can be switched off at any time:

- what it was doing goes back to the queue after `hub.lease_sec` of silence (a custom game or a build is done
  again; a sandbox game ends as an error that counts for nobody), and on its own restart at once;
- the site goes on: the ladder, the games played, submissions (they wait in the queue); a banner says the game
  server is offline (`status`);
- the hub keeps a replay `hub.replay_days`, the executor that played it for good: an older one is asked for again
  when somebody opens it (`games.restore_at`, answered with the next heartbeat).

Work that needs a GPU waits for an executor with one; when none is online for `hub.gpu_alert_minutes`, the operator
gets a Telegram message (`hub.telegram_token`, `hub.telegram_chat`)."""
from __future__ import annotations

import hmac
import inspect
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from .announce import Announcer
from .config import Config
from .db import DB, LeaseLost, hours_since, ts

log = logging.getLogger("codecraft.hub")

# What an executor may run on the hub's database: the worker's loops and `cw build` (set_image), nothing else.
RPC_METHODS = {
    # reading
    "version", "strategy", "strategies", "runnable_strategies", "sandbox_state", "sandbox_states", "sandbox_duels",
    "game",
    "ok_versions", "leaderboard",
    # the queues, games and builds
    "next_queued_game", "claim_game", "create_game", "set_game_engine", "finish_game", "mark_error",
    "abort_stale_running", "next_queued_version", "start_version", "finish_version", "abort_stale_builds",
    "restored",
    # `cw build` of the seeds on an executor
    "set_image",
}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,40}$")
GAME_FILE_RE = re.compile(r"^(config\.json|results\.json|meta\.json|engine\.log|preview\.png|replay\.dat\.xz|"
                          r"player-[0-3]\.log)$")
MAX_FILE = 64 * 2 ** 20
JSON_CACHE_DAYS = 7          # a replay.json.gz nobody opened for this long goes (it is made again on demand)
LOOP_SEC = 30


def _plain(value):
    """A database answer as JSON: rows as objects."""
    if isinstance(value, sqlite3.Row):
        return {k: value[k] for k in value.keys()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _write(path: Path, data: bytes) -> None:
    """Atomically, through a temp file of its own (requests may overlap)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def enabled(cfg: Config) -> bool:
    """This machine is a hub: executors may call it (a token is set and it is not an executor itself)."""
    return bool(cfg["hub"].get("token")) and not cfg.remote


# The site's view ---------------------------------------------------------------------------------------------------
def status(cfg: Config, db: DB) -> dict | None:
    """The game servers as the site shows them; None on one machine (no hub). `online`: an executor was heard of
    within `hub.offline_sec`; `since`: when the last one was."""
    if not enabled(cfg):
        return None
    offline = float(cfg["hub"].get("offline_sec") or 120)
    rows = db.executors()
    fresh = {r["name"] for r in rows if r["last_seen"] and hours_since(r["last_seen"]) * 3600 <= offline}
    working = {r["name"] for r in rows if r["name"] in fresh and not json.loads(r["info"] or "{}").get("stopping")}
    queued = db.one("SELECT (SELECT COUNT(*) FROM games WHERE status='queued') AS games, "
                    "(SELECT COUNT(*) FROM strategy_versions WHERE status='queued') AS builds")
    return {"online": bool(working), "gpu_online": any(r["gpu"] for r in rows if r["name"] in working),
            "since": max((r["last_seen"] for r in rows if r["last_seen"]), default=None),
            "queued_games": int(queued["games"]), "queued_builds": int(queued["builds"]),
            "executors": [{"name": r["name"], "gpu": bool(r["gpu"]), "last_seen": r["last_seen"],
                           "online": r["name"] in working} for r in rows]}


def replay_restorable(cfg: Config, row) -> bool:
    """A finished game whose replay this hub let go: the executor that played it still has it."""
    return enabled(cfg) and row is not None and row["status"] == "finished" and bool(row["executor"])


# The executors' API --------------------------------------------------------------------------------------------------
def router(cfg: Config, db: DB, announcer: Announcer | None = None) -> APIRouter:
    token = str(cfg["hub"].get("token") or "")
    announcer = announcer or Announcer(cfg, db)

    def executor(request: Request) -> str:
        auth = request.headers.get("authorization", "")
        if not token or not hmac.compare_digest(auth.encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, "bad hub token")
        name = request.headers.get("x-cw-executor", "")
        if not NAME_RE.match(name):
            raise HTTPException(400, "X-CW-Executor: a name of [a-z0-9_.-]")
        return name

    api = APIRouter(prefix="/internal")

    @api.post("/rpc")
    def rpc(body: dict, name: str = Depends(executor)):
        method = body.get("method")
        if method not in RPC_METHODS:
            raise HTTPException(403, f"{method!r} is not open to executors")
        fn = getattr(db, method)
        try:
            bound = inspect.signature(fn).bind(*(body.get("args") or []), **(body.get("kwargs") or {}))
        except TypeError as exc:
            raise HTTPException(400, f"{method}: {exc}")
        args = bound.arguments
        if "executor" in inspect.signature(fn).parameters:
            args["executor"] = name                  # an executor speaks for itself only
        if method == "finish_game":                  # the hub's own rating, points and leagues
            game = db.one("SELECT format FROM games WHERE id=?", args.get("game_id"))
            args["rating_cfg"] = cfg["rating"]
            args["leagues_cfg"] = cfg["leagues"]
            if game is not None:
                args["points_table"] = cfg["points"][game["format"]]
        try:
            result = fn(*bound.args, **bound.kwargs)
        except LeaseLost as exc:
            raise HTTPException(409, str(exc))
        if method == "finish_version" and result and args.get("status") == "ok":
            try:
                v = db.version(args["version_id"])
                announcer.built(v, db.one("SELECT s.*, u.name AS owner_name FROM strategies s LEFT JOIN users u "
                                          "ON u.id=s.owner_id WHERE s.name=?", v["strategy"]))
            except Exception:  # noqa: BLE001
                log.exception("announcement failed")
        return {"result": _plain(result)}

    @api.post("/heartbeat")
    def heartbeat(info: dict, name: str = Depends(executor)):
        db.heartbeat(name, bool(info.get("gpu")), info.get("engine"), info)
        return {"restore": db.restore_requests(name)}

    @api.put("/games/{game_id}/files/{fname}")
    async def game_file(game_id: int, fname: str, request: Request, name: str = Depends(executor)):
        if not GAME_FILE_RE.match(fname):
            raise HTTPException(400, "not a game file")
        row = db.one("SELECT executor FROM games WHERE id=?", game_id)
        if row is None:
            raise HTTPException(404, "no such game")
        if row["executor"] not in (None, name):
            raise HTTPException(409, f"game {game_id} is {row['executor']}'s")
        data = await request.body()
        if len(data) > MAX_FILE:
            raise HTTPException(413, "too large")
        _write(cfg.games_dir / f"{game_id:06d}" / fname, data)
        return {"ok": True}

    @api.put("/versions/{version_id}/log")
    async def build_log(version_id: int, request: Request, name: str = Depends(executor)):
        v = db.version(version_id)
        if v is None:
            raise HTTPException(404, "no such version")
        if v["executor"] != name:
            raise HTTPException(409, f"version {version_id} is not {name}'s build")
        data = await request.body()
        cap = int(float(cfg.get("build", "log_max_mb", default=16) or 16) * 2 ** 20)
        _write(cfg.build_dir / "logs" / f"{v['strategy']}-v{v['number']}.log", data[-cap:])
        return {"ok": True}

    @api.get("/versions/{version_id}/source.zip")
    def source(version_id: int, name: str = Depends(executor)):
        v = db.version(version_id)
        if v is None or v["source_kind"] != "zip" or not v["source"]:
            raise HTTPException(404, "no uploaded sources for this version")
        path = (cfg.data_dir / v["source"]).parent / "source.zip"
        if not path.is_file():
            raise HTTPException(404, "the archive is gone")
        return FileResponse(path, media_type="application/zip")

    return api


# The hub's own loop ---------------------------------------------------------------------------------------------------
class HubLoop:
    """Every LOOP_SEC: silent executors lose their work to the queue, the leagues and the announcements follow the
    ladder, an alert when GPU work waits with no GPU online; hourly, old replays go (the executors keep them)."""

    def __init__(self, cfg: Config, db: DB, announcer: Announcer | None = None, send=None):
        self.cfg, self.db = cfg, db
        self.announcer = announcer or Announcer(cfg, db)
        self.send = send or self.telegram
        self.stop = threading.Event()
        self.swept = 0.0

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, name="hub", daemon=True)
        t.start()
        return t

    def run(self) -> None:
        log.info("hub: executors lose their work after %s s of silence; replays kept %s days",
                 self.cfg["hub"]["lease_sec"], self.cfg["hub"]["replay_days"])
        while not self.stop.is_set():
            self.tick()
            self.stop.wait(LOOP_SEC)

    def tick(self) -> None:
        for step in (self.expire, self.leagues, self.announce, self.gpu_alert, self.sweep):
            try:
                step()
            except Exception:  # noqa: BLE001
                log.exception("hub: %s failed", step.__name__)

    def expire(self) -> None:
        for name in self.db.executors_silent(float(self.cfg["hub"].get("lease_sec") or 600)):
            games = self.db.abort_stale_running(name)
            builds = self.db.abort_stale_builds(name)
            log.warning("executor %s is silent: %d game(s) and %d build(s) taken back", name, games, builds)

    def leagues(self) -> None:
        self.db.update_leagues(self.cfg["leagues"])

    def announce(self) -> None:
        row = self.db.one("SELECT engine FROM games WHERE status='finished' AND engine IS NOT NULL "
                          "ORDER BY id DESC LIMIT 1")
        self.announcer.tick(engine=row["engine"] if row else None)

    # GPU -----------------------------------------------------------------------
    def gpu_alert(self) -> None:
        hub = self.cfg["hub"]
        st = status(self.cfg, self.db)
        waiting = self.db.gpu_waiting()
        items = waiting["versions"] + waiting["games"]
        if st is None or st["gpu_online"] or not items:
            if self.db.meta("gpu_alert_at"):
                with self.db.tx() as c:
                    c.execute("DELETE FROM meta WHERE key='gpu_alert_at'")
            return
        oldest = min(i["created_at"] for i in items)
        if hours_since(oldest) * 60 < float(hub.get("gpu_alert_minutes") or 10):
            return
        last = self.db.meta("gpu_alert_at")
        if last and hours_since(last) < 6:
            return
        parts = [f"{v['strategy']} v{v['number']} (build)" for v in waiting["versions"]]
        parts += [f"game #{g['id']}" for g in waiting["games"]]
        base = str(self.cfg["auth"].get("base_url") or "").rstrip("/")
        text = (f"CodeCraft: GPU work is waiting and no GPU machine is online: {', '.join(parts[:10])}"
                + (f" and {len(parts) - 10} more" if len(parts) > 10 else "")
                + f". Waiting since {oldest} UTC. {base}/admin")
        if self.send(text):
            with self.db.tx() as c:
                c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('gpu_alert_at', ?)",
                          (ts(datetime.now(timezone.utc)),))

    def telegram(self, text: str) -> bool:
        hub = self.cfg["hub"]
        token, chat = hub.get("telegram_token"), hub.get("telegram_chat")
        if not (token and chat):
            log.warning("alert not sent (no hub.telegram_token / telegram_chat): %s", text)
            return False
        body = json.dumps({"chat_id": chat, "text": text, "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("alert not sent: %s", type(exc).__name__)     # the URL holds the token: not logged
            return False

    # Replays -------------------------------------------------------------------
    def sweep(self, force: bool = False) -> int:
        """Once an hour: the replays of games finished more than `hub.replay_days` ago leave the hub (the executor
        that played them keeps them), and so do the viewer's JSON copies nobody opened for a week."""
        if not force and time.monotonic() - self.swept < 3600:
            return 0
        self.swept = time.monotonic()
        days = float(self.cfg["hub"].get("replay_days") or 0)
        removed = 0
        if days > 0:
            cut = ts(datetime.now(timezone.utc) - timedelta(days=days))
            old = time.time() - days * 86400     # a replay fetched back for somebody stays its own replay_days
            for row in self.db.q("SELECT id FROM games WHERE executor IS NOT NULL AND finished_at<?", cut):
                gdir = self.cfg.games_dir / f"{row['id']:06d}"
                for name in ("replay.dat.xz", "replay.dat"):
                    p = gdir / name
                    if p.is_file() and p.stat().st_mtime < old:
                        p.unlink()
                        (gdir / "replay.json.gz").unlink(missing_ok=True)
                        for tick_image in gdir.glob("preview-*.png"):
                            tick_image.unlink()
                        removed += 1
        stale = time.time() - JSON_CACHE_DAYS * 86400
        for p in self.cfg.games_dir.glob("*/replay.json.gz"):
            try:
                if max(p.stat().st_mtime, p.stat().st_atime) < stale:
                    p.unlink()
                    removed += 1
            except FileNotFoundError:
                pass
        if removed:
            log.info("hub: %d old replay file(s) removed", removed)
        return removed
