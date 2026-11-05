"""The worker: builds submitted versions, plays queued custom games, fills idle time with rated
sandbox games. One process; one builder thread and `worker.parallel_games` game threads."""
from __future__ import annotations

import json
import logging
import random
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import docker

from .builder import Builder, BuildError
from .config import SEATS, Config
from .db import DB
from .game import GameRunner, PlayerSpec
from .matchmaking import Candidate, choose_format, pick_players
from .strategies import Strategy, strategy_for_version, strategy_from_row

log = logging.getLogger("codewars.worker")


class Worker:
    def __init__(self, cfg: Config, db: DB, client: docker.DockerClient, seeds: dict[str, Strategy],
                 sandbox: bool | None = None, builds: bool | None = None):
        self.cfg = cfg
        self.db = db
        self.client = client
        self.seeds = seeds
        self.runner = GameRunner(cfg, client, db)
        self.builder = Builder(cfg, client)
        self.gpu = cfg.resolve_gpu(client)
        w = cfg["worker"]
        self.sandbox = bool(w.get("sandbox", True)) if sandbox is None else sandbox
        self.builds = bool(w.get("builds", True)) if builds is None else builds
        self.rng = random.Random()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.started = 0
        self.idle = False

    # Players -------------------------------------------------------------
    def spec_for_row(self, row, image: str | None = None, version_id: int | None = None,
                     label: str | None = None) -> PlayerSpec:
        strategy = strategy_from_row(row, self.seeds)
        if version_id:
            v = self.db.version(version_id)
            if v is not None:
                strategy.gpu = bool(v["gpu"])
                strategy.language = v["language"]
                strategy.version_id = v["id"]
                strategy.version_number = v["number"]
                image = image or v["image"]
        return PlayerSpec(strategy, None if strategy.is_builtin else (image or row["image"]), version_id, label)

    def runnable(self) -> list[PlayerSpec]:
        out = []
        for row in self.db.runnable_strategies():
            if row["gpu"] and not self.gpu:
                continue
            out.append(self.spec_for_row(row))
        return out

    def make_match(self) -> tuple[str, list[PlayerSpec]]:
        mm = self.cfg["matchmaking"]
        fmt = choose_format(self.cfg["formats"], self.rng)
        seats = SEATS[fmt]
        rows = {r["name"]: r for r in self.db.strategies()}
        specs = {s.name: s for s in self.runnable()}
        quota = int(self.cfg["worker"].get("sandbox_games_per_build") or 0)
        candidates = []
        for name, spec in specs.items():
            if spec.strategy.is_builtin and not mm.get("include_builtin", True):
                continue
            row = rows.get(name)
            rating = float(row["rating"]) if row and row["rating"] is not None else float(self.cfg["rating"]["initial"])
            games = int(row["games"]) if row else 0
            remaining = None
            if quota:
                if spec.strategy.is_builtin:
                    remaining = 0          # the engine bot never starts a game, it only fills seats
                else:
                    games = self.db.sandbox_games_of_build(name, spec.image)
                    remaining = max(0, quota - games)
            candidates.append(Candidate(name, rating, games, spec.strategy.is_builtin, spec.strategy.gpu, remaining))
        max_gpu = int(self.cfg.get("gpu", "max_per_game", default=1) or 0) if self.gpu else 0
        names = pick_players(candidates, seats, float(mm.get("rating_sigma", 200)), self.rng, max_gpu=max_gpu)
        if not names:
            return fmt, []
        players = [specs[n] for n in names]
        filler = next((s for s in specs.values() if s.strategy.is_builtin), None) \
            if mm.get("fill_with_builtin", True) else None
        while len(players) < seats:
            players.append(filler or PlayerSpec(None))
        self.rng.shuffle(players)
        return fmt, players

    def specs_for_game(self, game_id: int) -> list[PlayerSpec]:
        _, rows = self.db.game(game_id)
        specs = []
        for p in rows:
            if not p["strategy"]:
                specs.append(PlayerSpec(None))
                continue
            srow = self.db.strategy(p["strategy"])
            if srow is None:
                raise ValueError(f"strategy {p['strategy']} no longer exists")
            specs.append(self.spec_for_row(srow, image=p["image"], version_id=p["version_id"], label=p["label"]))
        return specs

    # Loops ---------------------------------------------------------------
    def _play_queued(self) -> bool:
        with self.lock:
            game = self.db.next_queued_game()
            if game is None or not self.db.claim_game(game["id"]):
                return False
        try:
            specs = self.specs_for_game(game["id"])
            seed = int(game["seed"]) if game["seed"] else None
            self.runner.run(game["format"], specs, seed=seed, tag=game["tag"], kind=game["kind"],
                            created_by=game["created_by"], rated=bool(game["rated"]), game_id=game["id"])
        except Exception as exc:  # noqa: BLE001
            log.exception("queued game %d failed", game["id"])
            self.db.mark_error(game["id"], f"{type(exc).__name__}: {exc}")
        return True

    def _play_sandbox(self) -> bool:
        with self.lock:
            try:
                fmt, players = self.make_match()
            except Exception:  # noqa: BLE001
                log.exception("matchmaking failed")
                return False
        if len([p for p in players if p.strategy and not p.strategy.is_builtin]) < 1:
            if not self.idle:
                log.info("sandbox idle: every build has played its %s games; waiting for new versions",
                         self.cfg["worker"].get("sandbox_games_per_build"))
                self.idle = True
            return False
        self.idle = False
        self.runner.run(fmt, players, tag="sandbox", kind="sandbox", rated=True)
        return True

    def _game_loop(self, worker_id: int, max_games: int | None) -> None:
        pause = float(self.cfg["worker"].get("pause_sec") or 0)
        while not self.stop.is_set():
            with self.lock:
                if max_games is not None and self.started >= max_games:
                    return
            played = False
            try:
                played = self._play_queued()
                if not played and self.sandbox:
                    played = self._play_sandbox()
            except Exception:  # noqa: BLE001
                log.exception("worker %d: game failed", worker_id)
                played = True
            if played:
                with self.lock:
                    self.started += 1
                self.stop.wait(pause)
            else:
                self.stop.wait(max(pause, 3))

    def build_one(self) -> bool:
        version = self.db.next_queued_version()
        if version is None:
            return False
        srow = self.db.strategy(version["strategy"])
        self.db.start_version(version["id"])
        log.info("building %s v%d (%s)", version["strategy"], version["number"], version["source_kind"])
        try:
            strategy = strategy_for_version(self.cfg, version, srow)
            image, build_hash = self.builder.build_strategy(strategy)
            self.db.finish_version(version["id"], "ok", image=image, build_hash=build_hash,
                                   log_path=str(self.builder.log_path(strategy).relative_to(self.cfg.data_dir)),
                                   commit_hash=self.builder.last_commit)
            log.info("%s v%d: %s", version["strategy"], version["number"], image)
        except Exception as exc:  # noqa: BLE001
            err = str(exc) if isinstance(exc, BuildError) else f"{type(exc).__name__}: {exc}"
            log.warning("%s v%d: build failed: %s", version["strategy"], version["number"], err[:500])
            log_rel = None
            try:
                log_rel = str(self.builder.log_path(strategy_for_version(self.cfg, version, srow))
                              .relative_to(self.cfg.data_dir))
            except Exception:  # noqa: BLE001
                pass
            self.db.finish_version(version["id"], "failed", error=err[:4000], log_path=log_rel)
        return True

    def _build_loop(self) -> None:
        while not self.stop.is_set():
            try:
                if not self.build_one():
                    self.stop.wait(3)
            except Exception:  # noqa: BLE001
                log.exception("build loop error")
                self.stop.wait(10)

    def run(self, max_games: int | None = None) -> None:
        removed = self.runner.cleanup_leftovers()
        if removed:
            log.info("removed %d leftover container(s)/network(s)", removed)
        stale = self.db.abort_stale_running()
        if stale:
            log.info("marked %d stale running game(s) as error", stale)
        stale = self.db.abort_stale_builds()
        if stale:
            log.info("marked %d interrupted build(s) as failed", stale)
        self.builder.build_engine()
        workers = max(1, int(self.cfg["worker"].get("parallel_games") or 1))
        log.info("worker: %d game thread(s), sandbox=%s, builds=%s, gpu=%s, players: %s", workers, self.sandbox,
                 self.builds, self.gpu, ", ".join(s.name for s in self.runnable()) or "-")

        def handle(signum, _frame):
            log.info("signal %s: finishing running games, no new ones", signum)
            self.stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handle)
            except ValueError:
                pass  # not in the main thread
        with ThreadPoolExecutor(max_workers=workers + 1) as pool:
            futures = [pool.submit(self._game_loop, i, max_games) for i in range(workers)]
            if self.builds:
                futures.append(pool.submit(self._build_loop))
            for f in futures[:workers]:
                f.result()
            self.stop.set()
            for f in futures[workers:]:
                f.result()
        log.info("worker finished after %d game(s)", self.started)


def strategy_limits_json(limits: dict | None) -> str | None:
    return json.dumps(limits) if limits else None
