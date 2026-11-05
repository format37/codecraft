"""SQLite storage: users, sessions, strategies and their versions, games, per-seat results."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import rating as R

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT UNIQUE NOT NULL,
    name          TEXT,
    role          TEXT NOT NULL DEFAULT 'user',   -- user | admin
    disabled      INTEGER DEFAULT 0,
    created_at    TEXT,
    last_login_at TEXT
);
CREATE TABLE IF NOT EXISTS login_codes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT NOT NULL,
    code_hash  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts   INTEGER DEFAULT 0,
    used_at    TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_login_codes_email ON login_codes(email, id);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT,
    expires_at TEXT,
    user_agent TEXT,
    ip         TEXT
);
CREATE TABLE IF NOT EXISTS invites (
    code       TEXT PRIMARY KEY,
    created_by INTEGER,
    created_at TEXT,
    used_by    INTEGER,
    used_at    TEXT,
    note       TEXT
);
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
    place_sum   INTEGER DEFAULT 0,
    owner_id    INTEGER,
    kind        TEXT DEFAULT 'seed',    -- seed (strategies/*.yaml) | user (submitted through the web)
    active_version INTEGER,
    limits      TEXT,                   -- JSON overrides of config `limits` (admin-set)
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS strategy_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy    TEXT NOT NULL REFERENCES strategies(name),
    number      INTEGER NOT NULL,
    source_kind TEXT NOT NULL,          -- zip | git
    source      TEXT,                   -- zip: path relative to data/; git: repository url
    git_ref     TEXT,
    commit_hash TEXT,
    language    TEXT NOT NULL,
    gpu         INTEGER DEFAULT 0,
    merge       INTEGER DEFAULT 0,
    include     TEXT,
    note        TEXT,
    status      TEXT NOT NULL DEFAULT 'queued',   -- queued | building | ok | failed
    image       TEXT,
    build_hash  TEXT,
    error       TEXT,
    log_path    TEXT,
    created_by  INTEGER,
    created_at  TEXT,
    started_at  TEXT,
    finished_at TEXT,
    UNIQUE (strategy, number)
);
CREATE TABLE IF NOT EXISTS games (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    format       TEXT NOT NULL,
    seed         TEXT,              -- engine seeds are unsigned 64-bit, too big for SQLite INTEGER
    status       TEXT NOT NULL,     -- queued | running | finished | aborted | error
    error        TEXT,
    tag          TEXT,
    created_at   TEXT,
    finished_at  TEXT,
    duration_sec REAL,
    kind         TEXT DEFAULT 'sandbox',   -- sandbox | custom | tournament
    created_by   INTEGER,
    rated        INTEGER DEFAULT 1,
    started_at   TEXT
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
    version_id    INTEGER,
    label         TEXT,             -- name shown for the seat, e.g. "format37 v3"
    PRIMARY KEY (game_id, slot)
);
CREATE INDEX IF NOT EXISTS idx_game_players_strategy ON game_players(strategy, game_id);
CREATE INDEX IF NOT EXISTS idx_games_status ON games(status, id);
CREATE TABLE IF NOT EXISTS api_tokens (            -- coding agents (MCP / REST), see agents.py
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    token_hash   TEXT UNIQUE NOT NULL,
    label        TEXT,
    created_at   TEXT,
    last_used_at TEXT,
    revoked_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens(user_id, id);
"""

# Columns added after v0.1 (table -> column -> DDL type/default); applied to existing databases.
ADDED_COLUMNS = {
    "strategies": {"owner_id": "INTEGER", "kind": "TEXT DEFAULT 'seed'", "active_version": "INTEGER",
                   "limits": "TEXT", "created_at": "TEXT"},
    "games": {"kind": "TEXT DEFAULT 'sandbox'", "created_by": "INTEGER", "rated": "INTEGER DEFAULT 1",
              "started_at": "TEXT"},
    "game_players": {"version_id": "INTEGER", "label": "TEXT"},
}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def since(hours: float) -> str:
    return ts(datetime.now(timezone.utc) - timedelta(hours=hours))


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
        for table, columns in ADDED_COLUMNS.items():
            present = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, ddl in columns.items():
                if col not in present:
                    with self.tx() as c:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

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

    # Users ---------------------------------------------------------------
    def user(self, user_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM users WHERE id=?", user_id)

    def user_by_email(self, email: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM users WHERE email=?", email)

    def users(self) -> list[sqlite3.Row]:
        return self.q("SELECT u.*, (SELECT COUNT(*) FROM strategies s WHERE s.owner_id=u.id) AS n_strategies "
                      "FROM users u ORDER BY id")

    def create_user(self, email: str, name: str | None, role: str = "user") -> sqlite3.Row:
        with self.tx() as c:
            c.execute("INSERT INTO users(email, name, role, created_at, last_login_at) VALUES (?, ?, ?, ?, ?)",
                      (email, name, role, now(), now()))
        return self.user_by_email(email)

    def touch_login(self, user_id: int, role: str | None = None) -> None:
        with self.tx() as c:
            c.execute("UPDATE users SET last_login_at=? WHERE id=?", (now(), user_id))
            if role:
                c.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))

    def update_user(self, user_id: int, **fields) -> None:
        allowed = {"name", "role", "disabled"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update {sorted(bad)}")
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE users SET {sets} WHERE id=?", (*fields.values(), user_id))

    # Login codes -----------------------------------------------------------
    def issue_code(self, email: str, code_hash: str, ttl_minutes: int) -> int:
        expires = ts(datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes))
        with self.tx() as c:
            # one live code per address: a new request replaces the previous one
            c.execute("UPDATE login_codes SET used_at=? WHERE email=? AND used_at IS NULL", (now(), email))
            cur = c.execute("INSERT INTO login_codes(email, code_hash, expires_at, created_at) VALUES (?, ?, ?, ?)",
                            (email, code_hash, expires, now()))
            return int(cur.lastrowid)

    def check_code(self, email: str, code_hash: str, max_attempts: int, spend: bool = True) -> bool:
        """True when `code_hash` is the live code of `email`. A wrong answer counts against the code;
        after `max_attempts` wrong answers the code is dead."""
        with self.tx() as c:
            row = c.execute("SELECT * FROM login_codes WHERE email=? AND used_at IS NULL ORDER BY id DESC LIMIT 1",
                            (email,)).fetchone()
            if row is None or row["expires_at"] < now() or row["attempts"] >= max_attempts:
                return False
            if row["code_hash"] != code_hash:
                c.execute("UPDATE login_codes SET attempts=attempts+1 WHERE id=?", (row["id"],))
                return False
            if spend:
                c.execute("UPDATE login_codes SET used_at=? WHERE id=?", (now(), row["id"]))
            return True

    def codes_issued_recently(self, email: str, hours: float = 1) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM login_codes WHERE email=? AND created_at>=?", email, since(hours))
        return int(row["n"]) if row else 0

    # Sessions --------------------------------------------------------------
    def create_session(self, token_hash: str, user_id: int, ttl_days: int, user_agent: str | None,
                       ip: str | None) -> str:
        expires = ts(datetime.now(timezone.utc) + timedelta(days=ttl_days))
        with self.tx() as c:
            c.execute("INSERT INTO sessions(token_hash, user_id, created_at, expires_at, user_agent, ip) "
                      "VALUES (?, ?, ?, ?, ?, ?)", (token_hash, user_id, now(), expires, user_agent, ip))
        return expires

    def session_user(self, token_hash: str) -> sqlite3.Row | None:
        return self.one("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                        "WHERE s.token_hash=? AND s.expires_at>? AND u.disabled=0", token_hash, now())

    def delete_session(self, token_hash: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def delete_user_sessions(self, user_id: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    # API tokens (coding agents) --------------------------------------------
    def create_api_token(self, user_id: int, token_hash: str, label: str | None, max_live: int) -> int | None:
        """A new live token, or None when the user already has `max_live` of them."""
        with self.tx() as c:
            live = c.execute("SELECT COUNT(*) FROM api_tokens WHERE user_id=? AND revoked_at IS NULL",
                             (user_id,)).fetchone()[0]
            if live >= max_live:
                return None
            cur = c.execute("INSERT INTO api_tokens(user_id, token_hash, label, created_at) VALUES (?, ?, ?, ?)",
                            (user_id, token_hash, label, now()))
            return int(cur.lastrowid)

    def api_tokens(self, user_id: int) -> list[sqlite3.Row]:
        """The user's live tokens, newest first."""
        return self.q("SELECT id, label, created_at, last_used_at FROM api_tokens "
                      "WHERE user_id=? AND revoked_at IS NULL ORDER BY id DESC", user_id)

    def revoke_api_token(self, user_id: int, token_id: int) -> bool:
        with self.tx() as c:
            cur = c.execute("UPDATE api_tokens SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
                            (now(), token_id, user_id))
            return cur.rowcount == 1

    def api_token_user(self, token_hash: str) -> sqlite3.Row | None:
        """The user behind a live token (a disabled user's tokens stop working with the user), plus the
        token's id and last use as `token_id` / `token_last_used`."""
        return self.one("SELECT u.*, t.id AS token_id, t.last_used_at AS token_last_used FROM api_tokens t "
                        "JOIN users u ON u.id=t.user_id WHERE t.token_hash=? AND t.revoked_at IS NULL "
                        "AND u.disabled=0", token_hash)

    def touch_api_token(self, token_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (now(), token_id))

    # Invites ---------------------------------------------------------------
    def create_invite(self, code: str, created_by: int | None, note: str | None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO invites(code, created_by, created_at, note) VALUES (?, ?, ?, ?)",
                      (code, created_by, now(), note))

    def invite(self, code: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM invites WHERE code=?", code)

    def use_invite(self, code: str, user_id: int) -> bool:
        with self.tx() as c:
            cur = c.execute("UPDATE invites SET used_by=?, used_at=? WHERE code=? AND used_by IS NULL",
                            (user_id, now(), code))
            return cur.rowcount == 1

    def invites(self) -> list[sqlite3.Row]:
        return self.q("SELECT i.*, u.email AS used_email FROM invites i LEFT JOIN users u ON u.id=i.used_by "
                      "ORDER BY i.created_at DESC")

    # Strategies ------------------------------------------------------------
    def upsert_strategy(self, s, initial_rating: float) -> None:
        """Seed strategies from strategies/*/strategy.yaml (kind 'seed')."""
        with self.tx() as c:
            c.execute(
                """INSERT INTO strategies(name, title, language, gpu, builtin, enabled, rating, kind, limits, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'seed', ?, ?)
                   ON CONFLICT(name) DO UPDATE SET title=excluded.title, language=excluded.language,
                       gpu=excluded.gpu, builtin=excluded.builtin, enabled=excluded.enabled, limits=excluded.limits""",
                (s.name, s.title, s.language, int(s.gpu), s.builtin, int(s.enabled), initial_rating,
                 json.dumps(getattr(s, "limits", None) or {}) if getattr(s, "limits", None) else None, now()),
            )

    def create_user_strategy(self, name: str, owner_id: int, title: str, language: str, gpu: bool,
                             initial_rating: float) -> None:
        with self.tx() as c:
            c.execute("""INSERT INTO strategies(name, title, language, gpu, enabled, rating, kind, owner_id, created_at)
                         VALUES (?, ?, ?, ?, 1, ?, 'user', ?, ?)""",
                      (name, title, language, int(gpu), initial_rating, owner_id, now()))

    def set_image(self, name: str, image: str, build_hash: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE strategies SET image=?, build_hash=?, built_at=? WHERE name=?",
                      (image, build_hash, now(), name))

    def update_strategy(self, name: str, **fields) -> None:
        allowed = {"title", "enabled", "limits", "language", "gpu", "owner_id"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update {sorted(bad)}")
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE strategies SET {sets} WHERE name=?", (*fields.values(), name))

    def activate_version(self, version_id: int) -> None:
        with self.tx() as c:
            v = c.execute("SELECT * FROM strategy_versions WHERE id=?", (version_id,)).fetchone()
            if v is None or v["status"] != "ok":
                raise ValueError("only a successfully built version can be activated")
            c.execute("UPDATE strategies SET image=?, build_hash=?, built_at=?, active_version=?, language=?, gpu=? "
                      "WHERE name=?", (v["image"], v["build_hash"], now(), v["id"], v["language"], v["gpu"],
                                       v["strategy"]))

    def strategies(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM strategies ORDER BY rating DESC, games DESC, name")

    def rename_strategy(self, old: str, new: str) -> list[int]:
        """Rename a strategy everywhere: its row, its versions, the seats and labels of past games.
        Returns the ids of the games touched (their cached previews carry the old label). A row that
        already holds the new name is folded in when it is an unused seed (the usual case: the manifest
        was renamed and the seed sync created a fresh row before the rename)."""
        if old == new:
            return []
        if self.strategy(old) is None:
            raise ValueError(f"no strategy {old!r}")
        with self.tx() as c:
            c.execute("PRAGMA defer_foreign_keys=ON")
            taken = c.execute("SELECT kind, games, active_version, image FROM strategies WHERE name=?", (new,)).fetchone()
            if taken is not None:
                versions = c.execute("SELECT COUNT(*) FROM strategy_versions WHERE strategy=?", (new,)).fetchone()[0]
                seats = c.execute("SELECT COUNT(*) FROM game_players WHERE strategy=?", (new,)).fetchone()[0]
                if taken["kind"] != "seed" or taken["games"] or versions or seats or taken["image"]:
                    raise ValueError(f"{new!r} already exists and has history")
                c.execute("DELETE FROM strategies WHERE name=?", (new,))
            games = [r[0] for r in c.execute("SELECT DISTINCT game_id FROM game_players WHERE strategy=?", (old,))]
            c.execute("UPDATE strategies SET name=? WHERE name=?", (new, old))
            c.execute("UPDATE strategy_versions SET strategy=? WHERE strategy=?", (new, old))
            c.execute("UPDATE game_players SET label=? || substr(label, ?) WHERE strategy=? AND "
                      "(label=? OR label LIKE ?)", (new, len(old) + 1, old, old, old + "-v%"))
            c.execute("UPDATE game_players SET strategy=? WHERE strategy=?", (new, old))
        return games

    def strategy(self, name: str) -> sqlite3.Row | None:
        return self.one("SELECT s.*, u.name AS owner_name, u.email AS owner_email FROM strategies s "
                        "LEFT JOIN users u ON u.id=s.owner_id WHERE s.name=?", name)

    def user_strategies(self, owner_id: int) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM strategies WHERE owner_id=? ORDER BY name", owner_id)

    def runnable_strategies(self) -> list[sqlite3.Row]:
        """Enabled strategies that can take a seat: built-in bots and those with a built image."""
        return self.q("SELECT * FROM strategies WHERE enabled=1 AND (builtin IS NOT NULL OR image IS NOT NULL) "
                      "ORDER BY name")

    def sandbox_games_of_build(self, name: str, image: str | None) -> int:
        """Sandbox games the strategy played with this image (a new build starts from zero)."""
        row = self.one("SELECT COUNT(*) AS n FROM game_players p JOIN games g ON g.id=p.game_id "
                       "WHERE p.strategy=? AND p.image IS ? AND g.kind='sandbox' AND g.status IN ('finished','aborted')",
                       name, image)
        return int(row["n"]) if row else 0

    def reset_ratings(self, initial: float) -> None:
        with self.tx() as c:
            c.execute("UPDATE strategies SET rating=?, games=0, wins=0, crashes=0, points=0, place_sum=0", (initial,))

    def leaderboard(self) -> list[sqlite3.Row]:
        return self.q("SELECT s.*, u.name AS owner_name FROM strategies s LEFT JOIN users u ON u.id=s.owner_id "
                      "WHERE s.enabled=1 AND (s.image IS NOT NULL OR s.builtin IS NOT NULL) "
                      "ORDER BY s.rating DESC, s.games DESC, s.name")

    # Strategy versions -----------------------------------------------------
    def create_version(self, strategy: str, created_by: int | None, source_kind: str, source: str | None,
                       git_ref: str | None, language: str, gpu: bool, merge: bool, include: list[str] | None,
                       note: str | None) -> int:
        with self.tx() as c:
            row = c.execute("SELECT COALESCE(MAX(number), 0) AS n FROM strategy_versions WHERE strategy=?",
                            (strategy,)).fetchone()
            number = int(row["n"]) + 1
            cur = c.execute(
                """INSERT INTO strategy_versions(strategy, number, source_kind, source, git_ref, language, gpu, merge,
                   include, note, status, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
                (strategy, number, source_kind, source, git_ref, language, int(gpu), int(merge),
                 json.dumps(include) if include else None, note, created_by, now()))
            return int(cur.lastrowid)

    def version(self, version_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM strategy_versions WHERE id=?", version_id)

    def versions(self, strategy: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM strategy_versions WHERE strategy=? ORDER BY number DESC", strategy)

    def version_by_image(self, strategy: str, image: str, before: str | None) -> sqlite3.Row | None:
        """The version a seat played when it was seated by name (no version_id): the newest one built into
        that image before the game (an unchanged build context reuses the image of an earlier version)."""
        return self.one("SELECT * FROM strategy_versions WHERE strategy=? AND image=? AND created_at<=? "
                        "ORDER BY id DESC LIMIT 1", strategy, image, before or "9999")

    def ok_versions(self, strategy: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM strategy_versions WHERE strategy=? AND status='ok' ORDER BY number DESC", strategy)

    def next_queued_version(self) -> sqlite3.Row | None:
        return self.one("SELECT * FROM strategy_versions WHERE status='queued' ORDER BY id LIMIT 1")

    def build_queue(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM strategy_versions WHERE status IN ('queued', 'building') ORDER BY id")

    def start_version(self, version_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE strategy_versions SET status='building', started_at=?, error=NULL WHERE id=?",
                      (now(), version_id))

    def finish_version(self, version_id: int, status: str, image: str | None = None, build_hash: str | None = None,
                       error: str | None = None, log_path: str | None = None, commit_hash: str | None = None) -> None:
        with self.tx() as c:
            c.execute("""UPDATE strategy_versions SET status=?, image=?, build_hash=?, error=?, log_path=?,
                         commit_hash=COALESCE(?, commit_hash), finished_at=? WHERE id=?""",
                      (status, image, build_hash, error, log_path, commit_hash, now(), version_id))
        if status == "ok":
            self.activate_version(version_id)

    def abort_stale_builds(self) -> int:
        with self.tx() as c:
            cur = c.execute("UPDATE strategy_versions SET status='failed', error='worker restarted during the build', "
                            "finished_at=? WHERE status='building'", (now(),))
            return cur.rowcount

    def user_pending_versions(self, user_id: int) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM strategy_versions WHERE created_by=? AND status IN ('queued','building')",
                       user_id)
        return int(row["n"]) if row else 0

    def user_versions_today(self, user_id: int) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM strategy_versions WHERE created_by=? AND created_at>=?",
                       user_id, since(24))
        return int(row["n"]) if row else 0

    def user_pending_version_rows(self, user_id: int) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM strategy_versions WHERE created_by=? AND status IN ('queued','building') "
                      "ORDER BY id", user_id)

    def versions_ahead(self, version_id: int) -> int:
        """Versions the builder takes before this one (it builds one at a time, oldest first)."""
        row = self.one("SELECT COUNT(*) AS n FROM strategy_versions WHERE status IN ('queued','building') AND id<?",
                       version_id)
        return int(row["n"]) if row else 0

    # Games -----------------------------------------------------------------
    def create_game(self, fmt: str, seed: int | None, tag: str | None = None, kind: str = "sandbox",
                    created_by: int | None = None, rated: bool = True, status: str = "running") -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO games(format, seed, status, tag, created_at, kind, created_by, rated, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (fmt, None if seed is None else str(seed), status, tag, now(), kind, created_by, int(rated),
                 now() if status == "running" else None))
            return int(cur.lastrowid)

    def queue_game(self, fmt: str, seed: int | None, created_by: int | None, seats: list[dict], kind: str = "custom",
                   rated: bool = False, tag: str | None = None) -> int:
        """A game to be played by the worker. `seats`: dicts with slot, strategy, version_id, image, label."""
        game_id = self.create_game(fmt, seed, tag or kind, kind, created_by, rated, status="queued")
        with self.tx() as c:
            for s in seats:
                c.execute("INSERT INTO game_players(game_id, slot, strategy, image, version_id, label) VALUES (?, ?, ?, ?, ?, ?)",
                          (game_id, s["slot"], s.get("strategy"), s.get("image"), s.get("version_id"), s.get("label")))
        return game_id

    def next_queued_game(self) -> sqlite3.Row | None:
        return self.one("SELECT * FROM games WHERE status='queued' ORDER BY id LIMIT 1")

    def claim_game(self, game_id: int) -> bool:
        with self.tx() as c:
            cur = c.execute("UPDATE games SET status='running', started_at=? WHERE id=? AND status='queued'",
                            (now(), game_id))
            return cur.rowcount == 1

    def game_queue(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM games WHERE status IN ('queued', 'running') ORDER BY id")

    def user_pending_games(self, user_id: int) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM games WHERE created_by=? AND status IN ('queued','running')", user_id)
        return int(row["n"]) if row else 0

    def user_games_today(self, user_id: int) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM games WHERE created_by=? AND created_at>=?", user_id, since(24))
        return int(row["n"]) if row else 0

    def user_pending_game_rows(self, user_id: int) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM games WHERE created_by=? AND status IN ('queued','running') ORDER BY id", user_id)

    def games_ahead(self, game_id: int) -> int:
        """Queued games the worker plays before this one (queued games go oldest first)."""
        row = self.one("SELECT COUNT(*) AS n FROM games WHERE status='queued' AND id<?", game_id)
        return int(row["n"]) if row else 0

    def finish_game(self, game_id: int, status: str, error: str | None, duration_sec: float, seed: int | None,
                    seats: list[dict], rating_cfg: dict, points_table: list[int], rated: bool = True) -> list[dict]:
        """Store per-seat results and, for a finished rated game, update ratings/stats.
        `seats`: dicts with slot, strategy (None for an Empty seat), image, score, crashed, comment, cpu_sec,
        version_id, label."""
        scores = [float(s.get("score") or 0) for s in seats]
        places = R.placements(scores)
        pts = R.points(places, points_table)
        rated_idx = [i for i, s in enumerate(seats) if s.get("strategy")]
        update_stats = rated and status == "finished"
        with self.tx() as c:
            current = {}
            for i in rated_idx:
                row = c.execute("SELECT rating, games FROM strategies WHERE name=?", (seats[i]["strategy"],)).fetchone()
                current[i] = (float(row["rating"]), int(row["games"])) if row else (float(rating_cfg["initial"]), 0)
            deltas = {i: 0.0 for i in rated_idx}
            if update_stats and len(rated_idx) >= 2:
                ratings = [current[i][0] for i in rated_idx]
                ks = [R.k_factor(rating_cfg, current[i][1]) for i in rated_idx]
                for i, d in zip(rated_idx, R.elo_deltas(ratings, [places[i] for i in rated_idx], ks)):
                    deltas[i] = d
            out = []
            for i, s in enumerate(seats):
                before = after = None
                if i in current and update_stats:
                    before = current[i][0]
                    after = before + deltas[i]
                c.execute(
                    """INSERT OR REPLACE INTO game_players(game_id, slot, strategy, image, score, place, points,
                       crashed, comment, cpu_sec, rating_before, rating_after, version_id, label)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (game_id, s["slot"], s.get("strategy"), s.get("image"), int(scores[i]), places[i], pts[i],
                     int(bool(s.get("crashed"))), s.get("comment"), s.get("cpu_sec"), before, after,
                     s.get("version_id"), s.get("label")),
                )
                if i in current and update_stats:
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

    def games(self, limit: int = 50, offset: int = 0, kind: str | None = None, strategy: str | None = None,
              user_id: int | None = None, status: str | None = None) -> list[sqlite3.Row]:
        where, params = [], []
        if kind:
            where.append("g.kind=?")
            params.append(kind)
        if status:
            where.append("g.status=?")
            params.append(status)
        if user_id is not None:
            where.append("(g.created_by=? OR g.id IN (SELECT p.game_id FROM game_players p JOIN strategies s "
                         "ON s.name=p.strategy WHERE s.owner_id=?))")
            params += [user_id, user_id]
        if strategy:
            where.append("g.id IN (SELECT game_id FROM game_players WHERE strategy=?)")
            params.append(strategy)
        sql = "SELECT g.* FROM games g" + (" WHERE " + " AND ".join(where) if where else "") + \
              " ORDER BY g.id DESC LIMIT ? OFFSET ?"
        return self.q(sql, *params, limit, offset)

    def game(self, game_id: int):
        game = self.one("SELECT g.*, u.name AS creator_name FROM games g LEFT JOIN users u ON u.id=g.created_by "
                        "WHERE g.id=?", game_id)
        players = self.q("SELECT p.*, s.owner_id FROM game_players p LEFT JOIN strategies s ON s.name=p.strategy "
                         "WHERE p.game_id=? ORDER BY p.slot", game_id)
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
                      p.rating_before, p.rating_after, p.version_id, p.label
               FROM game_players p JOIN games g ON g.id = p.game_id
               WHERE p.strategy=? ORDER BY g.id DESC LIMIT ?""", name, limit)

    def featured_games(self, limit: int = 8) -> list[sqlite3.Row]:
        """Finished games worth showing on the cover: many real (non built-in) strategies, long, recent."""
        return self.q(
            """SELECT g.*, (SELECT COUNT(*) FROM game_players p JOIN strategies s ON s.name=p.strategy
                            WHERE p.game_id=g.id AND s.builtin IS NULL) AS real_players
               FROM games g WHERE g.status='finished'
               ORDER BY real_players DESC, g.duration_sec DESC, g.id DESC LIMIT ?""", limit)

    def stats(self) -> dict:
        row = self.one("SELECT COUNT(*) AS n, SUM(status='finished') AS finished, SUM(status='running') AS running, "
                       "SUM(status='queued') AS queued FROM games")
        return dict(row) if row else {"n": 0, "finished": 0, "running": 0, "queued": 0}
