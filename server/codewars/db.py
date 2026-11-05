"""SQLite storage: strategies, games, per-seat results and rating history."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import rating as R

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
    name        TEXT PRIMARY KEY,
    title       TEXT,
    language    TEXT,
    gpu         INTEGER DEFAULT 0,
    builtin     TEXT,
    image       TEXT,
    build_hash  TEXT,
    built_at    TEXT,
    enabled     INTEGER DEFAULT 1,
    rating      REAL,
    games       INTEGER DEFAULT 0,
    wins        INTEGER DEFAULT 0,
    crashes     INTEGER DEFAULT 0,
    points      INTEGER DEFAULT 0,
    place_sum   INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS games (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    format       TEXT NOT NULL,
    seed         TEXT,              -- engine seeds are unsigned 64-bit, too big for SQLite INTEGER
    status       TEXT NOT NULL,
    error        TEXT,
    tag          TEXT,
    created_at   TEXT,
    finished_at  TEXT,
    duration_sec REAL
);
CREATE TABLE IF NOT EXISTS game_players (
    game_id       INTEGER NOT NULL REFERENCES games(id),
    slot          INTEGER NOT NULL,
    strategy      TEXT,
    image         TEXT,
    score         INTEGER,
    place         INTEGER,
    points        INTEGER,
    crashed       INTEGER,
    comment       TEXT,
    cpu_sec       REAL,
    rating_before REAL,
    rating_after  REAL,
    PRIMARY KEY (game_id, slot)
);
CREATE INDEX IF NOT EXISTS idx_game_players_strategy ON game_players(strategy, game_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r["name"]: (r["type"] or "").upper() for r in self.conn.execute("PRAGMA table_info(games)")}
        if cols.get("seed") == "INTEGER":  # v0.1.0 stored seeds as INTEGER and lost those >= 2**63
            with self.tx() as c:
                c.execute("ALTER TABLE games RENAME COLUMN seed TO seed_old")
                c.execute("ALTER TABLE games ADD COLUMN seed TEXT")
                c.execute("UPDATE games SET seed = CAST(seed_old AS TEXT)")
                c.execute("ALTER TABLE games DROP COLUMN seed_old")

    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def q(self, sql: str, *params) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, *params) -> sqlite3.Row | None:
        rows = self.q(sql, *params)
        return rows[0] if rows else None

    # Strategies ----------------------------------------------------------
    def upsert_strategy(self, s, initial_rating: float) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO strategies(name, title, language, gpu, builtin, enabled, rating)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET title=excluded.title, language=excluded.language,
                       gpu=excluded.gpu, builtin=excluded.builtin, enabled=excluded.enabled""",
                (s.name, s.title, s.language, int(s.gpu), s.builtin, int(s.enabled), initial_rating),
            )

    def set_image(self, name: str, image: str, build_hash: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE strategies SET image=?, build_hash=?, built_at=? WHERE name=?",
                      (image, build_hash, now(), name))

    def strategies(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM strategies ORDER BY rating DESC, games DESC, name")

    def strategy(self, name: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM strategies WHERE name=?", name)

    def reset_ratings(self, initial: float) -> None:
        with self.tx() as c:
            c.execute("UPDATE strategies SET rating=?, games=0, wins=0, crashes=0, points=0, place_sum=0", (initial,))

    # Games ---------------------------------------------------------------
    def create_game(self, fmt: str, seed: int | None, tag: str | None = None) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO games(format, seed, status, tag, created_at) VALUES (?, ?, 'running', ?, ?)",
                            (fmt, None if seed is None else str(seed), tag, now()))
            return int(cur.lastrowid)

    def finish_game(self, game_id: int, status: str, error: str | None, duration_sec: float, seed: int | None,
                    seats: list[dict], rating_cfg: dict, points_table: list[int]) -> list[dict]:
        """Store per-seat results and, for a finished game, update ratings/stats.
        `seats`: dicts with slot, strategy (None for an Empty seat), image, score, crashed, comment, cpu_sec."""
        scores = [float(s.get("score") or 0) for s in seats]
        places = R.placements(scores)
        pts = R.points(places, points_table)
        rated_idx = [i for i, s in enumerate(seats) if s.get("strategy")]
        with self.tx() as c:
            current = {}
            for i in rated_idx:
                row = c.execute("SELECT rating, games FROM strategies WHERE name=?", (seats[i]["strategy"],)).fetchone()
                current[i] = (float(row["rating"]), int(row["games"])) if row else (float(rating_cfg["initial"]), 0)
            deltas = {i: 0.0 for i in rated_idx}
            if status == "finished" and len(rated_idx) >= 2:
                ratings = [current[i][0] for i in rated_idx]
                ks = [R.k_factor(rating_cfg, current[i][1]) for i in rated_idx]
                for i, d in zip(rated_idx, R.elo_deltas(ratings, [places[i] for i in rated_idx], ks)):
                    deltas[i] = d
            out = []
            for i, s in enumerate(seats):
                before = after = None
                if i in current:
                    before = current[i][0]
                    after = before + deltas[i]
                c.execute(
                    """INSERT OR REPLACE INTO game_players(game_id, slot, strategy, image, score, place, points,
                       crashed, comment, cpu_sec, rating_before, rating_after)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (game_id, s["slot"], s.get("strategy"), s.get("image"), int(scores[i]), places[i], pts[i],
                     int(bool(s.get("crashed"))), s.get("comment"), s.get("cpu_sec"), before, after),
                )
                if i in current and status == "finished":
                    c.execute(
                        """UPDATE strategies SET rating=?, games=games+1, wins=wins+?, crashes=crashes+?,
                           points=points+?, place_sum=place_sum+? WHERE name=?""",
                        (after, int(places[i] == 1), int(bool(s.get("crashed"))), pts[i], places[i], s["strategy"]),
                    )
                out.append({**s, "score": int(scores[i]), "place": places[i], "points": pts[i],
                            "rating_before": before, "rating_after": after})
            c.execute("UPDATE games SET status=?, error=?, finished_at=?, duration_sec=?, seed=? WHERE id=?",
                      (status, error, now(), duration_sec, None if seed is None else str(seed), game_id))
        return out

    def mark_error(self, game_id: int, error: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE games SET status='error', error=?, finished_at=? WHERE id=?", (error, now(), game_id))

    def abort_stale_running(self) -> int:
        """Games left in 'running' by a crashed orchestrator (no process can still be playing them)."""
        with self.tx() as c:
            cur = c.execute("UPDATE games SET status='error', error='orchestrator restarted during the game', "
                            "finished_at=? WHERE status='running'", (now(),))
            return cur.rowcount

    def leaderboard(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM strategies WHERE enabled=1 ORDER BY rating DESC, games DESC, name")

    def games(self, limit: int = 50, offset: int = 0) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM games ORDER BY id DESC LIMIT ? OFFSET ?", limit, offset)

    def game(self, game_id: int):
        game = self.one("SELECT * FROM games WHERE id=?", game_id)
        players = self.q("SELECT * FROM game_players WHERE game_id=? ORDER BY slot", game_id)
        return game, players

    def game_players_for(self, game_ids: list[int]) -> dict[int, list[sqlite3.Row]]:
        if not game_ids:
            return {}
        marks = ",".join("?" * len(game_ids))
        rows = self.q(f"SELECT * FROM game_players WHERE game_id IN ({marks}) ORDER BY game_id, slot", *game_ids)
        out: dict[int, list] = {}
        for r in rows:
            out.setdefault(r["game_id"], []).append(r)
        return out

    def strategy_games(self, name: str, limit: int = 100) -> list[sqlite3.Row]:
        return self.q(
            """SELECT g.*, p.slot, p.score, p.place, p.points, p.crashed, p.comment, p.cpu_sec,
                      p.rating_before, p.rating_after
               FROM game_players p JOIN games g ON g.id = p.game_id
               WHERE p.strategy=? ORDER BY g.id DESC LIMIT ?""", name, limit)

    def stats(self) -> dict:
        row = self.one("SELECT COUNT(*) AS n, SUM(status='finished') AS finished, SUM(status='running') AS running FROM games")
        return dict(row) if row else {"n": 0, "finished": 0, "running": 0}
