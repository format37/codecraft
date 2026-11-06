"""Coding agents: API tokens, uploads and the tools behind the MCP server and the REST mirror.

A coding agent (Claude Code, Codex, Cursor, Gemini CLI, a claude.ai connector, curl) works with a
user's token, through MCP at /mcp/ (mcp_server.py) or as GET/POST /api/v1/<tool> (web.py). Both call
the same functions here. A tool is `fn(ax, user, **args)`: `ax` is the `Agents` hub (config, database,
token and upload stores), `user` the token's user row. It returns a dict (JSON) or a str (Markdown or
log text) and raises `AgentError(message)` for anything the agent did wrong or has to wait for; the
message says what to do next. `call()` validates the arguments against the signature, runs the tool in
a thread and does the `wait_s` polling of `version` and `game` on the event loop, so a waiting agent
holds no thread.

A token acts as its user, with three exceptions enforced by having no tool for them: it cannot create
or revoke tokens, cannot change the account, and has no admin action. The writing tools (submit, play,
settings) run with the plain user role even for an admin, so an agent never writes into another user's
strategy or skips a quota; reads keep the role (an admin's agent reads any seat's log, as on the web).
"""
from __future__ import annotations

import functools
import hashlib
import inspect
import io
import json
import os
import re
import secrets
import shutil
import threading
import time
import typing
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import anyio
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from . import replay as R
from .auth import RateLimiter, hash_token
from .builder import ANSI_RE
from .config import LIMIT_KEYS, MAX_TICKS, SEATS, Config
from .db import DB, since
from .submissions import SubmissionError, submit

# Limits. Promote one to config.yaml when somebody needs to change it. ------------------------------
TOKEN_PREFIX = "cc_"
MAX_TOKENS = 5                  # live tokens per user
RATE_LIMIT = 120                # requests per token ...
RATE_WINDOW_S = 60              # ... per this many seconds
TOUCH_EVERY_S = 60              # api_tokens.last_used_at is written at most this often
TICKET_TTL_S = 15 * 60          # an upload link
TICKET_FILES = 3                # files per upload link
UPLOAD_TTL_S = 60 * 60          # an upload nobody submitted
MAX_USER_UPLOADS = 10           # unsubmitted uploads per user
MAX_WAIT_S = 50                 # `wait_s` of version() and game(): under Codex's default 60 s tool timeout
POLL_S = 2.0
MAX_LOG_KB = 64
MAX_GREP_LINES = 200
MAX_LIMIT = 100
MAX_VERSIONS_SHOWN = 30
STARTER = "https://github.com/format37/codecraft-starter"
UPLOAD_PREFIX = "/mcp/upload/"
# A body that is not a zip but one source file is wrapped into a zip: the web form's single-file case.
SOURCE_EXT = {".py", ".java", ".kt", ".scala", ".cpp", ".cc", ".cxx", ".hpp", ".h", ".c", ".cs", ".fs",
              ".d", ".go", ".js", ".rb", ".rs"}
GAME_KINDS = ("sandbox", "custom", "tournament")
GAME_STATUSES = ("queued", "running", "finished", "error", "aborted")
FORMATS = {"Round1": "4 players, no fog of war; each starts with a builder, a melee and a ranged base, a turret, "
                     "one builder, one melee and one ranged unit, and 0 resources",
           "Round2": "4 players, fog of war; each starts with a builder base, one builder and 0 resources",
           "Finals": "1v1, fog of war; each starts with a builder base, one builder and 0 resources"}
REPLAY_JSON_SHAPE = (
    "The full replay, for anything finer than this timeline (positions, every tick, actions): public, no "
    "token, served gzip-encoded, so fetch it with `curl --compressed <url>` (1-4 MB of JSON). Shape: "
    "{map_size, fog_of_war, max_tick_count, tick_count, entity_types (type index -> name), properties "
    "(per type name: size, costs, scores, max_health, sight_range, population, build/attack/repair "
    "abilities), players [[player id, slot, score, resource]], entities at tick 0 [[entity id, player id "
    "(null for a resource), type index, x, y, health, active]], actions at tick 0 {entity id: action}, "
    "ticks [one record per tick: {t: tick, p: changed players, a: added entities, r: removed entity ids, "
    "u: updated entities (whole), act: {entity id: new action}, actr: entity ids whose action was "
    "dropped, ev: [[0, attacker id, target id]] the tick's attacks (mining included)}]}. Entities carry "
    "the player id, not the slot: map it through `players`. Apply the records in order to rebuild any "
    "tick.")


class AgentError(Exception):
    """Something the agent did wrong or has to wait for. The message is shown to it as it is."""


def _utc(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _clamp(value, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(value)))


def _num(value, digits: int = 1):
    return None if value is None else round(float(value), digits)


def _seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    fmt = "%Y-%m-%d %H:%M:%S"
    return (datetime.strptime(end, fmt) - datetime.strptime(start, fmt)).total_seconds()


def is_admin(user) -> bool:
    return bool(user) and user.get("role") == "admin"


def _reads(user: dict, row) -> bool:
    """Read access to a strategy's private side (versions, build logs): its owner, or an admin."""
    return row is not None and (row["owner_id"] == user["id"] or is_admin(user))


def _writer(user: dict) -> dict:
    """The user as the writing tools act: never with the admin role."""
    return {**user, "role": "user"}


# Tokens ---------------------------------------------------------------------------------------------
def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(30)


class Tokens:
    """`cc_` + 40 URL-safe characters, shown once, stored as a sha256 like a session token."""

    def __init__(self, db: DB):
        self.db = db

    def create(self, user_id: int, label: str | None) -> str:
        token = new_token()
        if self.db.create_api_token(user_id, hash_token(token), (label or "").strip()[:60] or None,
                                    MAX_TOKENS) is None:
            raise AgentError(f"you already have {MAX_TOKENS} tokens; revoke one first")
        return token

    def user(self, token: str | None) -> dict | None:
        """The token's user (with `token_id`), or None for an unknown, revoked or disabled one."""
        if not token or not token.startswith(TOKEN_PREFIX) or len(token) > 200:
            return None
        row = self.db.api_token_user(hash_token(token))
        if row is None:
            return None
        user = dict(row)
        last = user.pop("token_last_used")
        if last is None or last < since(TOUCH_EVERY_S / 3600):
            self.db.touch_api_token(user["token_id"])
        return user


# Uploads ----------------------------------------------------------------------------------------------
UPLOAD_ID_RE = re.compile(r"^u_[0-9a-f]{12}$")
MAX_NAME_BYTES = 200
UPLOAD_HINT = ("put the file name at the end of the URL: curl -sS -T strategy.zip <upload URL>/ "
               "(with the trailing slash, curl appends the file's own name)")
# Bidirectional controls can make "invoice‮fdp.exe" display as "invoiceexe.pdf".
_BIDI = dict.fromkeys(map(ord, "؜‎‏‪‫‬‭‮⁦⁧⁨⁩"))
_STRIP = {"Cc", "Cs", "Zl", "Zp"}  # controls, lone surrogates, line and paragraph separators


def clean_name(raw: str) -> str:
    """The name the recipient will see, or "" if none is left: the last path segment, no
    control, separator or bidi characters, NFC (macOS hands curl decomposed letters), and at
    most 200 bytes of UTF-8, cut at a character boundary with the extension kept."""
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if unicodedata.category(ch) not in _STRIP).translate(_BIDI)
    name = unicodedata.normalize("NFC", name).strip()
    if name in ("", ".", ".."):
        return ""
    if len(name.encode()) > MAX_NAME_BYTES:
        stem, ext = os.path.splitext(name)
        if len(ext.encode()) > 20:  # not an extension worth keeping
            stem, ext = name, ""
        stem = stem.encode()[: MAX_NAME_BYTES - len(ext.encode())].decode("utf-8", "ignore")
        name = stem.rstrip() + ext
    return name


class UploadError(Exception):
    def __init__(self, status: int, error: str, hint: str = ""):
        super().__init__(error)
        self.status, self.error, self.hint = status, error, hint


@dataclass
class Ticket:
    token: str
    user_id: int
    expires: float
    used: int = 0      # files stored with it
    active: int = 0    # its uploads running now


class Uploads:
    """Strategy sources an agent hands over with curl before `submit_strategy(upload_id=...)`.

        upload_link() -> curl -sS -T strategy.zip https://<host>/mcp/upload/<ticket>/
                      -> {"id": "u_...", "name", "size", "sha256", "expires"}
                      -> submit_strategy(name, language, upload_id="u_...")

    The ticket is the only credential of the upload route. The curl line lands in the agent's
    transcript, shell history and CI logs, so a ticket lives in memory for minutes, admits a few files
    for its user and can do nothing else; the long-lived token stays in the client's config.

    On disk, under data/uploads/ (0700): <id>/<name> the bytes (0600), <id>.json the record (0600),
    <id>.part an upload in progress, streamed to disk with the size cap applied while reading. A
    submission consumes its upload; the rest expire and are swept by the next upload_link()."""

    def __init__(self, root: Path, max_bytes: int):
        self.root = root
        self.max_bytes = max_bytes
        self.tickets: dict[str, Ticket] = {}
        self.receiving: set[str] = set()
        self.lock = threading.Lock()   # the sweep runs in a worker thread, uploads on the event loop

    @property
    def max_mb(self) -> int:
        return self.max_bytes // (1024 * 1024)

    # tickets
    def new_ticket(self, user_id: int) -> Ticket:
        self.sweep()
        ticket = Ticket(secrets.token_urlsafe(24), user_id, time.time() + TICKET_TTL_S)
        with self.lock:
            self.tickets[ticket.token] = ticket
        return ticket

    def _ticket(self, token: str) -> Ticket | None:
        """A ticket that may take one more file; None if unknown, expired or used up."""
        with self.lock:
            t = self.tickets.get(token or "")
            if t is None or time.time() > t.expires or t.used + t.active >= TICKET_FILES:
                return None
            return t

    # records
    def _read(self, upload_id: str) -> dict | None:
        try:
            return json.loads((self.root / f"{upload_id}.json").read_text())
        except (OSError, ValueError):
            return None

    def _live(self, user_id: int) -> int:
        now = time.time()
        n = 0
        for side in self.root.glob("u_*.json"):
            rec = self._read(side.stem)
            n += bool(rec and rec.get("user_id") == user_id and rec.get("expires", 0) > now)
        return n

    def get(self, user_id: int, upload_id: str) -> tuple[dict, Path]:
        """(record, path of the zip) of a live upload of this user; AgentError otherwise."""
        upload_id = (upload_id or "").strip()
        if not UPLOAD_ID_RE.match(upload_id):
            raise AgentError(f"bad upload id {upload_id!r}: use the id the upload printed (u_ and 12 hex digits)")
        rec = self._read(upload_id)
        path = self.root / upload_id / str((rec or {}).get("stored") or "-")
        if rec is None or rec.get("user_id") != user_id or rec.get("expires", 0) < time.time() or not path.is_file():
            raise AgentError(f"unknown or expired upload {upload_id}; upload again (upload_link)")
        return rec, path

    def consume(self, upload_id: str) -> None:
        (self.root / f"{upload_id}.json").unlink(missing_ok=True)
        shutil.rmtree(self.root / upload_id, ignore_errors=True)

    def sweep(self, now: float | None = None) -> int:
        """Forget expired tickets; delete expired uploads and anything no record owns. Holds the lock
        while it touches the disk, so an upload that is being received is never mistaken for debris."""
        now = time.time() if now is None else now
        gone = 0
        with self.lock:
            for tok, t in list(self.tickets.items()):
                if now > t.expires and not t.active:
                    del self.tickets[tok]
            if not self.root.is_dir():
                return 0
            keep = set()
            for side in self.root.glob("u_*.json"):
                rec = self._read(side.stem)
                if side.stem in self.receiving or (rec is not None and rec.get("expires", 0) > now):
                    keep.add(side.stem)
                    continue
                side.unlink(missing_ok=True)
                shutil.rmtree(self.root / side.stem, ignore_errors=True)
                gone += 1
            for p in self.root.iterdir():
                fid = p.name.split(".", 1)[0]
                if p.suffix != ".json" and fid not in keep and fid not in self.receiving:
                    if p.is_dir() and not p.is_symlink():
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        p.unlink(missing_ok=True)
        return gone

    # receiving
    def _new_id(self) -> str:
        while True:
            fid = "u_" + secrets.token_hex(6)
            if fid not in self.receiving and not (self.root / fid).exists() and not (self.root / f"{fid}.json").exists():
                return fid

    async def receive(self, method: str, rest: str, headers, chunks) -> dict:
        """Store one file under the ticket that starts `rest` (<ticket>/<name>). Raises UploadError and
        leaves nothing behind on failure."""
        if method not in ("PUT", "POST"):
            raise UploadError(405, "upload with PUT: curl -sS -T strategy.zip <upload URL>/")
        token, _, raw_name = rest.partition("/")
        ticket = self._ticket(token)
        if ticket is None:
            raise UploadError(404, "unknown, expired or used-up upload link; call upload_link() for a new one")
        name = clean_name(raw_name)
        if not name:
            raise UploadError(400, "no file name in the URL", UPLOAD_HINT)
        length = headers.get("content-length") or ""
        if length.isdigit() and int(length) > self.max_bytes:
            raise UploadError(413, f"the file is larger than {self.max_mb} MB")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._live(ticket.user_id) >= MAX_USER_UPLOADS:
            raise UploadError(429, f"you have {MAX_USER_UPLOADS} uploads waiting; submit one with submit_strategy "
                                   "or let them expire (60 minutes)")
        with self.lock:
            fid = self._new_id()
            ticket.active += 1
            self.receiving.add(fid)
        part = self.root / f"{fid}.part"
        size, digest = 0, hashlib.sha256()
        try:
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise UploadError(413, f"the file is larger than {self.max_mb} MB")
                    fh.write(chunk)
                    digest.update(chunk)
            if not size:
                raise UploadError(400, "the body is empty", UPLOAD_HINT)
            stored, wrapped = self._as_zip(part, name)
            rec = self._commit(fid, ticket.user_id, name, stored, part, size, digest.hexdigest(), wrapped)
            with self.lock:
                ticket.used += 1
            return rec
        finally:
            with self.lock:
                ticket.active -= 1
                self.receiving.discard(fid)
            part.unlink(missing_ok=True)

    def _as_zip(self, part: Path, name: str) -> tuple[str, bool]:
        """(stored name, wrapped): a zip is kept; one source file becomes a zip holding it."""
        if zipfile.is_zipfile(part):
            return name, False
        if Path(name).suffix.lower() not in SOURCE_EXT:
            raise UploadError(400, "upload a zip archive of the strategy folder, or one source file such as "
                                   "my_strategy.py")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(name, part.read_bytes())
        part.write_bytes(buf.getvalue())
        return name + ".zip", True

    def _commit(self, fid: str, user_id: int, name: str, stored: str, part: Path, size: int, sha256: str,
                wrapped: bool) -> dict:
        (self.root / fid).mkdir(mode=0o700)
        os.replace(part, self.root / fid / stored)
        now = time.time()
        rec = {"id": fid, "user_id": user_id, "name": name, "stored": stored, "size": size, "sha256": sha256,
               "wrapped": wrapped, "created": now, "expires": now + UPLOAD_TTL_S}
        fd = os.open(self.root / f"{fid}.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(rec, fh)
        return rec

    @staticmethod
    def public(rec: dict) -> dict:
        """The JSON line an upload answers with."""
        out = {"id": rec["id"], "name": rec["name"], "size": rec["size"], "sha256": rec["sha256"],
               "expires": _utc(rec["expires"])}
        if rec.get("wrapped"):
            out["wrapped"] = "not a zip: stored as a zip that holds this one file (the single-file submission)"
        out["next"] = f'submit_strategy(name=..., language=..., upload_id="{rec["id"]}")'
        return out


# The hub ------------------------------------------------------------------------------------------------
class Agents:
    """What the agent surface keeps: config, database, token store, upload store, per-token limiter."""

    def __init__(self, cfg: Config, db: DB):
        self.cfg, self.db = cfg, db
        self.tokens = Tokens(db)
        self.uploads = Uploads(cfg.uploads_dir, int(cfg["submissions"].get("max_zip_mb") or 20) * 1024 * 1024)
        self.limiter = RateLimiter(RATE_LIMIT, RATE_WINDOW_S)
        self.base = str(cfg["auth"].get("base_url") or "").rstrip("/")

    def url(self, path: str) -> str:
        return self.base + path

    def game_dir(self, game_id: int) -> Path:
        return self.cfg.games_dir / f"{game_id:06d}"


# Tool registry -------------------------------------------------------------------------------------------
@dataclass
class ToolSpec:
    fn: Callable
    read: bool                                        # read-only: also served on GET by the REST mirror
    pending: Callable[[dict], bool] | None = None     # `wait_s` keeps polling while this holds
    model: type[BaseModel] | None = None


TOOLS: dict[str, ToolSpec] = {}


def tool(read: bool, pending: Callable[[dict], bool] | None = None):
    def register(fn):
        TOOLS[fn.__name__] = ToolSpec(fn, read, pending)
        return fn
    return register


def arguments(name: str) -> type[BaseModel]:
    """The tool's arguments as a pydantic model built from its signature (after `ax, user`)."""
    spec = TOOLS[name]
    if spec.model is None:
        hints = typing.get_type_hints(spec.fn)
        fields: dict[str, Any] = {}
        for p in list(inspect.signature(spec.fn).parameters.values())[2:]:
            fields[p.name] = (hints.get(p.name, Any), ... if p.default is inspect.Parameter.empty else p.default)
        spec.model = create_model(f"{name}_arguments", __config__=ConfigDict(extra="forbid"), **fields)
    return spec.model


def _explain(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(x) for x in e.get('loc', ())) or 'arguments'}: {e.get('msg')}"
                     for e in exc.errors())


async def call(ax: Agents, name: str, user: dict, args: dict) -> dict | str:
    """Run a tool for `user`: arguments checked against its signature, the work in a thread, and for a
    tool with `pending`, `wait_s` polling every POLL_S seconds on the event loop until it is over."""
    spec = TOOLS[name]
    try:
        parsed = arguments(name).model_validate(args)
    except ValidationError as exc:
        raise AgentError(f"bad arguments for {name}: {_explain(exc)}") from None
    kwargs = {k: getattr(parsed, k) for k in type(parsed).model_fields}
    run = functools.partial(spec.fn, ax, user, **kwargs)
    wait = _clamp(kwargs.get("wait_s") or 0, 0, MAX_WAIT_S) if spec.pending else 0
    deadline = time.monotonic() + wait
    while True:
        result = await anyio.to_thread.run_sync(run)
        left = deadline - time.monotonic()
        if left <= 0 or not spec.pending(result):
            return result
        await anyio.sleep(min(POLL_S, left))


# Custom games: the rules of the web form, shared with `play` ------------------------------------------
def seat_options(cfg: Config, db: DB, user) -> list[dict]:
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


def _unavailable(cfg: Config, db: DB, user, value: str) -> str:
    """Why a seat value is not among the seat options."""
    name, _, vid = value.partition("@")
    row = db.strategy(name)
    if row is None:
        return "no such strategy"
    if not row["enabled"]:
        return "the strategy is paused (disabled)"
    if row["builtin"] is None and not row["image"]:
        return "the strategy has no successful build yet"
    if row["gpu"] and not cfg.gpu_ok:
        return "it needs a GPU and this server has none"
    if vid:
        v = db.version(int(vid)) if vid.isdigit() else None
        if v is None or v["strategy"] != name:
            return f"{name} has no version id {vid}"
        if not (user and (row["owner_id"] == user["id"] or is_admin(user))):
            return "an older version can be seated by its owner only"
        if v["status"] != "ok":
            return f"version id {vid} is not built ({v['status']})"
    return "not a seat option"


def queue_custom_game(cfg: Config, db: DB, user: dict, fmt: str, seats: list[str],
                      seed: int | str | None = None) -> int:
    """Queue an unrated custom game under the web form's rules (quotas, seat options, the GPU cap, at
    least one real strategy, the seed range). `seats` holds one value per seat of the format: "empty", a
    strategy name or name@version_id. Returns the game id; AgentError with the reason otherwise."""
    if fmt not in SEATS:
        raise AgentError(f"bad format {fmt!r}; one of {', '.join(SEATS)}")
    if len(seats) != SEATS[fmt]:
        raise AgentError(f"{fmt} has {SEATS[fmt]} seats, got {len(seats)}")
    sub = cfg["submissions"]
    if not is_admin(user):
        pending = db.user_pending_game_rows(user["id"])
        if len(pending) >= int(sub.get("custom_games_queued") or 3):
            waiting = ", ".join(f"#{g['id']} ({g['status']})" for g in pending)
            raise AgentError(f"you already have games waiting: {waiting}; let them finish first")
        per_day = int(sub.get("custom_games_per_day") or 50)
        if db.user_games_today(user["id"]) >= per_day:
            raise AgentError(f"daily custom-game limit reached ({per_day} in 24 hours)")
    allowed = {o["value"]: o for o in seat_options(cfg, db, user)}
    rows, gpu_count, real = [], 0, 0
    for slot, value in enumerate(seats):
        value = (value or "empty").strip()
        name, _, vid = value.partition("@")
        if vid.isdigit():                       # name@<the active version> is the plain name
            row = db.strategy(name)
            if row is not None and row["active_version"] == int(vid):
                value = name
        opt = allowed.get(value)
        if opt is None:
            raise AgentError(f"seat {slot + 1} ({value}): not available: {_unavailable(cfg, db, user, value)}")
        if value == "empty":
            rows.append({"slot": slot, "strategy": None})
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
        rows.append({"slot": slot, "strategy": name, "version_id": version_id, "image": image, "label": label})
    max_gpu = int(cfg.get("gpu", "max_per_game", default=1) or 0) if cfg.gpu_ok else 0
    if gpu_count > max_gpu:
        raise AgentError(f"at most {max_gpu} GPU strategy per game")
    if real < 1:
        raise AgentError("seat at least one real strategy")
    seed_value = None
    # An agent told to pass the seed "as a string" sometimes sends the quotes as well: '"42"'.
    text = None if seed is None else str(seed).strip().strip("\"'").strip()
    if text:
        try:
            seed_value = int(text)
            if not 0 <= seed_value < 2 ** 64:
                raise ValueError
        except ValueError:
            raise AgentError(f"seed must be an unsigned 64-bit integer (a string of digits), not {seed!r}") from None
    return db.queue_game(fmt, seed_value, user["id"], rows, kind="custom", rated=False)


# Row shapes ----------------------------------------------------------------------------------------------
SAFE_INT = 2 ** 53   # above it a JSON number is rounded by JavaScript clients


def seed_arg(value) -> str | None:
    """A seed as play() takes it: a string of digits. The live check of 2026-11-05: Claude Code sent the
    seed 11249517513654704893 as a JSON number, it arrived as 11249517513654704000, and the game ran on
    another map. A number above 2**53 may already have lost digits, so it is refused, not played."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentError("seed is a string of digits, e.g. \"7\"")
    if value > SAFE_INT:
        raise AgentError(f"pass the seed as a string, as game() gives it: as a JSON number a seed above 2^53 "
                         f"loses its last digits on the way, so {value} may not be the seed you meant")
    return str(value)


def _seed(value) -> str | None:
    """Seeds are unsigned 64-bit; a JSON number above 2**53 loses digits in a JavaScript client (an agent
    that passed one back to play() got another map), so they travel as strings, as the database keeps them."""
    return None if value is None else str(value)


def _avg_place(row) -> float | None:
    return round(row["place_sum"] / row["games"], 2) if row["games"] else None


def _source(v) -> dict:
    if v["source_kind"] == "git":
        return {"kind": "git", "url": v["source"], "ref": v["git_ref"], "commit": v["commit_hash"]}
    return {"kind": "zip"}


def _version_brief(v) -> dict | None:
    if v is None:
        return None
    out = {"id": v["id"], "number": v["number"], "status": v["status"]}
    if v["error"]:
        out["error"] = build_error(v["error"])[:300]
    return out


def _version_row(v, s) -> dict:
    return {"id": v["id"], "number": v["number"], "status": v["status"], "active": v["id"] == s["active_version"],
            "language": v["language"], "source": _source(v), "note": v["note"],
            "error": (build_error(v["error"]) or "")[:300] or None, "created_at": v["created_at"], "finished_at": v["finished_at"]}


def _seat_brief(p) -> dict:
    return {"slot": p["slot"], "strategy": p["strategy"], "label": p["label"] or p["strategy"] or "empty",
            "score": p["score"], "place": p["place"], "crashed": None if p["crashed"] is None else bool(p["crashed"])}


def _game_row(ax: Agents, g, seats) -> dict:
    out = {"id": g["id"], "kind": g["kind"], "format": g["format"], "status": g["status"], "rated": bool(g["rated"]),
           "seed": _seed(g["seed"]), "created_at": g["created_at"], "duration_sec": _num(g["duration_sec"])}
    if g["error"]:
        out["error"] = g["error"]
    out["seats"] = [_seat_brief(p) for p in seats]
    out["url"] = ax.url(f"/games/{g['id']}")
    return out


def _strategy_public(ax: Agents, s, user: dict) -> dict:
    return {"name": s["name"], "title": s["title"], "owner": s["owner_name"] or ("engine" if s["builtin"] else "server"),
            "mine": s["owner_id"] == user["id"], "kind": s["kind"], "builtin": bool(s["builtin"]),
            "language": s["language"] or s["builtin"], "gpu": bool(s["gpu"]), "enabled": bool(s["enabled"]),
            "rating": _num(s["rating"]), "games": s["games"], "wins": s["wins"], "avg_place": _avg_place(s),
            "points": s["points"], "crashes": s["crashes"], "active_version_id": s["active_version"],
            "built_at": s["built_at"], "url": ax.url(f"/strategies/{s['name']}")}


def build_error(text: str | None) -> str | None:
    """A stored build error as its owner reads it. Errors stored before 2026-11-06 carry terminal colours,
    "(see <log path>)" and the server's clone path; the builder no longer writes them."""
    if not text:
        return text
    text = re.sub(r"\s*\(see /[^)]*\)", "", ANSI_RE.sub("", text))
    return re.sub(r"/[\w./-]*/build/repos/[\w.-]+", "<clone>", text)


def _tail(path: Path, nbytes: int) -> str:
    """The last `nbytes` of a text file, from a line start when one is near, without terminal colours."""
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > nbytes:
            fh.seek(size - nbytes)
        text = fh.read(nbytes).decode("utf-8", errors="replace")
    if size > nbytes:
        cut = text.find("\n")
        text = "[...]\n" + (text[cut + 1:] if 0 <= cut < 300 else text)
    return ANSI_RE.sub("", text)


def _slot_of_file(name: str) -> int | None:
    try:
        return int(name.split("-")[1].split(".")[0])
    except (IndexError, ValueError):
        return None


# Read tools ----------------------------------------------------------------------------------------------
@tool(read=True)
def whoami(ax: Agents, user: dict) -> dict:
    db, cfg = ax.db, ax.cfg
    sub = cfg["submissions"]
    strategies = []
    for s in db.user_strategies(user["id"]):
        versions = db.versions(s["name"])
        active = next((v for v in versions if v["id"] == s["active_version"]), None)
        strategies.append({
            "name": s["name"], "title": s["title"], "language": s["language"], "enabled": bool(s["enabled"]),
            "rating": _num(s["rating"]), "games": s["games"], "wins": s["wins"],
            "active_version": {"id": active["id"], "number": active["number"]} if active else None,
            "latest_version": _version_brief(versions[0] if versions else None),
            "url": ax.url(f"/strategies/{s['name']}")})
    pending_versions = db.user_pending_version_rows(user["id"])
    pending_games = db.user_pending_game_rows(user["id"])
    limits = cfg["limits"]
    return {
        "user": {"name": user["name"], "role": user["role"]},
        "strategies": strategies,
        "pending": {
            "versions": [{"id": v["id"], "strategy": v["strategy"], "number": v["number"], "status": v["status"]}
                         for v in pending_versions],
            "games": [{"id": g["id"], "format": g["format"], "status": g["status"]} for g in pending_games]},
        "quotas": {
            "versions_today": db.user_versions_today(user["id"]),
            "versions_per_day": int(sub.get("per_user_per_day") or 20),
            "versions_waiting": len(pending_versions),
            "versions_waiting_allowed": int(sub.get("per_user_queued") or 1),
            "custom_games_today": db.user_games_today(user["id"]),
            "custom_games_per_day": int(sub.get("custom_games_per_day") or 50),
            "custom_games_waiting": len(pending_games),
            "custom_games_waiting_allowed": int(sub.get("custom_games_queued") or 3),
            "note": "'today' is the last 24 hours; 'waiting' counts queued plus building (versions) or running (games)"},
        "server": {
            "formats": {f: {"seats": SEATS[f], "about": FORMATS[f]} for f in SEATS},
            "languages": cfg.languages(),
            "gpu": cfg.gpu_ok,
            "limits": {k: limits.get(k) for k in ("tick_timeout_sec", "cpu_time_sec", "memory_mb", "cpus")},
            "limits_about": "per strategy container: tick_timeout_sec of real time to answer each tick, "
                            "cpu_time_sec of CPU for the whole game, memory_mb for the whole container (the "
                            "2020 rules' 256 MB was for the program alone; a JVM needs the headroom). The "
                            "filesystem is read-only except /tmp (256 MB, counted in memory_mb), the log keeps "
                            "its last 10 MB, and the only network is the engine's port of your own seat. A "
                            "build compiles without network: what a language needs beyond its official client "
                            "is not there"
                            + (". Strategies and builds run under gVisor, a kernel in user space between them and "
                               "the host: ordinary programs do not notice it, but uname and /proc describe gVisor "
                               "rather than the host" if cfg.get("docker", "runtime") == "runsc" else ""),
            "ticks": MAX_TICKS,
            "map_size": 80,
            "sandbox_games_per_build": int(cfg.get("worker", "sandbox_games_per_build", default=10) or 0),
            "max_zip_mb": int(sub.get("max_zip_mb") or 20),
            "git_submissions": bool(sub.get("allow_git", True))},
        "links": {"site": ax.base, "rules": ax.url("/rules"), "leaderboard": ax.url("/leaderboard"),
                  "agents": ax.url("/agents"), "starter": STARTER},
    }


HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, title) of the Markdown headings outside code fences."""
    out, fenced = [], False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        m = None if fenced else HEADING_RE.match(line)
        if m:
            out.append((i, len(m[1]), m[2].replace("`", "")))
    return out


@tool(read=True)
def rules(ax: Agents, user: dict, section: str | None = None) -> str:
    path = ax.cfg.root / "docs" / "doc-en.md"
    if not path.is_file():
        raise AgentError("the rules are not installed on this server; read them at " + ax.url("/rules"))
    text = path.read_text()
    if not section or not section.strip():
        return text
    lines = text.splitlines()
    heads = _headings(lines)
    needle = section.strip().strip("#` ").lower()
    for n, (i, level, title) in enumerate(heads):
        if needle in title.lower():
            end = next((j for j, lv, _ in heads[n + 1:] if lv <= level), len(lines))
            return "\n".join(lines[i:end]).strip() + "\n"
    raise AgentError(f"no section matching {section!r}; the headings: " + ", ".join(t for _, _, t in heads))


@tool(read=True)
def leaderboard(ax: Agents, user: dict, limit: int = 30) -> dict:
    rows = ax.db.leaderboard()
    shown = rows[:_clamp(limit, 1, MAX_LIMIT)]
    return {"strategies": [
        {"rank": i, "name": r["name"], "title": r["title"] if r["title"] and r["title"] != r["name"] else None,
         "owner": r["owner_name"] or ("engine" if r["builtin"] else "server"), "mine": r["owner_id"] == user["id"],
         "language": r["language"] or r["builtin"], "rating": _num(r["rating"]), "games": r["games"],
         "wins": r["wins"], "avg_place": _avg_place(r), "points": r["points"], "crashes": r["crashes"],
         "builtin": bool(r["builtin"])} for i, r in enumerate(shown, 1)],
        "total": len(rows)}


@tool(read=True)
def strategy(ax: Agents, user: dict, name: str) -> dict:
    db, cfg = ax.db, ax.cfg
    s = db.strategy((name or "").strip().lower())
    if s is None:
        raise AgentError(f"no strategy {name!r}; leaderboard() lists the active ones, whoami() yours")
    out = _strategy_public(ax, s, user)
    if _reads(user, s):
        versions = db.versions(s["name"])
        out["versions"] = [_version_row(v, s) for v in versions[:MAX_VERSIONS_SHOWN]]
        out["versions_total"] = len(versions)
        overrides = json.loads(s["limits"]) if s["limits"] else {}
        out["limits"] = {k: overrides.get(k, cfg["limits"].get(k)) for k in sorted(LIMIT_KEYS)}
        per_build = int(cfg.get("worker", "sandbox_games_per_build", default=10) or 0)
        out["sandbox"] = {
            "games_per_build": per_build or "unlimited",
            "played_by_active_build": db.sandbox_games_of_build(s["name"], s["image"]) if s["image"] else 0,
            "about": "every new active version of an enabled strategy plays this many rated sandbox games, "
                     "scheduled by the server; custom games (play) are unrated"}
    rows = db.games(limit=20, strategy=s["name"])   # one row per game, also when it held several seats
    seats = db.game_players_for([g["id"] for g in rows])
    out["games"] = [_game_row(ax, g, seats.get(g["id"], [])) for g in rows]
    return out


@tool(read=True, pending=lambda r: r["status"] in ("queued", "building"))
def version(ax: Agents, user: dict, version_id: int, wait_s: int = 0, log_tail_kb: int = 8) -> dict:
    """`wait_s` is applied by `call`, which polls this function while the version is pending."""
    db, cfg = ax.db, ax.cfg
    v = db.version(version_id)
    if v is None:
        raise AgentError(f"no version id {version_id}; strategy(name) lists a strategy's versions")
    s = db.strategy(v["strategy"])
    if not _reads(user, s):
        raise AgentError(f"version id {version_id} belongs to another user's strategy")
    out = {"id": v["id"], "strategy": v["strategy"], "number": v["number"], "status": v["status"],
           "active": v["id"] == s["active_version"], "language": v["language"], "gpu": bool(v["gpu"]),
           "source": _source(v), "note": v["note"], "error": build_error(v["error"]), "image": v["image"],
           "created_at": v["created_at"], "started_at": v["started_at"], "finished_at": v["finished_at"],
           "build_sec": _seconds(v["started_at"], v["finished_at"])}
    if v["status"] == "queued":
        out["queued_ahead"] = db.versions_ahead(v["id"])
    kb = _clamp(log_tail_kb, 0, MAX_LOG_KB)
    if kb:
        path = (cfg.data_dir / v["log_path"] if v["log_path"]
                else cfg.build_dir / "logs" / f"{v['strategy']}-v{v['number']}.log")
        if path.is_file():
            out["log_tail"] = _tail(path, kb * 1024)
    out["next"] = {
        "queued": f"version({v['id']}, wait_s=50) again: it waits for the builder",
        "building": f"version({v['id']}, wait_s=50) again: a build takes seconds to 10 minutes",
        "ok": f"active: play(opponents=[\"quickstart\"], mine=\"{v['strategy']}\") for a custom game"
              if v["id"] == s["active_version"] else f"built; set_strategy(\"{v['strategy']}\", activate_version={v['id']}) makes it active",
        "failed": "read error and log_tail, fix the code and submit again",
    }.get(v["status"], "")
    return out


@tool(read=True)
def games(ax: Agents, user: dict, strategy: str | None = None, kind: str | None = None, status: str | None = None,
          limit: int = 20) -> dict:
    db = ax.db
    if kind and kind not in GAME_KINDS:
        raise AgentError(f"kind is one of {', '.join(GAME_KINDS)}")
    if status and status not in GAME_STATUSES:
        raise AgentError(f"status is one of {', '.join(GAME_STATUSES)}")
    name = (strategy or "").strip().lower() or None
    if name and db.strategy(name) is None:
        raise AgentError(f"no strategy {strategy!r}")
    rows = db.games(limit=_clamp(limit, 1, MAX_LIMIT), kind=kind or None, strategy=name,
                    user_id=None if name else user["id"], status=status or None)
    seats = db.game_players_for([g["id"] for g in rows])
    return {"games": [_game_row(ax, g, seats.get(g["id"], [])) for g in rows]}


@tool(read=True, pending=lambda r: r["status"] in ("queued", "running"))
def game(ax: Agents, user: dict, game_id: int, wait_s: int = 0) -> dict:
    """`wait_s` is applied by `call`, which polls this function while the game is pending."""
    db = ax.db
    row, players = db.game(game_id)
    if row is None:
        raise AgentError(f"no game #{game_id}")
    gdir = ax.game_dir(game_id)
    mine = [p["slot"] for p in players if p["owner_id"] is not None and p["owner_id"] == user["id"]]
    files = []
    if gdir.is_dir():
        for p in sorted(gdir.iterdir()):
            if p.name.endswith((".gz", ".tmp")) or p.name.startswith("."):
                continue
            if p.name.startswith("player-") and not (is_admin(user) or _slot_of_file(p.name) in mine):
                continue
            files.append(p.name)
    has_replay = (gdir / "replay.dat").is_file()

    def played(p) -> dict:
        """The version in the seat: recorded when an older one was seated, else the one built into the image."""
        v = db.version(p["version_id"]) if p["version_id"] else (
            db.version_by_image(p["strategy"], p["image"], row["created_at"]) if p["strategy"] and p["image"] else None)
        return {"version_id": v["id"] if v else p["version_id"], "version": v["number"] if v else None}

    out = {"id": row["id"], "kind": row["kind"], "format": row["format"], "status": row["status"],
           "rated": bool(row["rated"]), "seed": _seed(row["seed"]), "error": row["error"],
           "created_at": row["created_at"], "started_at": row["started_at"], "finished_at": row["finished_at"],
           "duration_sec": _num(row["duration_sec"]), "created_by": row["creator_name"],
           "seats": [{"slot": p["slot"], "strategy": p["strategy"], "label": p["label"] or p["strategy"] or "empty",
                      **played(p), "score": p["score"], "place": p["place"], "points": p["points"],
                      "rating_before": _num(p["rating_before"]), "rating_after": _num(p["rating_after"]),
                      "cpu_sec": _num(p["cpu_sec"], 2), "crashed": None if p["crashed"] is None else bool(p["crashed"]),
                      "comment": p["comment"], "mine": p["slot"] in mine} for p in players],
           "my_slots": mine, "files": files, "url": ax.url(f"/games/{row['id']}"),
           "replay_json": ax.url(f"/games/{row['id']}/replay.json") if has_replay else None}
    if row["status"] == "queued":
        out["queued_ahead"] = db.games_ahead(row["id"])
    if row["status"] in ("queued", "running"):
        out["next"] = f"game({row['id']}, wait_s=50) again until it is finished"
    elif has_replay:
        out["next"] = (f"game_timeline({row['id']}) for the course of the game"
                       + (f", game_log({row['id']}) for your strategy's output" if mine else ""))
    else:
        out["next"] = f"no replay; game_log({row['id']}, slot=\"engine\") shows the engine's log"
    return out


@tool(read=True)
def game_timeline(ax: Agents, user: dict, game_id: int, every: int = 50) -> dict:
    row, players = ax.db.game(game_id)
    if row is None:
        raise AgentError(f"no game #{game_id}")
    path = ax.game_dir(game_id) / "replay.dat"
    if not path.is_file():
        if row["status"] in ("queued", "running"):
            raise AgentError(f"game #{game_id} is {row['status']}, no replay yet; wait with game({game_id}, wait_s=50)")
        raise AgentError(f"game #{game_id} has no replay ({row['status']}" + (f": {row['error']})" if row["error"] else ")"))
    try:
        tl = R.timeline(R.load(path), every)
    except R.ReplayError as exc:
        raise AgentError(f"the replay of game #{game_id} cannot be decoded: {exc}") from None
    seats = [{"slot": p["slot"], "label": p["label"] or p["strategy"] or "empty", "strategy": p["strategy"],
              "score": p["score"], "place": p["place"], "mine": p["owner_id"] is not None and p["owner_id"] == user["id"]}
             for p in players]
    return {"game_id": row["id"], "format": row["format"], "status": row["status"], "seats": seats,
            "tick_count": tl["tick_count"], "map_size": tl["map_size"], "fog_of_war": tl["fog_of_war"],
            "every": tl["every"], "samples": tl["samples"], "events": tl["events"],
            "replay_json": ax.url(f"/games/{row['id']}/replay.json"), "replay_json_shape": REPLAY_JSON_SHAPE}


@tool(read=True)
def game_log(ax: Agents, user: dict, game_id: int, slot: int | str | None = None, tail_kb: int = 16,
             grep: str | None = None) -> str:
    row, players = ax.db.game(game_id)
    if row is None:
        raise AgentError(f"no game #{game_id}")
    gdir = ax.game_dir(game_id)
    if isinstance(slot, str) and slot.strip().lower() == "engine":
        path, what = gdir / "engine.log", f"engine.log of game #{game_id}"
    else:
        if isinstance(slot, str):
            if not slot.strip().isdigit():
                raise AgentError('slot is a seat number (0-based) or "engine"')
            slot = int(slot.strip())
        if slot is None:
            mine = [p for p in players if p["owner_id"] is not None and p["owner_id"] == user["id"]]
            if not mine:
                raise AgentError(f'you have no seat in game #{game_id}; slot="engine" gives the engine\'s log')
            slot = mine[0]["slot"]
        seat = next((p for p in players if p["slot"] == slot), None)
        if seat is None:
            raise AgentError(f"game #{game_id} has no slot {slot} (slots 0..{len(players) - 1})")
        if not (is_admin(user) or (seat["owner_id"] is not None and seat["owner_id"] == user["id"])):
            raise AgentError("a strategy's log is visible to its owner only")
        label = seat["label"] or seat["strategy"] or "empty"
        path, what = gdir / f"player-{slot}.log", f"player-{slot}.log of game #{game_id} (slot {slot}, {label})"
    if not path.is_file():
        raise AgentError(f"no {path.name} for game #{game_id} (status {row['status']})")
    size = path.stat().st_size
    if grep:
        # Plain text, never a regular expression: Python's engine holds the GIL, and a pattern such as
        # (a|aa)+$ over a line the caller wrote itself would freeze the whole web process.
        needles = [n for n in grep.lower().split("|") if n]
        hits, total, n = [], 0, 0
        with path.open("rb") as fh:
            for n, raw in enumerate(fh, 1):
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                low = line.lower()
                if any(needle in low for needle in needles):
                    total += 1
                    if len(hits) < MAX_GREP_LINES:
                        hits.append(f"{n}: {line[:500]}")
        head = f"# {what}: {total} of {n} lines match {grep!r}"
        if total > len(hits):
            head += f", the first {len(hits)} shown"
        return head + "\n" + "\n".join(hits) + ("\n" if hits else "")
    kb = _clamp(tail_kb, 1, MAX_LOG_KB)
    head = f"# {what}, {size} bytes" + (f"; the last {kb} KB" if size > kb * 1024 else "")
    return head + "\n" + _tail(path, kb * 1024)


# Write tools ---------------------------------------------------------------------------------------------
@tool(read=False)
def upload_link(ax: Agents, user: dict) -> dict:
    ticket = ax.uploads.new_ticket(user["id"])
    url = ax.url(f"{UPLOAD_PREFIX}{ticket.token}/")
    return {"url": url, "curl": f"curl -sS -T strategy.zip {url}", "expires": _utc(ticket.expires),
            "max_files": TICKET_FILES, "max_mb": ax.uploads.max_mb,
            "how": "zip the strategy folder (cd <folder> && zip -qr ../strategy.zip .) and run the curl line; "
                   "keep the trailing '/', curl appends the file name. One source file such as my_strategy.py "
                   "may be sent as it is. The answer is one JSON line with the upload id: then "
                   "submit_strategy(name, language, upload_id=...). Uploads are kept 60 minutes."}


@tool(read=False)
def submit_strategy(ax: Agents, user: dict, name: str, language: str, upload_id: str | None = None,
                    git_url: str | None = None, git_ref: str | None = None, note: str | None = None,
                    title: str | None = None) -> dict:
    db, cfg = ax.db, ax.cfg
    if bool(upload_id) == bool(git_url):
        raise AgentError("give exactly one source: upload_id (from the upload's JSON line) or git_url")
    data = None
    if upload_id:
        _, path = ax.uploads.get(user["id"], upload_id)
        data = path.read_bytes()
    language = (language or "").strip().lower()
    try:
        vid = submit(cfg, db, name=name, language=language, user=_writer(user), zip_data=data, git_url=git_url,
                     git_ref=git_ref, gpu=language.endswith("-cuda"), note=note, title=title)
    except SubmissionError as exc:
        raise AgentError(str(exc)) from None
    if upload_id:
        ax.uploads.consume(upload_id.strip())
    v = db.version(vid)
    return {"version_id": vid, "strategy": v["strategy"], "number": v["number"], "status": v["status"],
            "pending_ahead": db.versions_ahead(vid), "url": ax.url(f"/strategies/{v['strategy']}"),
            "next": f"version({vid}, wait_s=50), again and again, until the status is ok or failed"}


@tool(read=False)
def play(ax: Agents, user: dict, opponents: list[str], format: str = "Round1", mine: str | None = None,
         seed: str | int | None = None, fill: str = "quickstart") -> dict:
    db, cfg = ax.db, ax.cfg
    seed = seed_arg(seed)
    fmt = (format or "").strip()
    if fmt not in SEATS:
        raise AgentError(f"format is one of {', '.join(SEATS)}")
    own = db.user_strategies(user["id"])
    names = ", ".join(s["name"] for s in own)
    mine = (mine or "").strip().lower()
    if not mine:
        if not own:
            raise AgentError("you have no strategy yet: submit one first (upload_link + submit_strategy)")
        if len(own) > 1:
            raise AgentError(f"you own {len(own)} strategies ({names}); say which one plays: mine=<name>")
        mine = own[0]["name"]
    row = db.strategy(mine.partition("@")[0])
    if row is None or row["owner_id"] != user["id"]:
        raise AgentError(f"mine={mine!r} is not one of your strategies ({names or 'you have none'})")
    seats = [mine] + [(o or "").strip().lower() or "empty" for o in opponents]
    if len(seats) > SEATS[fmt]:
        most = SEATS[fmt] - 1
        raise AgentError(f"{fmt} has {SEATS[fmt]} seats: yours and at most {most} opponent{'' if most == 1 else 's'}")
    fill = (fill or "quickstart").strip().lower()
    if fill not in ("quickstart", "empty"):
        raise AgentError(f"fill is \"quickstart\" or \"empty\", not {fill!r}")
    seats += [fill] * (SEATS[fmt] - len(seats))
    game_id = queue_custom_game(cfg, db, _writer(user), fmt, seats, seed)
    _, players = db.game(game_id)
    return {"game_id": game_id, "url": ax.url(f"/games/{game_id}"), "format": fmt, "rated": False,
            "seats": [{"slot": p["slot"], "label": p["label"] or p["strategy"] or "empty"} for p in players],
            "queued_ahead": db.games_ahead(game_id),
            "next": f"game({game_id}, wait_s=50), again and again, until it is finished"}


@tool(read=False)
def set_strategy(ax: Agents, user: dict, name: str, enabled: bool | None = None, title: str | None = None,
                 activate_version: int | None = None) -> dict:
    db = ax.db
    s = db.strategy((name or "").strip().lower())
    if s is None or s["owner_id"] != user["id"] or s["kind"] != "user":
        raise AgentError(f"{name!r} is not one of your strategies")
    if enabled is None and title is None and activate_version is None:
        raise AgentError("nothing to change: give enabled, title or activate_version")
    changed = []
    if activate_version is not None:
        v = db.version(activate_version)
        if v is None or v["strategy"] != s["name"]:
            raise AgentError(f"version id {activate_version} is not a version of {s['name']}; strategy(name) lists them")
        try:
            db.activate_version(activate_version)
        except ValueError as exc:
            raise AgentError(str(exc)) from None
        changed.append(f"version {v['number']} is now active")
    fields = {}
    if title is not None:
        fields["title"] = title.strip()[:80] or s["name"]
        changed.append(f"title {fields['title']!r}")
    if enabled is not None:
        fields["enabled"] = int(bool(enabled))
        changed.append("enabled: plays in the sandbox" if enabled else "paused: no sandbox games, no custom games")
    if fields:
        db.update_strategy(s["name"], **fields)
    out = _strategy_public(ax, db.strategy(s["name"]), user)
    out["changed"] = changed
    return out
