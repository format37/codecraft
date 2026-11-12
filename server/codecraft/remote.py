"""An executor's side of the hub (hub.py): the database over HTTPS, heartbeats, and the files a game or a build
leaves behind sent up to the hub.

An executor is a machine with Docker that builds strategies and plays games for a hub elsewhere (`hub.url`). It
keeps no database: `RemoteDB` stands in for `db.DB` in the worker and calls the same methods on the hub, which
runs them on its own database (hub.RPC_METHODS lists them). The hub never calls an executor: an executor that is
switched off simply stops asking, and its running work goes back to the queue after `hub.lease_sec`."""
from __future__ import annotations

import logging
import lzma
import time
from pathlib import Path

import httpx

from .config import Config
from .db import LeaseLost

log = logging.getLogger("codecraft.remote")

TIMEOUT = httpx.Timeout(60.0, connect=10.0)
RETRY_STATUS = {429, 502, 503, 504}         # the hub restarting, the proxy without it


class HubError(RuntimeError):
    pass


class Row(dict):
    """A database row as the hub sends it: a dict, read the way sqlite3.Row is (row["name"])."""


def _rows(value):
    if isinstance(value, dict):
        return Row({k: _rows(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_rows(v) for v in value]
    return value


class Hub:
    """HTTP to the hub, with the executor's name and the shared token on every request. Transient failures (the
    hub or its proxy restarting, the line down) are retried for up to `patience` seconds."""

    def __init__(self, cfg: Config, client: httpx.Client | None = None, patience: float = 300.0):
        self.cfg = cfg
        self.name = cfg.executor_name
        token = cfg["hub"].get("token")
        if not token:
            raise HubError("hub.url is set but hub.token (CW_HUB_TOKEN) is not")
        self.http = client or httpx.Client(base_url=str(cfg["hub"]["url"]).rstrip("/"), timeout=TIMEOUT)
        self.headers = {"Authorization": f"Bearer {token}", "X-CW-Executor": self.name}
        self.patience = patience

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        deadline = time.monotonic() + self.patience
        delay = 2.0
        while True:
            try:
                r = self.http.request(method, path, headers=self.headers, **kwargs)
                if r.status_code not in RETRY_STATUS:
                    return r
                why = f"HTTP {r.status_code}"
            except httpx.TransportError as exc:
                why = f"{type(exc).__name__}: {exc}"
            if time.monotonic() + delay > deadline:
                raise HubError(f"{method} {path}: the hub does not answer ({why})")
            log.warning("%s %s: %s; again in %.0f s", method, path, why, delay)
            time.sleep(delay)
            delay = min(delay * 2, 30.0)

    def ok(self, r: httpx.Response) -> httpx.Response:
        if r.status_code >= 400:
            raise HubError(f"{r.request.method} {r.request.url.path}: HTTP {r.status_code}: {r.text[:500]}")
        return r

    # The database ------------------------------------------------------------
    def call(self, method: str, *args, **kwargs):
        r = self.request("POST", "/internal/rpc", json={"method": method, "args": list(args), "kwargs": kwargs})
        if r.status_code == 409:
            raise LeaseLost(r.json().get("detail", "the hub took the work back"))
        body = self.ok(r).json()
        return _rows(body.get("result"))

    # Heartbeats, files -------------------------------------------------------
    def heartbeat(self, info: dict) -> dict:
        return self.ok(self.request("POST", "/internal/heartbeat", json=info)).json()

    def put_game_file(self, game_id: int, name: str, data: bytes) -> None:
        self.ok(self.request("PUT", f"/internal/games/{game_id}/files/{name}", content=data))

    def put_build_log(self, version_id: int, data: bytes) -> None:
        self.ok(self.request("PUT", f"/internal/versions/{version_id}/log", content=data))

    def get_source_zip(self, version_id: int) -> bytes:
        return self.ok(self.request("GET", f"/internal/versions/{version_id}/source.zip")).content


class RemoteDB:
    """db.DB for an executor: each method the worker calls runs on the hub's database."""

    def __init__(self, hub: Hub):
        self.hub = hub

    def __getattr__(self, method: str):
        if method.startswith("_"):
            raise AttributeError(method)

        def call(*args, **kwargs):
            return self.hub.call(method, *args, **kwargs)

        call.__name__ = method
        return call


# Game files ------------------------------------------------------------------
# What the hub gets of a game: everything the site shows. The replay goes compressed (a fifteenth of it) and the
# executor keeps it that way too, once the hub has it.
RESULT_FILES = ("config.json", "results.json", "engine.log")
FINAL_FILES = ("meta.json", "preview.png")


def compress_replay(gdir: Path) -> Path | None:
    """replay.dat -> replay.dat.xz in place (the executor's copy for good); the .xz path, or None without one."""
    raw, xz = gdir / "replay.dat", gdir / "replay.dat.xz"
    if raw.is_file():
        tmp = gdir / ".replay.dat.xz.tmp"
        tmp.write_bytes(lzma.compress(raw.read_bytes(), preset=6))
        tmp.replace(xz)
        raw.unlink()
    return xz if xz.is_file() else None


def upload_results(hub: Hub, game_id: int, gdir: Path) -> None:
    """The files the game's page needs as soon as it is finished: its replay, logs, config and results."""
    for name in RESULT_FILES + tuple(sorted(p.name for p in gdir.glob("player-*.log"))):
        if (gdir / name).is_file():
            hub.put_game_file(game_id, name, (gdir / name).read_bytes())
    xz = compress_replay(gdir)
    if xz is not None:
        hub.put_game_file(game_id, xz.name, xz.read_bytes())


def upload_final(hub: Hub, game_id: int, gdir: Path) -> None:
    for name in FINAL_FILES:
        if (gdir / name).is_file():
            hub.put_game_file(game_id, name, (gdir / name).read_bytes())
