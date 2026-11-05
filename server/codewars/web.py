"""Web UI and API: leaderboard, games, replay viewer, submissions, custom games, accounts, admin."""
from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import replay as R
from .auth import Auth, AuthError
from .config import SEATS, Config
from .db import DB
from .submissions import SubmissionError, submit

log = logging.getLogger("codewars.web")
HERE = Path(__file__).parent
COOKIE = "cw_session"


def create_app(cfg: Config, db: DB, auth: Auth | None = None) -> FastAPI:
    auth = auth or Auth(cfg, db)
    title = str(cfg["web"].get("title") or "CodeWars")
    app = FastAPI(title=title, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["rating"] = lambda v: "-" if v is None else f"{v:.0f}"
    if cfg.get("gpu", "available") is None:
        try:
            import docker
            cfg.resolve_gpu(docker.from_env())
        except Exception:  # noqa: BLE001
            cfg.resolve_gpu(None)
    templates.env.globals.update(title=title, SEATS=SEATS, cfg=cfg.data, registration=auth.registration,
                                 gpu_ok=cfg.gpu_ok)

    def game_dir(game_id: int) -> Path:
        return cfg.games_dir / f"{game_id:06d}"

    def render(request: Request, name: str, **ctx):
        ctx.setdefault("user", request.state.user)
        ctx.setdefault("msg", request.query_params.get("m"))
        ctx.setdefault("err", request.query_params.get("e"))
        return templates.TemplateResponse(request, name, ctx)

    def back(url: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
        sep = "&" if "?" in url else "?"
        if msg:
            url = f"{url}{sep}m={quote(msg)}"
        elif err:
            url = f"{url}{sep}e={quote(err)}"
        return RedirectResponse(url, status_code=303)

    def need_user(request: Request) -> dict:
        if request.state.user is None:
            raise HTTPException(303, headers={"Location": f"/login?next={quote(str(request.url.path))}"})
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
        return request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
            request.client.host if request.client else "?")

    @app.middleware("http")
    async def session_middleware(request: Request, call_next):
        request.state.user = auth.user_from_token(request.cookies.get(COOKIE))
        if request.method == "POST":
            origin = request.headers.get("origin")
            host = request.headers.get("host")
            if origin and host and origin.split("://", 1)[-1] != host:
                return PlainTextResponse("cross-site request refused", status_code=403)
        return await call_next(request)

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
            if g is not None and (game_dir(g["id"]) / "replay.dat").exists():
                featured = g
                break
        labels = []
        if featured is not None:
            labels = [p["label"] or p["strategy"] or "empty" for p in db.game(featured["id"])[1]]
        return render(request, "cover.html", featured=featured, labels=labels, stats=db.stats(),
                      n_strategies=len(db.leaderboard()), n_users=len(db.users()))

    @app.get("/rules", response_class=HTMLResponse)
    def rules(request: Request):
        path = cfg.root / "docs" / "doc-en.md"
        if not path.exists():
            raise HTTPException(404, "rules not found")
        try:
            import markdown
            body = markdown.markdown(path.read_text(), extensions=["tables", "fenced_code"])
        except ImportError:
            body = "<pre>" + path.read_text().replace("&", "&amp;").replace("<", "&lt;") + "</pre>"
        return render(request, "rules.html", body=body)

    @app.get("/leaderboard", response_class=HTMLResponse)
    def leaderboard(request: Request):
        recent = db.games(limit=15)
        return render(request, "index.html", leaderboard=db.leaderboard(), recent=recent,
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
    def seat_options(user):
        """Strategies a user may seat: every runnable strategy's active image, plus the user's own
        successfully built older versions."""
        opts = [{"value": "empty", "label": "(empty seat)", "gpu": False}]
        for row in db.runnable_strategies():
            if row["gpu"] and not cfg.gpu_ok:
                continue
            opts.append({"value": row["name"], "label": row["name"] + (" (built-in)" if row["builtin"] else ""),
                         "gpu": bool(row["gpu"])})
            if user and (row["owner_id"] == user["id"] or is_admin(user)):
                for v in db.ok_versions(row["name"]):
                    if v["id"] != row["active_version"] and not (v["gpu"] and not cfg.gpu_ok):
                        opts.append({"value": f"{row['name']}@{v['id']}", "label": f"{row['name']} v{v['number']}",
                                     "gpu": bool(v["gpu"])})
        return opts

    @app.get("/games/new", response_class=HTMLResponse)
    def new_game_form(request: Request):
        user = need_user(request)
        return render(request, "new_game.html", options=seat_options(user), formats=sorted(SEATS),
                      max_gpu=int(cfg.get("gpu", "max_per_game", default=1) or 0) if cfg.gpu_ok else 0)

    @app.post("/games/new")
    async def new_game_post(request: Request):
        user = need_user(request)
        form = await request.form()
        fmt = str(form.get("format") or "Round1")
        if fmt not in SEATS:
            return back("/games/new", err="bad format")
        sub = cfg["submissions"]
        if not is_admin(user):
            if db.user_pending_games(user["id"]) >= int(sub.get("custom_games_queued") or 3):
                return back("/games/new", err="you already have games waiting; let them finish first")
            if db.user_games_today(user["id"]) >= int(sub.get("custom_games_per_day") or 50):
                return back("/games/new", err="daily custom-game limit reached")
        allowed = {o["value"]: o for o in seat_options(user)}
        seats, gpu_count, real = [], 0, 0
        for slot in range(SEATS[fmt]):
            value = str(form.get(f"seat{slot}") or "empty")
            opt = allowed.get(value)
            if opt is None:
                return back("/games/new", err=f"seat {slot + 1}: not available")
            if value == "empty":
                seats.append({"slot": slot, "strategy": None})
                continue
            name, _, vid = value.partition("@")
            row = db.strategy(name)
            version_id, image, label = None, row["image"], name
            if vid:
                v = db.version(int(vid))
                version_id, image, label = v["id"], v["image"], f"{name}-v{v['number']}"
            if row["builtin"] is None:
                real += 1
            gpu_count += int(opt["gpu"])
            seats.append({"slot": slot, "strategy": name, "version_id": version_id, "image": image, "label": label})
        max_gpu = int(cfg.get("gpu", "max_per_game", default=1) or 0) if cfg.gpu_ok else 0
        if gpu_count > max_gpu:
            return back("/games/new", err=f"at most {max_gpu} GPU strategy per game")
        if real < 1:
            return back("/games/new", err="seat at least one real strategy")
        seed_txt = str(form.get("seed") or "").strip()
        seed = None
        if seed_txt:
            try:
                seed = int(seed_txt)
                if not 0 <= seed < 2 ** 64:
                    raise ValueError
            except ValueError:
                return back("/games/new", err="seed must be an unsigned 64-bit integer")
        game_id = db.queue_game(fmt, seed, user["id"], seats, kind="custom", rated=False)
        return back(f"/games/{game_id}", msg="queued; the worker will play it shortly")

    @app.get("/games/{game_id}", response_class=HTMLResponse)
    def game(request: Request, game_id: int):
        row, players = db.game(game_id)
        if row is None:
            raise HTTPException(404, "no such game")
        gdir = game_dir(game_id)
        user = request.state.user
        files = []
        if gdir.exists():
            for p in sorted(gdir.iterdir()):
                if p.name.endswith((".gz", ".tmp")):
                    continue
                if p.name.startswith("player-") and not (user and (is_admin(user) or _owns_slot(user, players, p.name))):
                    continue
                files.append(p.name)
        return render(request, "game.html", game=row, players=players, files=files,
                      has_replay=(gdir / "replay.dat").exists(),
                      labels=[p["label"] or p["strategy"] or "empty" for p in players])

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
        raw = gdir / "replay.dat"
        if not raw.exists():
            raise HTTPException(404, "no replay for this game")
        if not cache.exists() or cache.stat().st_mtime < raw.stat().st_mtime:
            try:
                data = R.to_json(R.load(raw))
            except R.ReplayError as exc:
                raise HTTPException(500, f"replay cannot be decoded: {exc}")
            tmp = cache.with_suffix(".tmp")
            with gzip.open(tmp, "wb", compresslevel=6) as fh:
                fh.write(json.dumps(data, separators=(",", ":")).encode())
            tmp.replace(cache)
        return FileResponse(cache, media_type="application/json",
                            headers={"Content-Encoding": "gzip", "Cache-Control": "public, max-age=86400"})

    @app.get("/games/{game_id}/files/{name}")
    def game_file(request: Request, game_id: int, name: str):
        if "/" in name or name.startswith(".") or name.endswith(".gz"):
            raise HTTPException(400, "bad file name")
        path = game_dir(game_id) / name
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
        return render(request, "strategy.html", s=row, games=db.strategy_games(name), versions=versions,
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
        if request.state.user:
            return RedirectResponse(next if next.startswith("/") else "/", status_code=303)
        inv = db.invite(invite) if invite else None
        invite_ok = inv is not None and inv["used_by"] is None
        return render(request, "login.html", next=next, invite=invite if invite_ok else "", email=email,
                      invite_bad=bool(invite and not invite_ok), sent=request.query_params.get("sent"))

    @app.post("/login/code")
    def login_code(request: Request, email: str = Form(...), next: str = Form("/"), invite: str = Form("")):
        try:
            auth.request_code(email, ip_of(request), invite or None)
        except AuthError as exc:
            return back(f"/login?next={quote(next)}&invite={quote(invite)}", err=str(exc))
        return back(f"/login?next={quote(next)}&invite={quote(invite)}&email={quote(email.strip().lower())}&sent=1")

    @app.post("/login")
    def login_post(request: Request, email: str = Form(...), code: str = Form(...), next: str = Form("/"),
                   invite: str = Form(""), name: str = Form("")):
        try:
            user, token = auth.login(email, code, ip_of(request), request.headers.get("user-agent"),
                                     invite or None, name or None)
        except AuthError as exc:
            return back(f"/login?next={quote(next)}&invite={quote(invite)}&email={quote(email.strip().lower())}&sent=1",
                        err=str(exc))
        resp = RedirectResponse(next if next.startswith("/") else "/", status_code=303)
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

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if exc.status_code == 303 and exc.headers:
            return RedirectResponse(exc.headers["Location"], status_code=303)
        if "text/html" in request.headers.get("accept", ""):
            return templates.TemplateResponse(request, "error.html", {"user": request.state.user, "status": exc.status_code,
                                                                      "detail": exc.detail}, status_code=exc.status_code)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app
