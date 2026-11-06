"""Run one game: the engine container and one container per strategy, each strategy on a network of its own.

Every TCP seat gets an internal bridge network shared only with the engine (no route out, no host address on it),
and the engine listens for that seat on its address in that network only: a strategy reaches its own seat's port
and nothing else, not the other seats, not the node, not the internet."""
from __future__ import annotations

import copy
import io
import ipaddress
import json
import logging
import re
import secrets
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import docker
from docker.types import DeviceRequest, IPAMConfig, IPAMPool, LogConfig, Ulimit

from .config import MAX_TICKS, SEATS, Config
from .db import DB
from .strategies import Strategy

log = logging.getLogger("codecraft.game")

# Wrapper around the client's run.sh: records the exit code and the CPU time of the strategy
# (user+sys of all children) so the orchestrator can report it.
WRAPPER = (
    "TIMEFORMAT='[cw] time real=%R user=%U sys=%S'; time bash /cw-run.sh \"$@\"; rc=$?; "
    'echo "[cw] exit=$rc" >&2; exit $rc'
)
CPU_RE = re.compile(r"\[cw\] time real=([\d.]+) user=([\d.]+) sys=([\d.]+)")
# The host gets no address on a game network, so nothing the node serves on 0.0.0.0 answers a player.
INHIBIT_IPV4 = "com.docker.network.bridge.inhibit_ipv4"
# What docker keeps of a container's output (players and the engine): the last 10 MB. The [cw] lines are
# printed last, so they survive the rotation.
LOG_MAX = "10m"
PLAYER_TMP_MB = 256          # the writable /tmp of a read-only player container (counted in its memory)
SUBNET_RETRIES = 4


def seat_subnet(base: str, game_id: int, slot: int, attempt: int = 0) -> ipaddress.IPv4Network:
    """The /24 of a seat's network within `docker.seat_subnets`: number (game_id * 4 + slot) of its /24s,
    64 further on for each retry after an overlap (a leftover network, or a parallel game 64 ids apart)."""
    subnets = list(ipaddress.ip_network(base).subnets(new_prefix=24))
    return subnets[(game_id * 4 + slot + 64 * attempt) % len(subnets)]


def redacted(config: dict) -> dict:
    """The engine config without the seat tokens, for the game's public file list."""
    shown = copy.deepcopy(config)
    for player in shown.get("players", []):
        if isinstance(player, dict) and isinstance(player.get("Tcp"), dict):
            player["Tcp"]["token"] = "***"
    return shown


def _log_config() -> LogConfig:
    return LogConfig(type="json-file", config={"max-size": LOG_MAX, "max-file": "1"})


@dataclass
class PlayerSpec:
    """Who sits in a seat: a strategy with the image to run (None strategy = Empty seat)."""
    strategy: Strategy | None
    image: str | None = None
    version_id: int | None = None
    label: str | None = None

    @property
    def name(self) -> str:
        if self.strategy is None:
            return "empty"
        return self.label or self.strategy.name


def specs_from(players: list[Strategy | None], images: dict[str, str]) -> list[PlayerSpec]:
    return [PlayerSpec(p, images.get(p.name) if p else None, p.version_id if p else None) for p in players]


@dataclass
class Seat:
    slot: int
    spec: PlayerSpec
    port: int = 0
    token: str = ""
    container: object = None
    score: int = 0
    crashed: bool = False
    comment: str | None = None
    cpu_sec: float | None = None
    exit_code: int | None = None
    oom_killed: bool = False
    limits: dict = field(default_factory=dict)
    network: object = None              # the seat's own network (TCP seats)
    engine_ip: str | None = None        # the engine's address on it: the engine listens there for this seat

    @property
    def strategy(self) -> Strategy | None:
        return self.spec.strategy

    @property
    def image(self) -> str | None:
        return self.spec.image

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def is_tcp(self) -> bool:
        return self.strategy is not None and not self.strategy.is_builtin

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

    def row(self) -> dict:
        return dict(slot=self.slot, strategy=self.strategy.name if self.strategy else None, image=self.image,
                    score=self.score, crashed=self.crashed, comment=self.comment, cpu_sec=self.cpu_sec,
                    version_id=self.spec.version_id, label=self.spec.label)


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


def gpu_available(cfg: Config, client: docker.DockerClient | None) -> bool:
    return cfg.resolve_gpu(client)


class GameRunner:
    def __init__(self, cfg: Config, client: docker.DockerClient, db: DB):
        self.cfg = cfg
        self.client = client
        self.db = db
        self.prefix = cfg["docker"]["prefix"]
        self.label = cfg["docker"]["label"]

    # ------------------------------------------------------------------
    def run(self, fmt: str, players: list[PlayerSpec], seed: int | None = None, tag: str | None = None,
            kind: str = "sandbox", created_by: int | None = None, rated: bool = True,
            game_id: int | None = None) -> GameOutcome:
        if fmt not in SEATS:
            raise ValueError(f"unknown format {fmt!r}; expected one of {list(SEATS)}")
        if len(players) != SEATS[fmt]:
            raise ValueError(f"{fmt} needs exactly {SEATS[fmt]} players, got {len(players)}")
        limits = self.cfg["limits"]
        seats = []
        for slot, spec in enumerate(players):
            seat = Seat(slot=slot, spec=spec)
            seat.limits = {**limits, **(spec.strategy.limits if spec.strategy else {})}
            if seat.is_tcp:
                if not spec.image:
                    raise ValueError(f"strategy {spec.name} has no built image")
                seat.port = 31001 + slot
                seat.token = secrets.token_hex(8)
            seats.append(seat)

        if game_id is None:
            game_id = self.db.create_game(fmt, seed, tag, kind=kind, created_by=created_by, rated=rated)
        gdir = self.cfg.games_dir / f"{game_id:06d}"
        gdir.mkdir(parents=True, exist_ok=True)
        names = [re.sub(r"[^\w.@-]", "_", s.name)[:40] for s in seats]
        log.info("game %d: %s %s %s", game_id, kind, fmt, " vs ".join(names))

        started = time.time()
        status, error = "error", None
        engine = None
        networks: list = []
        name = f"{self.prefix}-{game_id}"
        try:
            for seat in seats:
                if seat.is_tcp:
                    seat.network, seat.engine_ip = self._seat_network(name, game_id, seat.slot)
                    networks.append(seat.network)
            engine_config = self._engine_config(fmt, seats, seed)
            (gdir / "config.json").write_text(json.dumps(redacted(engine_config), indent=2))
            engine = self._start_engine(name, seats, engine_config, names)
            if any(s.is_tcp for s in seats):
                self._wait_for_ports(engine, [s.port for s in seats if s.is_tcp])
                for seat in seats:
                    if seat.is_tcp:
                        seat.container = self._start_player(name, seat)
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
            self._cleanup(engine, seats, networks)
        duration = time.time() - started

        rows = self._record(game_id, fmt, status, error, duration, self._seed_from_results(gdir, seed),
                            [s.row() for s in seats], rated)
        outcome = GameOutcome(game_id, fmt, seed, status, error, duration, gdir, seats, rows)
        (gdir / "meta.json").write_text(json.dumps({
            "id": game_id, "format": fmt, "kind": kind, "rated": rated, "status": status, "error": error,
            "duration_sec": round(duration, 1), "players": rows,
        }, indent=2, default=str))
        log.info("game %d: %s in %.0fs: %s", game_id, status, duration,
                 ", ".join(f"{r['label'] or r['strategy'] or 'empty'}={r['score']}(#{r['place']})" for r in rows))
        if status == "finished" and (gdir / "replay.dat").exists():
            try:   # the link-preview image, so a shared link shows the board at once
                from . import preview
                preview.ensure(gdir, [r["label"] or r["strategy"] or "empty" for r in rows], game_id, fmt,
                               footer=preview.site_host(self.cfg["auth"].get("base_url")))
            except Exception as exc:  # noqa: BLE001
                log.warning("game %d: preview image not rendered: %s", game_id, exc)
        return outcome

    def _record(self, game_id: int, fmt: str, status: str, error: str | None, duration: float,
                seed: int | None, rows: list[dict], rated: bool) -> list[dict]:
        last_exc = None
        for attempt in range(5):
            try:
                return self.db.finish_game(game_id, status, error, duration, seed, rows,
                                           self.cfg["rating"], self.cfg["points"][fmt], rated=rated)
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
                    "host": seat.engine_ip,             # the listen address: this seat's network only
                    "port": seat.port,
                    "accept_timeout": seat.limits.get("accept_timeout_sec"),
                    "timeout": seat.limits.get("tick_timeout_sec"),
                    "token": seat.token,
                }})
        return {"seed": seed, "game": {"Create": fmt}, "players": players}

    def _seat_network(self, name: str, game_id: int, slot: int):
        """An internal network for one seat with a fixed subnet, so the engine's address on it is known before
        the engine starts. -> (network, the engine's address)"""
        base = str(self.cfg.get("docker", "seat_subnets", default=None) or "10.200.0.0/16")
        for attempt in range(SUBNET_RETRIES + 1):
            subnet = seat_subnet(base, game_id, slot, attempt)
            try:
                network = self.client.networks.create(
                    f"{name}-s{slot}", driver="bridge", internal=True, options={INHIBIT_IPV4: "true"},
                    ipam=IPAMConfig(pool_configs=[IPAMPool(subnet=str(subnet))]), labels={self.label: str(game_id)})
                return network, str(subnet.network_address + 2)
            except docker.errors.APIError as exc:
                if "overlap" not in str(exc).lower() or attempt == SUBNET_RETRIES:
                    raise
                log.warning("game %d: seat %d subnet %s is taken, trying another", game_id, slot, subnet)

    def _start_engine(self, name: str, seats: list[Seat], config: dict, names: list[str]):
        eng = self.cfg["engine"]
        command = ["--config", "/game/config.json", "--batch-mode", "--save-results", "/game/results.json",
                   "--save-replay", "/game/replay.dat", "--log-level", "info", "--player-names", *names]
        kwargs: dict = dict(
            command=command, name=f"{name}-engine", working_dir="/game",
            mem_limit=f"{eng['memory_mb']}m", nano_cpus=int(float(eng["cpus"]) * 1e9), pids_limit=256,
            log_config=_log_config(), labels={self.label: name},
        )
        tcp = [s for s in seats if s.is_tcp]
        if tcp:     # on every seat's network at once, at the address the config names (Docker API 1.44+)
            kwargs["network"] = tcp[0].network.name
            kwargs["networking_config"] = {s.network.name: self.client.api.create_endpoint_config(ipv4_address=s.engine_ip)
                                           for s in tcp}
        else:       # built-in bots only: nobody to talk to
            kwargs["network_mode"] = "none"
        engine = self.client.containers.create(eng["image"], **kwargs)
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

    def _start_player(self, name: str, seat: Seat):
        """The strategy's container: on its seat's network only, dialling the engine at its fixed address there
        (an address, not the engine's name: gVisor's network stack never reaches Docker's embedded DNS),
        read-only but for a small /tmp, with the limits of the seat."""
        limits = seat.limits
        strategy = seat.strategy
        cpus = float(limits.get("cpus") or 1.0)
        threads = str(max(1, int(round(cpus))))
        env = {"OMP_NUM_THREADS": threads, "MKL_NUM_THREADS": threads, "OPENBLAS_NUM_THREADS": threads,
               "NUMBA_NUM_THREADS": threads, "CW_GAME": name, "CW_SLOT": str(seat.slot),
               # the root filesystem is read-only: whatever a runtime writes goes to /tmp
               "HOME": "/tmp", "TMPDIR": "/tmp", "DOTNET_CLI_HOME": "/tmp", "NUMBA_CACHE_DIR": "/tmp",
               **strategy.env}
        kwargs: dict = dict(
            name=f"{name}-p{seat.slot}", network=seat.network.name,
            entrypoint=["bash", "-c", WRAPPER, "cw"],
            command=[seat.engine_ip, str(seat.port), seat.token],
            environment=env, labels={self.label: name},
            nano_cpus=int(cpus * 1e9), pids_limit=int(limits.get("pids") or 256),
            read_only=True, tmpfs={"/tmp": f"rw,size={PLAYER_TMP_MB}m,mode=1777"}, log_config=_log_config(),
        )
        hard = self.cfg.get("hardening", default={}) or {}
        if hard.get("no_new_privileges", True):
            kwargs["security_opt"] = ["no-new-privileges:true"]
        if hard.get("cap_drop_all", True):
            kwargs["cap_drop"] = ["ALL"]
        mem = limits.get("memory_mb")
        if mem:
            kwargs["mem_limit"] = f"{int(mem)}m"
            kwargs["memswap_limit"] = f"{int(mem)}m"
        cpu_time = limits.get("cpu_time_sec")
        if cpu_time:
            kwargs["ulimits"] = [Ulimit(name="cpu", soft=int(cpu_time), hard=int(cpu_time) + 5)]
        if strategy.gpu:
            kwargs["device_requests"] = [DeviceRequest(count=-1, capabilities=[["gpu"]])]
        elif self.cfg.get("docker", "runtime"):      # gVisor and the like; a GPU seat keeps the default runtime
            kwargs["runtime"] = self.cfg.get("docker", "runtime")
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
            (gdir / f"player-{seat.slot}.log").write_bytes(logs)
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

    def _cleanup(self, engine, seats: list[Seat], networks: list) -> None:
        for container in [s.container for s in seats] + [engine]:
            if container is None:
                continue
            try:
                container.remove(force=True)
            except docker.errors.APIError as exc:
                log.warning("remove %s: %s", container.name, exc)
        for network in networks:
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
