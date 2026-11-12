"""The worker: builds submitted versions, plays queued custom games, fills idle time with rated
sandbox games. One process; one builder thread and `worker.parallel_games` game threads.

On one machine it works on the database there. As an executor of a hub (`hub.url`, remote.py) the same loops run
on the hub's database over HTTPS: it heartbeats, sends each game's files and each build's log up, rebuilds an image
it does not have (a strategy built on another executor, or a machine that lost its images), and uploads a replay
again when the hub asks for one it no longer keeps."""
from __future__ import annotations

import json
import logging
import random
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import docker

from .announce import Announcer
from . import remote
from .builder import Builder, BuildError, EngineImage
from .config import SEATS, Config
from .db import DB
from .game import GameRunner, PlayerSpec
from .matchmaking import Candidate, choose_format, pick_anchor, pick_players
from .strategies import Strategy, strategy_for_version, strategy_from_row
from .submissions import extract_zip

log = logging.getLogger("codecraft.worker")


class Worker:
    def __init__(self, cfg: Config, db: DB, client: docker.DockerClient, seeds: dict[str, Strategy],
                 sandbox: bool | None = None, builds: bool | None = None, hub: remote.Hub | None = None):
        self.cfg = cfg
        self.db = db                                 # a remote.RemoteDB on an executor
        self.hub = hub                               # an executor's hub, None on one machine
        self.name = hub.name if hub else None        # the executor's name, which the hub's records carry
        self.client = client
        self.seeds = seeds
        self.builder = Builder(cfg, client)
        self.runner = GameRunner(cfg, client, db, EngineImage(self.builder), publish=self._publish if hub else None)
        self.announcer = Announcer(cfg, db)          # the community webhook, when configured
        if hub:
            self.announcer.url = None                # the hub announces: it sees every build and every game
        self.gpu = cfg.resolve_gpu(client)
        w = cfg["worker"]
        # True, False or "gpu": only the work that needs a GPU (an executor kept for it)
        self.sandbox = _mode(w.get("sandbox", True)) if sandbox is None else sandbox
        self.builds = _mode(w.get("builds", True)) if builds is None else builds
        self.building: int | None = None             # the version being built, for the heartbeat
        self.playing: set[int] = set()               # the games being played
        self.image_lock = threading.Lock()           # one rebuild of a missing image at a time
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
        formats = self.cfg["formats"]
        rows = {r["name"]: r for r in self.db.strategies()}
        specs = {s.name: s for s in self.runnable()}
        candidates, states = [], {}
        wanted = [(n, sp.image) for n, sp in specs.items() if not sp.strategy.is_builtin]
        all_states = self.db.sandbox_states(self.cfg["worker"], wanted) if wanted else {}
        for name, spec in specs.items():
            if spec.strategy.is_builtin and not mm.get("include_builtin", True):
                continue
            row = rows.get(name)
            rating = float(row["rating"]) if row and row["rating"] is not None else float(self.cfg["rating"]["initial"])
            if spec.strategy.is_builtin:            # the engine bot never starts a game, it only fills seats
                candidates.append(Candidate(name, rating, int(row["games"]) if row else 0, True, spec.strategy.gpu))
                continue
            st = states[name] = all_states[name]
            candidates.append(Candidate(name, rating, st["games"], False, spec.strategy.gpu, st["placing"], st["overdue"]))
        # an executor kept for the GPU starts only games of GPU strategies; the other seats are anybody's
        anchor = pick_anchor([c for c in candidates if c.gpu] if self.sandbox == "gpu" else candidates, self.rng)
        if anchor is None:
            return choose_format(formats, self.rng), []
        fmt = choose_format(formats, self.rng, played=states[anchor.name]["formats"])
        seats = SEATS[fmt]
        duels = self.db.sandbox_duels(anchor.name, specs[anchor.name].image,
                                      [f for f in formats if SEATS.get(f) == 2]) if seats == 2 else None
        max_gpu = int(self.cfg.get("gpu", "max_per_game", default=1) or 0) if self.gpu else 0
        names = pick_players(candidates, seats, float(mm.get("rating_sigma", 200)), self.rng, max_gpu=max_gpu,
                             duels=duels, anchor=anchor)
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
    # Images ----------------------------------------------------------------
    def ensure_images(self, specs: list[PlayerSpec]) -> None:
        """Every seat's image present here: one built elsewhere (another executor) or lost (a machine set up anew)
        is built again from its version's sources, the commit it was built from, and tagged with the name the
        ladder knows it by."""
        for spec in specs:
            if spec.strategy is None or spec.strategy.is_builtin or not spec.image:
                continue
            with self.image_lock:
                if self.builder._image_exists(spec.image):
                    continue
                strategy = self._strategy_to_rebuild(spec)
                log.info("%s: image %s is not here, building it", spec.name, spec.image)
                image, _ = self.builder.build_strategy(
                    strategy, log_file=self.builder.logs_dir / f"rebuild-{self.builder.log_name(strategy)}.log")
                repo, _, tag = spec.image.rpartition(":")
                self.client.images.get(image).tag(repo, tag)

    def _strategy_to_rebuild(self, spec: PlayerSpec) -> Strategy:
        version_id = spec.version_id or spec.strategy.version_id
        if not version_id:                           # a seed: its manifest, as `cw build` builds it
            seed = self.seeds.get(spec.strategy.name)
            if seed is None:
                raise BuildError(f"{spec.name}: image {spec.image} is missing and there is no manifest to build it from")
            return seed
        version = self.db.version(version_id)
        if version is None:
            raise BuildError(f"{spec.name}: image {spec.image} is missing and version {version_id} is gone")
        strategy = strategy_for_version(self.cfg, version, self.db.strategy(version["strategy"]))
        if strategy.source.git and version["commit_hash"]:
            strategy.source.ref = version["commit_hash"]       # the very commit, not where the branch is now
        self._fetch_source(version)
        return strategy

    def _fetch_source(self, version) -> None:
        """An executor's copy of an uploaded version's sources (data/submissions/<name>/v<n>/src), from the hub."""
        if version["source_kind"] != "zip" or self.hub is None:
            return
        dest = self.cfg.data_dir / version["source"]
        if dest.is_dir():
            return
        data = self.hub.get_source_zip(version["id"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        (dest.parent / "source.zip").write_bytes(data)
        sub = self.cfg["submissions"]
        extract_zip(data, dest, int(sub.get("max_files") or 2000), int(sub.get("max_unpacked_mb") or 200))

    # Hub -------------------------------------------------------------------
    def _publish(self, stage: str, game_id: int, gdir: Path) -> None:
        if stage == "results":
            remote.upload_results(self.hub, game_id, gdir)
        else:
            remote.upload_final(self.hub, game_id, gdir)

    def heartbeat(self) -> dict:
        info = {"gpu": self.gpu, "engine": self.runner.engine.image, "parallel_games": self.parallel,
                "playing": sorted(self.playing), "building": self.building, "sandbox": self.sandbox,
                "builds": self.builds, "stopping": self.stop.is_set()}
        answer = self.hub.heartbeat(info)
        for game_id in answer.get("restore") or []:
            self.restore(int(game_id))
        return answer

    def restore(self, game_id: int) -> None:
        """Send a replay the hub has let go (hub.replay_days) up again; the game's other files are there still."""
        gdir = self.cfg.games_dir / f"{game_id:06d}"
        xz = remote.compress_replay(gdir)
        if xz is not None:
            self.hub.put_game_file(game_id, xz.name, xz.read_bytes())
            log.info("game %d: replay sent to the hub again", game_id)
        else:
            log.warning("game %d: the hub asked for its replay, there is none here", game_id)
        self.db.restored(game_id)

    def _heartbeat_loop(self) -> None:
        every = float(self.cfg["executor"].get("heartbeat_sec") or 15)
        while True:
            try:
                self.heartbeat()
            except Exception:  # noqa: BLE001 - the hub is away; the loops wait for it in their own calls
                log.warning("heartbeat failed", exc_info=True)
            if self.done.wait(every):
                return

    # Loops ---------------------------------------------------------------
    def _play_queued(self) -> bool:
        with self.lock:
            game = self.db.next_queued_game(gpu=self.gpu, gpu_only=self.sandbox == "gpu")
            if game is None or not self.db.claim_game(game["id"], self.name):
                return False
        self.playing.add(game["id"])
        try:
            specs = self.specs_for_game(game["id"])
            self.ensure_images(specs)
            seed = int(game["seed"]) if game["seed"] else None
            self.runner.run(game["format"], specs, seed=seed, tag=game["tag"], kind=game["kind"],
                            created_by=game["created_by"], rated=bool(game["rated"]), game_id=game["id"])
        except Exception as exc:  # noqa: BLE001
            log.exception("queued game %d failed", game["id"])
            self.db.mark_error(game["id"], f"{type(exc).__name__}: {exc}", self.name)
        finally:
            self.playing.discard(game["id"])
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
                log.info("sandbox idle: every build has played its %s placement games and no strategy is due a "
                         "game (one every %s h and up); waiting", self.cfg["worker"].get("sandbox_games_per_build"),
                         self.cfg["worker"].get("sandbox_interval_hours"))
                self.idle = True
            return False
        self.idle = False
        self.ensure_images(players)
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
            if self.hub is None:                                  # the hub does both for its executors
                try:
                    self.db.update_leagues(self.cfg["leagues"])  # Monday's lines, also on a ladder that stands still
                except Exception:  # noqa: BLE001
                    log.exception("leagues not updated")
                try:
                    self.announcer.tick(engine=self.runner.engine.image)
                except Exception:  # noqa: BLE001
                    log.exception("announcement failed")
            if played:
                with self.lock:
                    self.started += 1
                self.stop.wait(pause)
            else:   # nothing to do: an executor asks its hub less often (each look is a few calls over the net)
                self.stop.wait(max(pause, 10 if self.hub else 3))

    def build_one(self) -> bool:
        version = self.db.next_queued_version(gpu=self.gpu, gpu_only=self.builds == "gpu")
        if version is None:
            return False
        srow = self.db.strategy(version["strategy"])
        if not self.db.start_version(version["id"], self.name):
            return True                                         # another executor took it
        log.info("building %s v%d (%s)", version["strategy"], version["number"], version["source_kind"])
        self.building = version["id"]
        log_path = self.builder.logs_dir / f"{version['strategy']}-v{version['number']}.log"   # Builder.log_path
        shipping = threading.Event()
        shipper = None
        if self.hub is not None:                     # the log goes up while it grows: version() shows its tail
            shipper = threading.Thread(target=self._ship_log, args=(version["id"], log_path, shipping), daemon=True)
            shipper.start()
        try:
            strategy = strategy_for_version(self.cfg, version, srow)
            self._fetch_source(version)
            image, build_hash = self.builder.build_strategy(strategy)
            result = dict(status="ok", image=image, build_hash=build_hash, commit_hash=self.builder.last_commit)
            log.info("%s v%d: %s", version["strategy"], version["number"], image)
        except Exception as exc:  # noqa: BLE001
            err = str(exc) if isinstance(exc, BuildError) else f"{type(exc).__name__}: {exc}"
            log.warning("%s v%d: build failed: %s", version["strategy"], version["number"], err[:500])
            result = dict(status="failed", error=err[:4000])
        finally:
            self.building = None
            shipping.set()
            if shipper is not None:
                shipper.join()
        log_rel = str(log_path.relative_to(self.cfg.data_dir)) if log_path.is_file() else None
        status = result.pop("status")
        if self.db.finish_version(version["id"], status, log_path=log_rel, **result, **(
                {"executor": self.name} if self.name else {})) is False:
            log.warning("%s v%d: built, but the hub gave the build to another executor", version["strategy"],
                        version["number"])
        elif status == "ok":
            self.announcer.built(self.db.version(version["id"]), srow)
        return True

    def _ship_log(self, version_id: int, path: Path, done: threading.Event) -> None:
        sent = None
        while True:
            last = done.wait(5)
            try:
                stat = path.stat()
                if (stat.st_size, stat.st_mtime) != sent:
                    self.hub.put_build_log(version_id, path.read_bytes())
                    sent = (stat.st_size, stat.st_mtime)
            except FileNotFoundError:
                pass
            except Exception:  # noqa: BLE001
                log.warning("build log of version %d not sent", version_id, exc_info=True)
            if last:
                return

    def _build_loop(self) -> None:
        while not self.stop.is_set():
            try:
                if not self.build_one():
                    self.stop.wait(3)
            except Exception:  # noqa: BLE001
                log.exception("build loop error")
                self.stop.wait(10)

    @property
    def parallel(self) -> int:
        return max(1, int(self.cfg["worker"].get("parallel_games") or 1))

    def run(self, max_games: int | None = None) -> None:
        removed = self.runner.cleanup_leftovers()
        if removed:
            log.info("removed %d leftover container(s)/network(s)", removed)
        # what this machine was doing when it stopped (a restart, the power): the queue's again
        stale = self.db.abort_stale_running(self.name)
        if stale:
            log.info("%d game(s) interrupted by the stop: queued again (custom) or ended (sandbox)", stale)
        stale = self.db.abort_stale_builds(self.name)
        if stale:
            log.info("%d interrupted build(s) queued again", stale)
        engine = self.runner.engine.current()                    # and again before every game
        log.info("engine: %s", engine)
        if self.hub is None:
            self.announcer.tick(engine=engine)
        workers = self.parallel
        self.done = threading.Event()
        beat = None
        if self.hub is not None:
            log.info("executor %s of %s", self.name, self.cfg["hub"]["url"])
            beat = threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True)
            beat.start()
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
        self.done.set()
        if beat is not None:
            beat.join(timeout=5)
            try:
                self.heartbeat()                     # the last word: stopped, nothing running
            except Exception:  # noqa: BLE001
                pass
        log.info("worker finished after %d game(s)", self.started)


def _mode(value) -> bool | str:
    """worker.sandbox / worker.builds: true, false or "gpu"."""
    if isinstance(value, str) and value.strip().lower() == "gpu":
        return "gpu"
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def strategy_limits_json(limits: dict | None) -> str | None:
    return json.dumps(limits) if limits else None
