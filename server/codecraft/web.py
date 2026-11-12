"""Web UI and API: leaderboard, games, replay viewer, submissions, custom games, accounts, admin, and the
surface for coding agents (MCP at /mcp/, REST at /api/v1/<tool>, the /agents page; see agents.py)."""
from __future__ import annotations

import contextlib
import gzip
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.convertors import Convertor, register_url_convertor
from starlette.datastructures import MutableHeaders
from starlette.requests import ClientDisconnect

from . import agents as A
from . import hub as H
from . import leagues as L
from . import mcp_server
from . import preview as P
from . import replay as R
from . import rules as RU
from .auth import Auth, AuthError
from .config import SEATS, Config
from .db import DB
from .submissions import SubmissionError, submit

log = logging.getLogger("codecraft.web")
HERE = Path(__file__).parent
COOKIE = "cw_session"
# The replay viewer is the local runner's (built into it as well): one viewer for the runner and the site.
VIEWER = HERE.parents[1] / "sim-cpp" / "viewer" / "viewer.html"
VIEWER_SLOT = '<script id="cc-site" type="application/json">null</script>'
VIEWER_SPEEDS = (1, 2, 5, 10, 20, 50, 100)


def viewer_page(site: dict, engine_version: str) -> str:
    """The viewer's page for one game: its data in place of the runner's null (the page then plays the game's
    replay.json, with no runner behind it), the engine's version in its help. Read on every request, so a git
    pull updates it, as it does the engine."""
    page = VIEWER.read_text(encoding="utf-8")
    if VIEWER_SLOT not in page:
        raise RuntimeError(f"{VIEWER} has no {VIEWER_SLOT}")
    data = json.dumps(site, separators=(",", ":"))
    data = data.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")   # inert in a <script>
    return (page.replace(VIEWER_SLOT, VIEWER_SLOT.replace(">null<", f">{data}<"))
            .replace("%ENGINE_VERSION%", engine_version))


class _Anything(Convertor):
    """Like Starlette's `path` convertor, but across newlines too: with `path` (regex `.*`) a decoded %0A
    would make the upload route miss and the request fall through to the next route under /mcp/."""

    regex = r"[\s\S]*"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("anything", _Anything())


def _agent_path(path: str) -> bool:
    """Paths where the API token is the credential: the MCP endpoint and the REST mirror."""
    return path in ("/mcp", "/api/v1") or path.startswith(("/mcp/", "/api/v1/"))


def _json_line(body: dict, status: int, headers: dict | None = None) -> Response:
    """JSON with a newline at the end: curl prints it, and the shell prompt starts on a new line."""
    return Response(json.dumps(body, ensure_ascii=False) + "\n", status_code=status, media_type="application/json",
                    headers=headers)


def safe_next(target: str | None) -> str:
    """Where to go after signing in: a path on this site, else "/". Refuses what a browser reads as another
    host (//evil.com, ///evil.com, /\\evil.com), a scheme, and control characters (browsers drop tabs)."""
    target = target or "/"
    if (not target.startswith("/") or target.startswith("//") or "\\" in target
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in target) or urlsplit(target).netloc):
        return "/"
    return target


class SecurityHeaders:
    """Headers on every response, the MCP and REST routes and the early refusals included (pure ASGI, so a
    streamed response is not buffered). HSTS only when the site is served over https."""

    def __init__(self, app, hsts: bool = False):
        self.app = app
        self.headers = [("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "same-origin"),
                        ("X-Frame-Options", "DENY")]
        if hsts:
            self.headers.append(("Strict-Transport-Security", "max-age=31536000"))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                for name, value in self.headers:
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_app(cfg: Config, db: DB, auth: Auth | None = None) -> FastAPI:
    auth = auth or Auth(cfg, db)
    title = str(cfg["web"].get("title") or "CodeCraft")
    ax = A.Agents(cfg, db)
    mcp = mcp_server.build(ax)
    mcp_app = mcp.streamable_http_app()      # creates the session manager the lifespan runs

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        # The MCP SDK needs its session manager running even in stateless mode; a mounted app's own
        # lifespan is not run by Starlette, so it is entered here.
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title=title, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.agents, app.state.mcp = ax, mcp
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["rating"] = lambda v: "-" if v is None else f"{v:.0f}"
    templates.env.filters["sd"] = lambda v: "" if v is None else f"±{v:.0f}"
    templates.env.filters["build_error"] = A.build_error
    if cfg.get("gpu", "available") is None:
        # The web service has no Docker socket (docker-compose.yml), so `auto` resolves to no GPU here;
        # a GPU host says CW_GPU_ENABLED=true and the probe is skipped.
        client = None
        if cfg["gpu"].get("enabled") == "auto":
            try:
                import docker
                client = docker.from_env()
            except Exception:  # noqa: BLE001
                client = None
        cfg.resolve_gpu(client)
    templates.env.globals.update(title=title, SEATS=SEATS, cfg=cfg.data, registration=auth.registration,
                                 gpu_ok=cfg.gpu_ok, server_status=lambda: H.status(cfg, db))
    if H.enabled(cfg):
        app.include_router(H.router(cfg, db))      # the executors' API (hub.py)
    decoding = threading.BoundedSemaphore(2)      # a replay decodes in ~300 MB: not too many at once

    def game_dir(game_id: int) -> Path:
        return cfg.games_dir / f"{game_id:06d}"

    def replay_state(row, gdir: Path) -> str | None:
        """"here", "away" (the hub let it go; the executor that played it uploads it again, asked now) or None."""
        if R.replay_file(gdir) is not None:
            return "here"
        if H.replay_restorable(cfg, row):
            return "away"
        return None

    def fetch_replay(row) -> HTTPException:
        """Ask the executor for a replay the hub let go; the answer for the meantime."""
        db.request_restore(row["id"])
        st = H.status(cfg, db)
        when = "in a minute" if st and st.get("online") else "when the game server is back (it is offline now)"
        return HTTPException(503, f"the replay of game #{row['id']} is kept on the game server; it is being fetched, "
                                  f"try again {when}", headers={"Retry-After": "30"})

    def game_files(gdir: Path) -> list[str]:
        names = []
        if gdir.exists():
            for p in sorted(gdir.iterdir()):
                if p.name.endswith((".gz", ".tmp")) or p.name.startswith("."):
                    continue
                names.append("replay.dat" if p.name == R.REPLAY_XZ else p.name)
        return sorted(set(names))

    def absolute(request: Request, path: str) -> str:
        """Absolute URL for link previews: the configured base URL, else the request's."""
        base = (cfg["auth"].get("base_url") or str(request.base_url)).rstrip("/")
        return base + path

    def player_labels(players) -> list[str]:
        return [p["label"] or p["strategy"] or "empty" for p in players]

    def game_og(request: Request, row, players, has_replay: bool) -> dict:
        parts = [row["format"]]
        if row["status"] == "finished":
            parts += [f"{(p['label'] or p['strategy'] or 'empty')} {p['score'] or 0} (#{p['place']})"
                      for p in sorted(players, key=lambda p: (p["place"] or 99, p["slot"]))]
        else:
            parts += [row["status"], " vs ".join(player_labels(players))]
        return dict(title=f"Game #{row['id']} · {row['format']}", description=" · ".join(parts),
                    url=absolute(request, f"/games/{row['id']}"),
                    image=absolute(request, f"/games/{row['id']}/preview.png") if has_replay else None)

    def render(request: Request, template: str, **ctx):
        ctx.setdefault("user", request.state.user)
        ctx.setdefault("msg", request.query_params.get("m"))
        ctx.setdefault("err", request.query_params.get("e"))
        return templates.TemplateResponse(request, template, ctx)

    def back(url: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
        sep = "&" if "?" in url else "?"
        if msg:
            url = f"{url}{sep}m={quote(msg)}"
        elif err:
            url = f"{url}{sep}e={quote(err)}"
        return RedirectResponse(url, status_code=303)

    def need_user(request: Request) -> dict:
        if request.state.user is None:
            raise HTTPException(303, headers={"Location": f"/login?next={quote(safe_next(str(request.url.path)))}"})
        return request.state.user

    def need_admin(request: Request) -> dict:
        user = need_user(request)
        if user["role"] != "admin":
            raise HTTPException(403, "admins only")
        return user

    def is_admin(user) -> bool:
        return bool(user) and user["role"] == "admin"

    def owns(user, strategy_row) -> bool:
        return bool(user) and strategy_row is not None and (strategy_row["owner_id"] == user["id"] or is_admin(user))

    def ip_of(request: Request) -> str:
        """The client's address. Behind the configured proxy (web.trusted_proxies) uvicorn has already put
        the forwarded address here; an X-Forwarded-For from anyone else is ignored."""
        return request.client.host if request.client else "?"

    async def agent_gate(request: Request, call_next, path: str):
        """/mcp/ and /api/v1/: the API token is the credential and the cookie is ignored, so a browser
        cannot drive these routes; there is no Origin check here (an MCP client is not a browser; the
        MCP SDK checks Host and Origin itself). The token comes as `Authorization: Bearer` or as the first
        path segment, /mcp/<token>/..., which is cut out before the MCP app sees the path. The upload
        route /mcp/upload/<ticket>/ carries its own credential."""
        request.state.user = None
        if path.startswith(A.UPLOAD_PREFIX):
            return await call_next(request)
        token = None
        header = request.headers.get("authorization") or ""
        if header[:7].lower() == "bearer ":
            token = header[7:].strip()
        segments = path.split("/")                      # ['', 'mcp', '<token>', ...]
        if segments[1] == "mcp" and len(segments) > 2 and segments[2].startswith(A.TOKEN_PREFIX):
            token = segments[2]
            rest = "/mcp/" + "/".join(segments[3:])
            request.scope["path"] = rest
            request.scope["raw_path"] = rest.encode()
        user = ax.tokens.user(token)
        if user is None:
            return JSONResponse(
                {"error": "a valid API token is needed: Authorization: Bearer <token>, or the URL "
                          f"{ax.url('/mcp/<token>/')}; create one at {ax.url('/agents')}"},
                status_code=401, headers={"WWW-Authenticate": 'Bearer realm="codecraft"'})
        if not ax.limiter.allow(f"token:{user['token_id']}"):
            return JSONResponse({"error": f"rate limit: at most {A.RATE_LIMIT} requests a minute per token"},
                                status_code=429, headers={"Retry-After": "30"})
        if request.method == "GET" and request.scope["path"] == "/mcp/":
            # Stateless: the server never pushes, and the SDK would hold an empty event stream open
            # forever. 405 is the spec's way of saying "no stream here"; clients carry on without one.
            return JSONResponse({"error": "no event stream: this MCP server is stateless, POST JSON-RPC to it"},
                                status_code=405, headers={"Allow": "POST"})
        request.state.user = user
        return await call_next(request)

    @app.middleware("http")
    async def session_middleware(request: Request, call_next):
        path = request.scope.get("path") or "/"
        if _agent_path(path):
            return await agent_gate(request, call_next, path)
        request.state.user = auth.user_from_token(request.cookies.get(COOKIE))
        if request.method == "POST":
            origin = request.headers.get("origin")
            host = request.headers.get("host")
            if origin and host and origin.split("://", 1)[-1] != host:
                return PlainTextResponse("cross-site request refused", status_code=403)
        return await call_next(request)

    # Added after the session middleware, so it is the outer one and its refusals get the headers too.
    app.add_middleware(SecurityHeaders, hsts=bool(cfg["auth"].get("cookie_secure")))

    # Public pages ------------------------------------------------------------
    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect width="16" height="16" rx="3" fill="#2457d6"/>'
               '<rect x="3" y="3" width="4" height="4" fill="#fff"/><rect x="9" y="9" width="4" height="4" fill="#fff"/>'
               '<rect x="9" y="3" width="4" height="4" fill="#9fc0ff"/><rect x="3" y="9" width="4" height="4" fill="#9fc0ff"/></svg>')
        return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=604800"})

    @app.get("/", response_class=HTMLResponse)
    def cover(request: Request):
        featured = None
        gid = cfg["web"].get("featured_game")
        candidates = [db.game(int(gid))[0]] if gid else db.featured_games()
        for g in candidates:
            if g is not None and R.replay_file(game_dir(g["id"])) is not None:
                featured = g
                break
        labels = []
        og = dict(title=f"{title} · the CodeCraft 2020 arena", url=absolute(request, "/"))
        if featured is not None:
            labels = player_labels(db.game(featured["id"])[1])
            og["image"] = absolute(request, f"/games/{featured['id']}/preview.png")
        return render(request, "cover.html", featured=featured, labels=labels, stats=db.stats(),
                      n_strategies=len(db.leaderboard()), n_users=len(db.users()), og=og)

    @app.get("/rules", response_class=HTMLResponse)
    def rules(request: Request):
        """The arena (docs/arena.md with the numbers of config.yaml), then the game (docs/doc-en.md)."""
        if not RU.exists(cfg):
            raise HTTPException(404, "rules not found")
        text = RU.rules_markdown(cfg)
        try:
            body, toc = RU.html(text)
        except ImportError:
            body, toc = "<pre>" + text.replace("&", "&amp;").replace("<", "&lt;") + "</pre>", ""
        return render(request, "rules.html", body=body, toc=toc)

    @app.get("/quickstart", response_class=HTMLResponse)
    def quickstart(request: Request):
        """From the starter to a game on your machine to the ladder: docs/QUICKSTART.md with the numbers of
        config.yaml and the engine's version; the page shows the blocks of the language picked."""
        if not RU.quickstart_exists(cfg):
            raise HTTPException(404, "quick start not found")
        text = RU.quickstart_markdown(cfg)
        try:
            body, toc = RU.html(text)
        except ImportError:
            body, toc = "<pre>" + text.replace("&", "&amp;").replace("<", "&lt;") + "</pre>", ""
        og = dict(title=f"{title} · quick start", url=absolute(request, "/quickstart"),
                  description="From the starter to a game on your machine in ten minutes, in any of 15 languages; "
                              "then the ladder.")
        return render(request, "quickstart.html", body=body, toc=toc, languages=RU.LANGUAGES, og=og)

    @app.get("/leaderboard", response_class=HTMLResponse)
    def leaderboard(request: Request):
        recent = db.games(limit=15)
        leagues = db.league_state()
        settled = int(cfg["leagues"].get("settled_games", 30))
        return render(request, "index.html", recent=recent, leagues=leagues, settled_games=settled,
                      standings=L.standings(db.leaderboard(), leagues["lines"], settled),
                      players=db.game_players_for([g["id"] for g in recent]), stats=db.stats(),
                      queue=db.game_queue(), builds=db.build_queue())

    @app.get("/games", response_class=HTMLResponse)
    def games(request: Request, page: int = 0, kind: str | None = None, strategy: str | None = None,
              mine: int = 0, status: str | None = None):
        limit = 50
        user_id = request.state.user["id"] if (mine and request.state.user) else None
        rows = db.games(limit=limit, offset=page * limit, kind=kind or None, strategy=strategy or None,
                        user_id=user_id, status=status or None)
        return render(request, "games.html", games=rows, players=db.game_players_for([g["id"] for g in rows]),
                      page=page, kind=kind or "", strategy=strategy or "", mine=mine, status=status or "", limit=limit)

    # Custom games -----------------------------------------------------------
    @app.get("/games/new", response_class=HTMLResponse)
    def new_game_form(request: Request):
        user = need_user(request)
        return render(request, "new_game.html", options=A.seat_options(cfg, db, user), formats=sorted(SEATS),
                      max_gpu=int(cfg.get("gpu", "max_per_game", default=1) or 0) if cfg.gpu_ok else 0)

    @app.post("/games/new")
    async def new_game_post(request: Request):
        """The form's rules live in agents.queue_custom_game, shared with the agents' `play`."""
        user = need_user(request)
        form = await request.form()
        fmt = str(form.get("format") or "Round1")
        if fmt not in SEATS:
            return back("/games/new", err="bad format")
        seats = [str(form.get(f"seat{slot}") or "empty") for slot in range(SEATS[fmt])]
        try:
            game_id = A.queue_custom_game(cfg, db, user, fmt, seats, str(form.get("seed") or "").strip() or None)
        except A.AgentError as exc:
            return back("/games/new", err=str(exc))
        return back(f"/games/{game_id}", msg="queued; the worker will play it shortly")

    @app.get("/games/{game_id}", response_class=HTMLResponse)
    def game(request: Request, game_id: int):
        row, players = db.game(game_id)
        if row is None:
            raise HTTPException(404, "no such game")
        gdir = game_dir(game_id)
        user = request.state.user
        files = [n for n in game_files(gdir)
                 if not (n.startswith("player-") and not (user and (is_admin(user) or _owns_slot(user, players, n))))]
        has_replay = replay_state(row, gdir) is not None
        return render(request, "game.html", game=row, players=players, files=files, has_replay=has_replay,
                      labels=player_labels(players), og=game_og(request, row, players, has_replay))

    @app.get("/games/{game_id}/preview.png")
    def game_preview(request: Request, game_id: int, tick: int | None = None):
        """The field at the last tick (or `?tick=`, rounded to a keyframe) as a 1200x630 PNG."""
        row, players = db.game(game_id)
        if row is None:
            raise HTTPException(404, "no such game")
        gdir = game_dir(game_id)
        try:
            path = P.ensure(gdir, player_labels(players), game_id, row["format"],
                            footer=P.site_host(cfg["auth"].get("base_url")), tick=tick)
        except FileNotFoundError:
            raise HTTPException(404, "no replay for this game")
        except R.ReplayError as exc:
            raise HTTPException(500, f"replay cannot be decoded: {exc}")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    def _owns_slot(user, players, fname: str) -> bool:
        try:
            slot = int(fname.split("-")[1].split(".")[0])
        except (IndexError, ValueError):
            return False
        for p in players:
            if p["slot"] == slot:
                return p["owner_id"] == user["id"]
        return False

    @app.get("/games/{game_id}/replay.json")
    def game_replay(request: Request, game_id: int):
        gdir = game_dir(game_id)
        cache = gdir / "replay.json.gz"
        raw = R.replay_file(gdir)
        if raw is None:
            row, _ = db.game(game_id)
            if H.replay_restorable(cfg, row):
                raise fetch_replay(row)
            raise HTTPException(404, "no replay for this game")
        if not cache.exists() or cache.stat().st_mtime < raw.stat().st_mtime:
            try:
                with decoding:
                    data = R.to_json(R.load(raw))
            except R.ReplayError as exc:
                raise HTTPException(500, f"replay cannot be decoded: {exc}")
            # a temp file of its own: two requests at once (a phone loads it twice) must not share one
            fd, tmp_name = tempfile.mkstemp(dir=gdir, prefix=".replay.", suffix=".tmp")
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as raw_fh, gzip.GzipFile(fileobj=raw_fh, mode="wb", compresslevel=6) as fh:
                    fh.write(json.dumps(data, separators=(",", ":")).encode())
                os.chmod(tmp, 0o644)
                tmp.replace(cache)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        return FileResponse(cache, media_type="application/json",
                            headers={"Content-Encoding": "gzip", "Cache-Control": "public, max-age=86400"})

    @app.get("/games/{game_id}/viewer", response_class=HTMLResponse)
    def game_viewer(request: Request, game_id: int, autoplay: bool = False, speed: int | None = None):
        """The replay in the local runner's viewer, a page of its own that the game page and the cover frame
        (this origin only); `autoplay` plays it in a loop, at `speed` ticks a second."""
        row, players = db.game(game_id)
        if row is None:
            raise HTTPException(404, "no such game")
        state = replay_state(row, game_dir(game_id))
        if state is None:
            raise HTTPException(404, "no replay for this game")
        if state == "away":                         # a page that waits for it, reloading itself
            exc = fetch_replay(row)
            return HTMLResponse(f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="15">'
                                f'<title>Replay #{game_id}</title><body style="font:15px system-ui;margin:2em;'
                                f'color:#888">{exc.detail}. This page reloads itself.</body>', status_code=503,
                                headers={"Content-Security-Policy": "frame-ancestors 'self'",
                                         "X-Frame-Options": "SAMEORIGIN", "Cache-Control": "no-cache"})
        engine = row["engine"] or ""
        site = {"game": game_id, "page": f"/games/{game_id}", "replay": f"/games/{game_id}/replay.json",
                "download": f"/games/{game_id}/files/replay.dat", "labels": player_labels(players),
                "seed": row["seed"] or "", "engine": engine, "autoplay": autoplay,
                "speed": speed if speed in VIEWER_SPEEDS else None}
        # frame-ancestors makes browsers ignore X-Frame-Options, which a reverse proxy may add (the live one adds
        # DENY to every response, and DENY with SAMEORIGIN is DENY)
        return HTMLResponse(viewer_page(site, engine.rpartition(":")[2] or "unknown"),
                            headers={"Content-Security-Policy": "frame-ancestors 'self'",
                                     "X-Frame-Options": "SAMEORIGIN", "Cache-Control": "no-cache"})

    @app.get("/games/{game_id}/files/{name}")
    def game_file(request: Request, game_id: int, name: str):
        if "/" in name or name.startswith(".") or name.endswith((".gz", ".xz")):
            raise HTTPException(400, "bad file name")
        gdir = game_dir(game_id)
        path = gdir / name
        if name == R.REPLAY and not path.is_file():
            xz = gdir / R.REPLAY_XZ
            if xz.is_file():
                return Response(R.read_bytes(xz), media_type="application/octet-stream",
                                headers={"Content-Disposition": f'attachment; filename="{name}"'})
            row, _ = db.game(game_id)
            if H.replay_restorable(cfg, row):
                raise fetch_replay(row)
        if not path.is_file():
            raise HTTPException(404, "no such file")
        if name.startswith("player-"):
            user = request.state.user
            _, players = db.game(game_id)
            if not (user and (is_admin(user) or _owns_slot(user, players, name))):
                raise HTTPException(403, "a strategy's log is visible to its owner only")
        media = "application/octet-stream" if name.endswith(".dat") else "text/plain; charset=utf-8"
        return FileResponse(path, media_type=media, filename=name if name.endswith(".dat") else None)

    @app.get("/strategies/{name}", response_class=HTMLResponse)
    def strategy(request: Request, name: str):
        row = db.strategy(name)
        if row is None:
            raise HTTPException(404, "no such strategy")
        user = request.state.user
        versions = db.versions(name) if owns(user, row) else []
        settled = int(cfg["leagues"].get("settled_games", 30))
        standing = next(({"league": g["league"], **e} for g in L.standings(db.leaderboard(), db.league_state()["lines"], settled)
                         for e in g["rows"] if e["row"]["name"] == name), None)
        return render(request, "strategy.html", s=row, games=db.strategy_games(name), versions=versions,
                      standing=standing, settled_games=settled,
                      owner=owns(user, row), limits=json.loads(row["limits"]) if row["limits"] else {})

    @app.post("/strategies/{name}/settings")
    def strategy_settings(request: Request, name: str, title_: str = Form("", alias="title"),
                          enabled: str = Form(""), activate: int | None = Form(None)):
        user = need_user(request)
        row = db.strategy(name)
        if row is None or not owns(user, row):
            raise HTTPException(403, "not your strategy")
        if activate:
            v = db.version(activate)
            if v is None or v["strategy"] != name:
                raise HTTPException(400, "bad version")
            try:
                db.activate_version(activate)
            except ValueError as exc:
                return back(f"/strategies/{name}", err=str(exc))
            return back(f"/strategies/{name}", msg=f"version {v['number']} is now active")
        db.update_strategy(name, title=(title_ or name).strip()[:80], enabled=int(enabled == "on"))
        return back(f"/strategies/{name}", msg="saved")

    @app.get("/versions/{version_id}/log", response_class=PlainTextResponse)
    def version_log(request: Request, version_id: int):
        v = db.version(version_id)
        if v is None:
            raise HTTPException(404, "no such version")
        user = request.state.user
        if not owns(user, db.strategy(v["strategy"])):
            raise HTTPException(403, "not your strategy")
        text = f"# {v['strategy']} v{v['number']} - {v['status']}\n"
        if v["error"]:
            text += f"# error: {v['error']}\n"
        if v["log_path"] and (cfg.data_dir / v["log_path"]).is_file():
            text += "\n" + (cfg.data_dir / v["log_path"]).read_text(errors="replace")[-200_000:]
        return text

    # Submissions ------------------------------------------------------------
    @app.get("/submit", response_class=HTMLResponse)
    def submit_form(request: Request, name: str = ""):
        user = need_user(request)
        return render(request, "submit.html", languages=cfg.languages(), mine=db.user_strategies(user["id"]),
                      name=name, allow_git=bool(cfg["submissions"].get("allow_git", True)))

    @app.post("/submit")
    async def submit_post(request: Request, name: str = Form(...), language: str = Form(...),
                          git_url: str = Form(""), git_ref: str = Form(""), gpu: str = Form(""),
                          merge: str = Form(""), note: str = Form(""), title_: str = Form("", alias="title"),
                          archive: UploadFile | None = File(None)):
        user = need_user(request)
        data = None
        if archive is not None and archive.filename:
            data = await archive.read()
            limit = int(cfg["submissions"].get("max_zip_mb") or 20) * 1024 * 1024
            if len(data) > limit:
                return back("/submit", err=f"the archive is larger than {limit // 1024 // 1024} MB")
        try:
            vid = submit(cfg, db, name=name, language=language, user=user, zip_data=data,
                         git_url=git_url.strip() or None, git_ref=git_ref, gpu=gpu == "on", merge=merge == "on",
                         note=note, title=title_)
        except SubmissionError as exc:
            return back(f"/submit?name={quote(name)}", err=str(exc))
        v = db.version(vid)
        return back(f"/strategies/{v['strategy']}", msg=f"version {v['number']} queued for building")

    @app.post("/games/{game_id}/cancel")
    def cancel_game(request: Request, game_id: int):
        user = need_user(request)
        row, _ = db.game(game_id)
        if row is None:
            raise HTTPException(404)
        if row["status"] != "queued" or not (is_admin(user) or row["created_by"] == user["id"]):
            return back(f"/games/{game_id}", err="only a queued game of yours can be cancelled")
        db.mark_error(game_id, "cancelled")
        return back(f"/games/{game_id}", msg="cancelled")

    # Accounts -----------------------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/", invite: str = "", email: str = ""):
        next = safe_next(next)
        if request.state.user:
            return RedirectResponse(next, status_code=303)
        inv = db.invite(invite) if invite else None
        invite_ok = inv is not None and inv["used_by"] is None
        return render(request, "login.html", next=next, invite=invite if invite_ok else "", email=email,
                      invite_bad=bool(invite and not invite_ok), sent=request.query_params.get("sent"))

    @app.post("/login/code")
    def login_code(request: Request, email: str = Form(...), next: str = Form("/"), invite: str = Form("")):
        next = safe_next(next)
        try:
            auth.request_code(email, ip_of(request), invite or None)
        except AuthError as exc:
            return back(f"/login?next={quote(next)}&invite={quote(invite)}", err=str(exc))
        return back(f"/login?next={quote(next)}&invite={quote(invite)}&email={quote(email.strip().lower())}&sent=1")

    @app.post("/login")
    def login_post(request: Request, email: str = Form(...), code: str = Form(...), next: str = Form("/"),
                   invite: str = Form(""), name: str | None = Form(None)):
        """The code form has no name field: an existing account is signed in at once (also when the mail
        link auto-submits the form); a new address gets the name step, which posts the same code again."""
        next = safe_next(next)
        try:
            user, token = auth.login(email, code, ip_of(request), request.headers.get("user-agent"),
                                     invite or None, name)
        except AuthError as exc:
            if exc.code == "name_required":
                address = email.strip().lower()
                return render(request, "login.html", ask_name=True, email=address, code=code, next=next,
                              invite=invite, suggested=address.split("@")[0][:60])
            return back(f"/login?next={quote(next)}&invite={quote(invite)}&email={quote(email.strip().lower())}&sent=1",
                        err=str(exc))
        resp = RedirectResponse(next, status_code=303)
        resp.set_cookie(COOKIE, token, max_age=auth.session_days * 86400, httponly=True,
                        secure=bool(cfg["auth"].get("cookie_secure")), samesite="lax", path="/")
        return resp

    @app.post("/logout")
    def logout(request: Request):
        auth.logout(request.cookies.get(COOKIE))
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.get("/me", response_class=HTMLResponse)
    def me(request: Request):
        user = need_user(request)
        mine = db.user_strategies(user["id"])
        versions = {s["name"]: db.versions(s["name"])[:5] for s in mine}
        games = db.games(limit=30, user_id=user["id"])
        return render(request, "me.html", mine=mine, versions=versions, games=games,
                      players=db.game_players_for([g["id"] for g in games]))

    @app.post("/me")
    def me_post(request: Request, name: str = Form("")):
        user = need_user(request)
        db.update_user(user["id"], name=name.strip()[:60] or user["email"].split("@")[0])
        return back("/me", msg="saved")

    # Admin ----------------------------------------------------------------------
    @app.get("/admin", response_class=HTMLResponse)
    def admin(request: Request):
        need_admin(request)
        return render(request, "admin.html", users=db.users(), invites=db.invites(), builds=db.build_queue(),
                      queue=db.game_queue(), strategies=db.strategies(), languages=cfg.languages(),
                      base_url=str(cfg["auth"].get("base_url") or "").rstrip("/"))

    @app.post("/admin/users/{user_id}")
    def admin_user(request: Request, user_id: int, role: str = Form("user"), disabled: str = Form("")):
        me_ = need_admin(request)
        if user_id == me_["id"] and (role != "admin" or disabled == "on"):
            return back("/admin", err="you cannot demote or disable yourself")
        db.update_user(user_id, role="admin" if role == "admin" else "user", disabled=int(disabled == "on"))
        if disabled == "on":
            db.delete_user_sessions(user_id)
        return back("/admin", msg="user updated")

    @app.post("/admin/invites")
    def admin_invite(request: Request, note: str = Form("")):
        user = need_admin(request)
        code = auth.make_invite(user["id"], note.strip()[:100] or None)
        return back("/admin", msg=f"invite created: {code}")

    @app.post("/admin/strategies/{name}")
    def admin_strategy(request: Request, name: str, enabled: str = Form(""), limits: str = Form("")):
        need_admin(request)
        if db.strategy(name) is None:
            raise HTTPException(404)
        parsed = None
        if limits.strip():
            try:
                parsed = json.loads(limits)
                from .strategies import check_limits
                check_limits(parsed, name)
            except (ValueError, TypeError) as exc:
                return back("/admin", err=f"{name}: limits must be a JSON object: {exc}")
        db.update_strategy(name, enabled=int(enabled == "on"), limits=json.dumps(parsed) if parsed else None)
        return back("/admin", msg=f"{name} updated")

    @app.post("/admin/versions/{version_id}/retry")
    def admin_retry(request: Request, version_id: int):
        need_admin(request)
        v = db.version(version_id)
        if v is None:
            raise HTTPException(404)
        with db.tx() as c:
            c.execute("UPDATE strategy_versions SET status='queued', error=NULL WHERE id=?", (version_id,))
        return back("/admin", msg=f"{v['strategy']} v{v['number']} queued again")

    # API --------------------------------------------------------------------------
    @app.get("/api/leaderboard")
    def api_leaderboard():
        return JSONResponse([dict(r) for r in db.leaderboard()])

    @app.get("/api/games/{game_id}")
    def api_game(game_id: int):
        row, players = db.game(game_id)
        if row is None:
            raise HTTPException(404, "no such game")
        return JSONResponse({"game": dict(row), "players": [dict(p) for p in players]})

    # Coding agents ---------------------------------------------------------------------
    def agents_page(request: Request, new_token: str | None = None):
        user = request.state.user
        resp = render(request, "agents.html", tokens=db.api_tokens(user["id"]) if user else [], new_token=new_token,
                      base=ax.base or absolute(request, ""), max_tokens=A.MAX_TOKENS, starter=A.STARTER,
                      languages=cfg.languages(),
                      og=dict(title=f"Coding agents · {title}", url=absolute(request, "/agents"),
                              description="Let Claude Code, Codex, Cursor or any MCP client write, submit and test "
                                          "CodeCraft strategies with your token."))
        if new_token:
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/agents", response_class=HTMLResponse)
    def agents(request: Request):
        return agents_page(request)

    @app.post("/agents/tokens", response_class=HTMLResponse)
    def agents_new_token(request: Request, label: str = Form("")):
        """The token is rendered into this response once (never into a redirect URL) and only its hash is kept."""
        user = need_user(request)
        try:
            token = ax.tokens.create(user["id"], label)
        except A.AgentError as exc:
            return back("/agents", err=str(exc))
        return agents_page(request, new_token=token)

    @app.post("/agents/tokens/{token_id}/revoke")
    def agents_revoke_token(request: Request, token_id: int):
        user = need_user(request)
        if not db.revoke_api_token(user["id"], token_id):
            return back("/agents", err="no such token")
        return back("/agents", msg="token revoked; it stopped working at once")

    @app.api_route(A.UPLOAD_PREFIX + "{rest:anything}", include_in_schema=False,
                   methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def agents_upload(request: Request):
        """upload_link()'s endpoint: the ticket in the path is the credential (agent_gate lets it through).
        It answers every method itself, so nothing under this token-free prefix reaches the MCP app."""
        try:
            rec = await ax.uploads.receive(request.method, request.path_params.get("rest", ""), request.headers,
                                           request.stream())
        except A.UploadError as exc:
            return _json_line({"error": exc.error, **({"hint": exc.hint} if exc.hint else {})}, exc.status,
                              {"Allow": "PUT, POST"} if exc.status == 405 else None)
        except ClientDisconnect:
            return _json_line({"error": "the upload was cut off"}, 400)
        return _json_line(A.Uploads.public(rec), 201)

    @app.api_route("/api/v1/{tool}", methods=["GET", "POST"], include_in_schema=False)
    async def api_v1(request: Request, tool: str):
        """The REST mirror of the MCP tools: POST /api/v1/<tool> with a JSON object of its arguments, or GET
        with query parameters for the read-only ones; Authorization: Bearer <token> (agent_gate)."""
        spec = A.TOOLS.get(tool)
        if spec is None:
            return JSONResponse({"error": f"unknown tool {tool!r}; the tools: {', '.join(A.TOOLS)}"}, status_code=404)
        if request.method == "GET":
            if not spec.read:
                return JSONResponse({"error": f"{tool} changes things: POST a JSON object of its arguments"},
                                    status_code=405, headers={"Allow": "POST"})
            args = dict(request.query_params)
        else:
            body = await request.body()
            try:
                args = json.loads(body) if body.strip() else {}
            except ValueError:
                args = None
            if not isinstance(args, dict):
                return JSONResponse({"error": "the body must be a JSON object of the tool's arguments"}, status_code=400)
        try:
            result = await A.call(ax, tool, request.state.user, args)
        except A.AgentError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if isinstance(result, str):
            return PlainTextResponse(result)
        return JSONResponse(result)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if exc.status_code == 303 and exc.headers:
            return RedirectResponse(exc.headers["Location"], status_code=303)
        if "text/html" in request.headers.get("accept", ""):
            return templates.TemplateResponse(request, "error.html", {"user": request.state.user, "status": exc.status_code,
                                                                      "detail": exc.detail}, status_code=exc.status_code)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    # The MCP endpoint is exactly /mcp/ (streamable_http_path "/" inside the mount). Mounted last: the
    # upload route above must match first.
    app.mount("/mcp", mcp_app)
    return app
