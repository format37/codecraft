"""The sandbox loop: pick a format and players, play, rate, repeat."""
from __future__ import annotations

import logging
import random
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .config import SEATS, Config
from .db import DB
from .game import GameRunner
from .matchmaking import Candidate, choose_format, pick_players
from .strategies import Strategy

log = logging.getLogger("codewars.loop")


class Sandbox:
    def __init__(self, cfg: Config, db: DB, runner: GameRunner, strategies: dict[str, Strategy], images: dict[str, str]):
        self.cfg = cfg
        self.db = db
        self.runner = runner
        self.strategies = strategies
        self.images = images
        self.rng = random.Random()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.started = 0

    def runnable(self) -> list[Strategy]:
        out = []
        for s in self.strategies.values():
            if not s.enabled:
                continue
            if s.is_builtin or self.images.get(s.name):
                out.append(s)
        return out

    def make_match(self) -> tuple[str, list[Strategy | None]]:
        mm = self.cfg["matchmaking"]
        fmt = choose_format(self.cfg["formats"], self.rng)
        seats = SEATS[fmt]
        rows = {r["name"]: r for r in self.db.strategies()}
        candidates = []
        for s in self.runnable():
            if s.is_builtin and not mm.get("include_builtin", True):
                continue
            row = rows.get(s.name)
            rating = float(row["rating"]) if row and row["rating"] is not None else float(self.cfg["rating"]["initial"])
            games = int(row["games"]) if row else 0
            candidates.append(Candidate(s.name, rating, games, s.is_builtin))
        names = pick_players(candidates, seats, float(mm.get("rating_sigma", 200)), self.rng)
        players: list[Strategy | None] = [self.strategies[n] for n in names]
        filler = next((s for s in self.runnable() if s.is_builtin), None) if mm.get("fill_with_builtin", True) else None
        while len(players) < seats:
            players.append(filler)
        self.rng.shuffle(players)
        return fmt, players

    def _worker(self, worker_id: int, max_games: int | None) -> None:
        pause = float(self.cfg["loop"].get("pause_sec") or 0)
        while not self.stop.is_set():
            with self.lock:
                if max_games is not None and self.started >= max_games:
                    return
                self.started += 1
                try:
                    fmt, players = self.make_match()
                except Exception:
                    log.exception("worker %d: matchmaking failed", worker_id)
                    self.stop.set()
                    return
            try:
                self.runner.run(fmt, players, self.images, tag="sandbox")
            except Exception:
                log.exception("worker %d: game failed", worker_id)
            if pause:
                self.stop.wait(pause)

    def run(self, max_games: int | None = None) -> None:
        if len([s for s in self.runnable() if not s.is_builtin]) < 1:
            raise RuntimeError("no built strategies to play; run `build` first")
        workers = max(1, int(self.cfg["loop"].get("parallel_games") or 1))
        log.info("sandbox loop: %d worker(s), players: %s", workers, ", ".join(s.name for s in self.runnable()))

        def handle(signum, _frame):
            log.info("signal %s: finishing running games, no new ones", signum)
            self.stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handle)
            except ValueError:
                pass  # not in the main thread
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(self._worker, i, max_games) for i in range(workers)]
            for f in futures:
                f.result()
        log.info("sandbox loop finished after %d game(s)", self.started)
