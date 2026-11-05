"""Minimal web UI: leaderboard, games, per-game details, replay/log downloads."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .config import Config
from .db import DB

TEMPLATES = Path(__file__).parent / "templates"


def create_app(cfg: Config, db: DB) -> FastAPI:
    app = FastAPI(title="CodeWars sandbox")
    templates = Jinja2Templates(directory=str(TEMPLATES))

    def fmt_rating(value):
        return "-" if value is None else f"{value:.0f}"

    templates.env.filters["rating"] = fmt_rating

    def game_dir(game_id: int) -> Path:
        return cfg.games_dir / f"{game_id:06d}"

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        rows = db.leaderboard()
        recent = db.games(limit=15)
        players = db.game_players_for([g["id"] for g in recent])
        return templates.TemplateResponse(request, "index.html", {
            "leaderboard": rows, "recent": recent, "players": players, "stats": db.stats(), "cfg": cfg.data,
        })

    @app.get("/games", response_class=HTMLResponse)
    def games(request: Request, page: int = 0):
        limit = 50
        rows = db.games(limit=limit, offset=page * limit)
        players = db.game_players_for([g["id"] for g in rows])
        return templates.TemplateResponse(request, "games.html", {"games": rows, "players": players, "page": page})

    @app.get("/games/{game_id}", response_class=HTMLResponse)
    def game(request: Request, game_id: int):
        row, players = db.game(game_id)
        if row is None:
            raise HTTPException(404, "no such game")
        gdir = game_dir(game_id)
        files = sorted(p.name for p in gdir.iterdir()) if gdir.exists() else []
        return templates.TemplateResponse(request, "game.html", {
            "game": row, "players": players, "files": files, "has_replay": (gdir / "replay.dat").exists(),
            "replay_path": str(gdir / "replay.dat"),
        })

    @app.get("/games/{game_id}/files/{name}")
    def game_file(game_id: int, name: str):
        if "/" in name or name.startswith("."):
            raise HTTPException(400, "bad file name")
        path = game_dir(game_id) / name
        if not path.is_file():
            raise HTTPException(404, "no such file")
        media = "application/octet-stream" if name.endswith(".dat") else "text/plain; charset=utf-8"
        return FileResponse(path, media_type=media, filename=name if name.endswith(".dat") else None)

    @app.get("/strategies/{name}", response_class=HTMLResponse)
    def strategy(request: Request, name: str):
        row = db.strategy(name)
        if row is None:
            raise HTTPException(404, "no such strategy")
        return templates.TemplateResponse(request, "strategy.html", {"s": row, "games": db.strategy_games(name)})

    @app.get("/api/leaderboard")
    def api_leaderboard():
        return JSONResponse([dict(r) for r in db.leaderboard()])

    @app.get("/api/games/{game_id}")
    def api_game(game_id: int):
        row, players = db.game(game_id)
        if row is None:
            raise HTTPException(404, "no such game")
        return JSONResponse({"game": dict(row), "players": [dict(p) for p in players]})

    return app
