"""Run one game: an isolated docker network, the engine container and one container per strategy."""
from __future__ import annotations

import io
import json
import logging
import re
import secrets
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import docker
from docker.types import DeviceRequest, Ulimit

from .config import MAX_TICKS, SEATS, Config
from .db import DB
from .strategies import Strategy

log = logging.getLogger("codewars.game")

# Wrapper around the client's run.sh: records the exit code and the CPU time of the strategy
# (user+sys of all children) so the orchestrator can report it.
WRAPPER = (
    "TIMEFORMAT='[cw] time real=%R user=%U sys=%S'; time bash /cw-run.sh \"$@\"; rc=$?; "
    'echo "[cw] exit=$rc" >&2; exit $rc'
)
CPU_RE = re.compile(r"\[cw\] time real=([\d.]+) user=([\d.]+) sys=([\d.]+)")


@dataclass
class Seat:
    slot: int
    strategy: Strategy | None          # None -> Empty seat
    image: str | None = None
    port: int = 0
    token: str = ""
    container: object = None
    score: int = 0
    crashed: bool = False
    comment: str | None = None
    cpu_sec: float | None = None
    exit_code: int | None = None
    oom_killed: bool = False

    def diagnosis(self, limits: dict) -> str | None:
        """Human-readable reason for a crash, from the container state (None if nothing is known)."""
        if self.oom_killed:
            return f"out of memory (container limit {limits.get('memory_mb')} MB)"
        if self.exit_code == 152:   # 128 + SIGXCPU
            return f"CPU time limit exceeded ({limits.get('cpu_time_sec')} s)"
        if self.exit_code == 137:
            return "killed (SIGKILL)"
        if self.exit_code not in (None, 0):
            return f"exited with code {self.exit_code}"
        return None

    limits: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.strategy.name if self.strategy else "empty"

    @property
    def is_tcp(self) -> bool:
        return self.strategy is not None and not self.strategy.is_builtin


@dataclass
class GameOutcome:
    game_id: int
    format: str
    seed: int | None
    status: str                         # finished | aborted | error
    error: str | None
    duration_sec: float
    dir: Path
    seats: list[Seat] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)


def _tar_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _read_file_from_container(container, path: str) -> bytes | None:
    try:
        stream, _ = container.get_archive(path)
    except docker.errors.NotFound:
        return None
    buf = io.BytesIO()
    for chunk in stream:
        buf.write(chunk)
    buf.seek(0)
    with tarfile.open(fileobj=buf) as tar:
        for member in tar.getmembers():
            if member.isfile():
                return tar.extractfile(member).read()
    return None


class GameRunner:
    def __init__(self, cfg: Config, client: docker.DockerClient, db: DB):
        self.cfg = cfg
        self.client = client
        self.db = db
        self.prefix = cfg["docker"]["prefix"]
        self.label = cfg["docker"]["label"]

    # ------------------------------------------------------------------
    def run(self, fmt: str, players: list[Strategy | None], images: dict[str, str],
            seed: int | None = None, tag: str | None = None) -> GameOutcome:
        if fmt not in SEATS:
            raise ValueError(f"unknown format {fmt!r}; expected one of {list(SEATS)}")
        if len(players) != SEATS[fmt]:
            raise ValueError(f"{fmt} needs exactly {SEATS[fmt]} players, got {len(players)}")
        limits = self.cfg["limits"]
        seats = []
        for slot, strategy in enumerate(players):
            seat = Seat(slot=slot, strategy=strategy)
            seat.limits = {**limits, **(strategy.limits if strategy else {})}
            if seat.is_tcp:
                seat.image = images.get(strategy.name)
                if not seat.image:
                    raise ValueError(f"strategy {strategy.name} has no built image; run `build` first")
                seat.port = 31001 + slot
                seat.token = secrets.token_hex(8)
            seats.append(seat)

        game_id = self.db.create_game(fmt, seed, tag)
        gdir = self.cfg.games_dir / f"{game_id:06d}"
        gdir.mkdir(parents=True, exist_ok=True)
        engine_config = self._engine_config(fmt, seats, seed)
        (gdir / "config.json").write_text(json.dumps(engine_config, indent=2))
        names = [s.name for s in seats]
        log.info("game %d: %s %s", game_id, fmt, " vs ".join(names))

        started = time.time()
        status, error = "error", None
        network = engine = None
        name = f"{self.prefix}-{game_id}"
        try:
            network = self.client.networks.create(name, driver="bridge", internal=True,
                                                  labels={self.label: str(game_id)})
            engine = self._start_engine(name, network, engine_config, names, gdir)
            if any(s.is_tcp for s in seats):
                self._wait_for_ports(engine, [s.port for s in seats if s.is_tcp])
                for seat in seats:
                    if seat.is_tcp:
                        seat.container = self._start_player(name, network, seat, engine.name)
            slowest = max([s.limits for s in seats], key=lambda l: (l.get("tick_timeout_sec") or 1.0))
            wall = limits.get("game_wall_timeout_sec") or (
                MAX_TICKS * (slowest.get("tick_timeout_sec") or 1.0) + (slowest.get("accept_timeout_sec") or 60) + 120)
            try:
                result = engine.wait(timeout=int(wall))
                engine_rc = result.get("StatusCode", -1)
            except Exception:  # read timeout from the docker API -> the game ran too long
                engine.reload()
                if engine.status == "running":
                    log.warning("game %d: wall timeout %.0fs reached, killing the engine", game_id, wall)
                    engine.kill()
                    engine_rc = -1
                    error = f"wall-clock timeout ({wall:.0f}s)"
                else:
                    engine_rc = engine.wait(timeout=10).get("StatusCode", -1)
            self._collect(engine, seats, gdir)
            results = self._parse_results(gdir, seats)
            if results and engine_rc == 0 and error is None:
                status = "finished"
            else:
                status = "aborted"
                error = error or f"engine exited with code {engine_rc}, results {'present' if results else 'missing'}"
        except Exception as exc:  # noqa: BLE001 - any failure is recorded on the game
            log.exception("game %d failed", game_id)
            status, error = "error", f"{type(exc).__name__}: {exc}"
        finally:
            self._cleanup(engine, seats, network)
        duration = time.time() - started

        rows = [dict(slot=s.slot, strategy=s.strategy.name if s.strategy else None, image=s.image, score=s.score,
                     crashed=s.crashed, comment=s.comment, cpu_sec=s.cpu_sec) for s in seats]
        rows = self._record(game_id, fmt, status, error, duration, self._seed_from_results(gdir, seed), rows)
        outcome = GameOutcome(game_id, fmt, seed, status, error, duration, gdir, seats, rows)
        (gdir / "meta.json").write_text(json.dumps({
            "id": game_id, "format": fmt, "status": status, "error": error, "duration_sec": round(duration, 1),
            "players": rows,
        }, indent=2, default=str))
        log.info("game %d: %s in %.0fs: %s", game_id, status, duration,
                 ", ".join(f"{r['strategy'] or 'empty'}={r['score']}(#{r['place']})" for r in rows))
        return outcome

    def _record(self, game_id: int, fmt: str, status: str, error: str | None, duration: float,
                seed: int | None, rows: list[dict]) -> list[dict]:
        last_exc = None
        for attempt in range(5):
            try:
                return self.db.finish_game(game_id, status, error, duration, seed, rows,
                                           self.cfg["rating"], self.cfg["points"][fmt])
            except Exception as exc:  # noqa: BLE001 - e.g. a transient "database is locked"
                last_exc = exc
                log.warning("game %d: recording results failed (attempt %d): %s", game_id, attempt + 1, exc)
                time.sleep(1 + attempt)
        log.error("game %d: giving up on recording results: %s", game_id, last_exc)
        try:
            self.db.mark_error(game_id, f"result not recorded: {last_exc}")
        except Exception:  # noqa: BLE001
            log.exception("game %d: could not even mark the game as failed", game_id)
        return [dict(r, place=None, points=None, rating_before=None, rating_after=None) for r in rows]

    # ------------------------------------------------------------------
    def _engine_config(self, fmt: str, seats: list[Seat], seed: int | None) -> dict:
        players = []
        for seat in seats:
            if seat.strategy is None:
                players.append({"Empty": None})
            elif seat.strategy.is_builtin:
                players.append(seat.strategy.builtin)
            else:
                players.append({"Tcp": {
                    "host": "0.0.0.0",
                    "port": seat.port,
                    "accept_timeout": seat.limits.get("accept_timeout_sec"),
                    "timeout": seat.limits.get("tick_timeout_sec"),
                    "token": seat.token,
                }})
        return {"seed": seed, "game": {"Create": fmt}, "players": players}

    def _start_engine(self, name: str, network, config: dict, names: list[str], gdir: Path):
        eng = self.cfg["engine"]
        command = ["--config", "/game/config.json", "--batch-mode", "--save-results", "/game/results.json",
                   "--save-replay", "/game/replay.dat", "--log-level", "info", "--player-names", *names]
        engine = self.client.containers.create(
            eng["image"], command=command, name=f"{name}-engine", network=network.name, working_dir="/game",
            mem_limit=f"{eng['memory_mb']}m", nano_cpus=int(float(eng["cpus"]) * 1e9),
            labels={self.label: name},
        )
        engine.put_archive("/game", _tar_bytes({"config.json": json.dumps(config).encode()}))
        engine.start()
        return engine

    def _wait_for_ports(self, engine, ports: list[int]) -> None:
        deadline = time.time() + float(self.cfg["engine"].get("startup_timeout_sec", 30))
        pending = set(ports)
        while pending:
            text = engine.logs().decode(errors="replace")
            for port in list(pending):
                if f"Waiting for connection on port {port}" in text:
                    pending.discard(port)
            if not pending:
                return
            engine.reload()
            if engine.status != "running":
                raise RuntimeError(f"engine exited before listening: {text[-800:]}")
            if time.time() > deadline:
                raise RuntimeError(f"engine did not open ports {sorted(pending)} in time: {text[-800:]}")
            time.sleep(0.2)

    def _start_player(self, name: str, network, seat: Seat, engine_host: str):
        limits = seat.limits
        strategy = seat.strategy
        cpus = float(limits.get("cpus") or 1.0)
        threads = str(max(1, int(round(cpus))))
        env = {"OMP_NUM_THREADS": threads, "MKL_NUM_THREADS": threads, "OPENBLAS_NUM_THREADS": threads,
               "NUMBA_NUM_THREADS": threads, "CW_GAME": name, "CW_SLOT": str(seat.slot), **strategy.env}
        kwargs: dict = dict(
            name=f"{name}-p{seat.slot}", network=network.name,
            entrypoint=["bash", "-c", WRAPPER, "cw"],
            command=[engine_host, str(seat.port), seat.token],
            environment=env, labels={self.label: name},
            nano_cpus=int(cpus * 1e9), pids_limit=int(limits.get("pids") or 256),
        )
        mem = limits.get("memory_mb")
        if mem:
            kwargs["mem_limit"] = f"{int(mem)}m"
            kwargs["memswap_limit"] = f"{int(mem)}m"
        cpu_time = limits.get("cpu_time_sec")
        if cpu_time:
            kwargs["ulimits"] = [Ulimit(name="cpu", soft=int(cpu_time), hard=int(cpu_time) + 5)]
        if strategy.gpu:
            kwargs["device_requests"] = [DeviceRequest(count=-1, capabilities=[["gpu"]])]
        container = self.client.containers.create(seat.image, **kwargs)
        container.start()
        return container

    def _collect(self, engine, seats: list[Seat], gdir: Path) -> None:
        for fname in ("results.json", "replay.dat"):
            data = _read_file_from_container(engine, f"/game/{fname}")
            if data is not None:
                (gdir / fname).write_bytes(data)
        (gdir / "engine.log").write_bytes(engine.logs())
        for seat in seats:
            if seat.container is None:
                continue
            try:
                seat.container.reload()
                if seat.container.status == "running":
                    seat.container.stop(timeout=5)
                seat.container.reload()
                state = seat.container.attrs.get("State", {})
                seat.exit_code = state.get("ExitCode")
                seat.oom_killed = bool(state.get("OOMKilled"))
                logs = seat.container.logs()
            except docker.errors.APIError as exc:
                logs = f"[cw] could not fetch logs: {exc}".encode()
            (gdir / f"player-{seat.slot}-{seat.name}.log").write_bytes(logs)
            m = CPU_RE.search(logs.decode(errors="replace"))
            if m:
                seat.cpu_sec = round(float(m[2]) + float(m[3]), 2)

    def _parse_results(self, gdir: Path, seats: list[Seat]) -> dict | None:
        path = gdir / "results.json"
        if not path.exists():
            return None
        results = json.loads(path.read_text())
        for seat, info, score in zip(seats, results.get("players", []), results.get("results", [])):
            seat.score = int(score)
            seat.crashed = bool(info.get("crashed"))
            seat.comment = info.get("comment")
            why = seat.diagnosis(seat.limits)
            if why and seat.crashed:
                seat.comment = f"{why}; {seat.comment}" if seat.comment else why
        return results

    @staticmethod
    def _seed_from_results(gdir: Path, fallback: int | None) -> int | None:
        path = gdir / "results.json"
        if path.exists():
            try:
                return json.loads(path.read_text()).get("seed", fallback)
            except ValueError:
                pass
        return fallback

    def _cleanup(self, engine, seats: list[Seat], network) -> None:
        for container in [s.container for s in seats] + [engine]:
            if container is None:
                continue
            try:
                container.remove(force=True)
            except docker.errors.APIError as exc:
                log.warning("remove %s: %s", container.name, exc)
        if network is not None:
            for _ in range(5):
                try:
                    network.remove()
                    break
                except docker.errors.APIError:
                    time.sleep(1)

    # ------------------------------------------------------------------
    def cleanup_leftovers(self) -> int:
        """Remove containers/networks of games left behind by a crashed orchestrator."""
        n = 0
        for c in self.client.containers.list(all=True, filters={"label": self.label}):
            c.remove(force=True)
            n += 1
        for net in self.client.networks.list(filters={"label": self.label}):
            try:
                net.remove()
                n += 1
            except docker.errors.APIError:
                pass
        return n
