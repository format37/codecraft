"""The C++ engine's GUI mode (no --batch-mode): the local runner, with a viewer in the browser in place of the
official runner's window and the official GUI's debug interface for TCP clients (how the official runner answers it:
docs/private/engine-probes/step9); and the official configs' null host and token, in every engine.

- The GUI mode plays the batch mode's game: from the same config and seed, the same replay and results.
- A TCP client gets the debug interface, checked with the official Python client's own classes: debug_available,
  a DebugUpdate between ticks (the view of the tick on screen), the DebugState it asks for (in its own frame); what
  it draws reaches the viewer's event stream in the global frame and on the tick it drew for (a late Flush too), its
  debug_update drawings on the persistent layer.
- The viewer's controls hold the game (paused, a step plays one tick); Ctrl-E and Ctrl-S save a state the replay
  agrees with; the server answers only the page (Host and JSON checks); a config screen starts games.
- A null host listens on 127.0.0.1 and a null token takes any token: the official engine, the Python and the C++ one.
- `--replay FILE` streams a replay file (ours, and the official engine's) state for state.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from codecraft_sim import replay as replay_io
from codecraft_sim.model import model_json

from conftest import ENGINE, ROOT, engine_runs, replays
from test_cpp import CPP, needs_cpp
from test_engine import SIM, custom

sys.path.insert(0, str(ROOT / "clients" / "Python"))
import model as client  # noqa: E402  the official Python client's protocol classes
from stream_wrapper import StreamWrapper  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tcp_seat(port: int, host=None, token=None) -> dict:
    return {"Tcp": {"host": host, "port": port, "accept_timeout": 60, "timeout": None, "token": token}}


class Client(threading.Thread):
    """A TCP client made of the official Python client's classes: no orders; with the debug interface it runs
    `script(client, "get_action" | "debug_update", tick)` and records what it was sent."""

    def __init__(self, port: int, token: str = "any token at all", script=None) -> None:
        super().__init__(daemon=True)
        self.port, self.token, self.script = port, token, script
        self.ticks, self.debug_available, self.updates_before, self.update_ticks, self.states = [], [], [], [], []
        self.finished, self.error, self.drew = False, None, False

    def run(self) -> None:
        try:
            deadline = time.time() + 30
            while True:
                try:
                    sock = socket.create_connection(("127.0.0.1", self.port), timeout=60)
                    break
                except OSError:
                    if time.time() > deadline:
                        raise
                    time.sleep(0.05)
            f = sock.makefile("rwb")
            self.rd, self.wr = StreamWrapper(f), StreamWrapper(f)
            self.wr.write_string(self.token)
            self.wr.flush()
            updates = 0
            while True:
                m = client.ServerMessage.read_from(self.rd)
                if isinstance(m, client.ServerMessage.GetAction):
                    tick = m.player_view.current_tick
                    self.ticks.append(tick)
                    self.debug_available.append(m.debug_available)
                    self.updates_before.append(updates)
                    updates = 0
                    if m.debug_available and self.script:
                        self.script(self, "get_action", tick)
                    client.ClientMessage.ActionMessage(client.Action({})).write_to(self.wr)
                    self.wr.flush()
                elif isinstance(m, client.ServerMessage.DebugUpdate):
                    updates += 1
                    self.update_ticks.append((self.ticks[-1] if self.ticks else None, m.player_view.current_tick))
                    if self.script:
                        self.script(self, "debug_update", m.player_view.current_tick)
                    client.ClientMessage.DebugUpdateDone().write_to(self.wr)
                    self.wr.flush()
                else:
                    self.finished = True
                    break
            sock.close()
        except Exception as exc:  # noqa: BLE001 - reported by the test
            self.error = repr(exc)

    def send(self, command) -> None:
        client.ClientMessage.DebugMessage(command).write_to(self.wr)
        self.wr.flush()

    def state(self):
        client.ClientMessage.RequestDebugState().write_to(self.wr)
        self.wr.flush()
        st = client.DebugState.read_from(self.rd)
        self.states.append(st)
        return st


class Stream(threading.Thread):
    """The viewer's event stream as (event, data) pairs, until the game is over."""

    def __init__(self, port: int) -> None:
        super().__init__(daemon=True)
        self.port, self.events, self.over = port, [], threading.Event()

    def run(self) -> None:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/events", timeout=120) as r:
            name = None
            for raw in r:
                line = raw.decode().rstrip("\n")
                if line.startswith("event: "):
                    name = line[7:]
                elif line.startswith("data: "):
                    self.events.append((name, json.loads(line[6:])))
                    if name == "over":
                        self.over.set()
                        return

    def named(self, name: str) -> list:
        return [d for n, d in list(self.events) if n == name]


def live(tmp_path: Path, config: dict | None, *args: str) -> tuple[subprocess.Popen, int, list]:
    """The GUI mode without a browser, on a free port; returns the process, the viewer's port and its log lines."""
    cmd = [str(CPP), "--no-browser", "--viewer-port", "0", *args]
    if config is not None:
        (tmp_path / "config.json").write_text(json.dumps(config))
        cmd += ["--config", str(tmp_path / "config.json")]
    p = subprocess.Popen(cmd, cwd=tmp_path, stderr=subprocess.PIPE, text=True)
    log, port = [], None
    for line in p.stderr:
        log.append(line)
        if line.startswith("INFO - Viewer: "):
            port = int(line.rstrip().rstrip("/").rsplit(":", 1)[1])
            break
    assert port, "".join(log)
    threading.Thread(target=lambda: log.extend(p.stderr), daemon=True).start()
    return p, port, log


def post(port: int, path: str, body: dict | None = None, host: str | None = None,
         content_type: str = "application/json") -> tuple[int, dict | None]:
    headers = {"Content-Type": content_type}
    if host:
        headers["Host"] = host
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body or {}).encode(),
                                 method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, None


def until(cond, seconds: float = 30) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def vertex(x, y, rgba=(1.0, 0.0, 0.0, 1.0), world=True):
    return client.ColoredVertex(client.Vec2Float(x, y) if world else None, client.Vec2Float(3.0, 4.0),
                                client.Color(*rgba))


@needs_cpp
@pytest.mark.parametrize("game", ["Round1", "fog"])
def test_the_gui_mode_plays_the_batch_game(tmp_path, game):
    config = {"seed": 11, "game": {"Create": "Round1" if game == "Round1" else custom(300, fog=True)},
              "players": ["QuickStart"] * 4}
    (tmp_path / "config.json").write_text(json.dumps(config))
    c = str(tmp_path / "config.json")
    subprocess.run([str(CPP), "--config", c, "--batch-mode", "--save-replay", str(tmp_path / "b.dat"),
                    "--save-results", str(tmp_path / "b.json")], check=True, timeout=120)
    r = subprocess.run([str(CPP), "--config", c, "--no-browser", "--viewer-port", "0", "--viewer-speed", "0",
                        "--viewer-exit", "--save-replay", str(tmp_path / "l.dat"), "--save-results",
                        str(tmp_path / "l.json")], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "l.dat").read_bytes() == (tmp_path / "b.dat").read_bytes()
    assert (tmp_path / "l.json").read_bytes() == (tmp_path / "b.json").read_bytes()


def debug_script(c: Client, kind: str, tick: int) -> None:
    D, C = client.DebugData, client.DebugCommand
    if kind == "debug_update":
        if not c.drew:
            c.send(C.Add(D.Log("persistent")))
            c.drew = True
        c.state()
        return
    c.state()
    if tick == 1:
        c.send(C.Add(D.Primitives([vertex(5, 5), vertex(6, 7)], client.PrimitiveType.LINES)))
    elif tick == 2:
        c.send(C.Add(D.Log("gone")))
        c.send(C.Clear())
        c.send(C.Add(D.PlacedText(vertex(1, 2, world=False), "text", 0.5, 20.0)))
    elif tick == 3:
        c.send(C.SetAutoFlush(False))
        c.send(C.Add(D.Log("late")))
    elif tick == 5:
        c.send(C.Flush())
        c.send(C.SetAutoFlush(True))


@needs_cpp
def test_the_debug_interface(tmp_path):
    """Seat 1 (its frame turned by 180 degrees) on a 40 map: what it draws and the debug state it gets."""
    port = free_port()
    config = {"seed": 5, "game": {"Create": custom(30)}, "players": ["QuickStart", tcp_seat(port)]}
    c = Client(port, script=debug_script)
    c.start()
    p, vport, log = live(tmp_path, config, "--viewer-speed", "0", "--viewer-exit")
    stream = Stream(vport)
    stream.start()
    assert p.wait(60) == 0, "".join(log)
    c.join(10)
    stream.over.wait(10)
    assert c.error is None and c.finished
    assert c.ticks == list(range(30)) and all(c.debug_available)
    assert all(n >= 1 for n in c.updates_before[1:])          # a debug update between every two ticks
    assert all(shown == asked for asked, shown in c.update_ticks if asked is not None)
    st = c.states[0]                                          # no viewer: its defaults, in seat 1's frame
    assert (st.player_index, st.window_size.x, st.window_size.y) == (1, 800, 600)
    assert (st.mouse_pos_world.x, st.mouse_pos_world.y) == (40.0, 40.0)    # global (0, 0)
    assert (st.camera.center.x, st.camera.center.y) == (0.0, 0.0)          # global (40, 40)
    dbg = [(d["t"], d["ops"]) for d in stream.named("dbg") if d["s"] == 1]
    layers = {}
    for t, ops in dbg:
        layers.setdefault(t, []).extend(ops)
    line = layers[1][0]
    assert (line["k"], line["p"]) == (1, 0)
    assert line["v"] == [35, 35, 3, 4, 1, 0, 0, 1, 34, 33, 3, 4, 1, 0, 0, 1]   # 40 - x, 40 - y; offsets as sent
    assert [op.get("x", "clear" if op.get("c") else None) for op in layers[2]] == ["gone", "clear", "text"]
    assert layers[2][2]["v"][:2] == [None, None] and layers[2][2]["al"] == 0.5
    assert [op["x"] for op in layers[3]] == ["late"]
    assert [op["x"] for op in layers[-1]] == ["persistent"]
    order = [(n, d.get("t")) for n, d in stream.events if n in ("tick", "dbg")]
    assert order.index(("dbg", 3)) > order.index(("tick", 5))  # flushed at tick 5, drawn on tick 3


@needs_cpp
def test_the_viewer_controls_and_saves(tmp_path):
    config = {"seed": 3, "game": {"Create": custom(40)}, "players": ["QuickStart", "QuickStart"]}
    p, port, log = live(tmp_path, config, "--viewer-speed", "1", "--viewer-exit")
    try:
        assert post(port, "/control", {"playing": False})[0] == 200
        stream = Stream(port)
        stream.start()
        time.sleep(1.5)
        assert stream.named("tick") == []                       # paused before the first tick
        for k in (1, 2):
            post(port, "/control", {"step": True})
            assert until(lambda: len(stream.named("tick")) == k)
        time.sleep(0.5)
        assert [d["t"] for d in stream.named("tick")] == [1, 2]
        status, reply = post(port, "/save-state", {"tick": 2})
        state = Path(reply["message"].split(" saved to ")[1].split(" (")[0])
        _, reply = post(port, "/save-replay")
        saved = Path(reply["message"].split(" saved to ")[1])
        replay = replay_io.load(saved)
        assert replay.tick_count == 2
        at2 = list(replay.states())[2]
        assert model_json(at2) == json.loads(state.read_text())
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
            assert r.status == 200 and b"<canvas" in r.read()
        assert post(port, "/new", host=f"evil.example:{port}")[0] == 403
        assert post(port, "/control", {"playing": True}, content_type="text/plain")[0] == 403
        post(port, "/control", {"playing": True, "speed": 0})
        assert p.wait(60) == 0
        stream.over.wait(10)
        assert stream.named("over")[0]["tick"] == 40
    finally:
        p.kill()


@needs_cpp
def test_the_config_screen(tmp_path):
    p, port, log = live(tmp_path, None, "--viewer-speed", "0", "--viewer-exit")
    try:
        stream = Stream(port)
        stream.start()
        assert until(lambda: stream.named("config"))
        offer = stream.named("config")[0]
        assert offer["config"]["players"] == [{"Tcp": {"host": None, "port": 31001, "accept_timeout": None,
                                                         "timeout": None, "token": None}}] + ["QuickStart"] * 3
        post(port, "/start", {"config": {"seed": 1, "game": {"Create": "Round9"}, "players": ["QuickStart"] * 2}})
        assert until(lambda: len(stream.named("config")) == 2)
        assert "Round9" in stream.named("config")[1]["error"]
        good = {"seed": 1, "game": {"Create": custom(25)}, "players": ["QuickStart", {"Empty": None}]}
        post(port, "/start", {"config": good})
        assert p.wait(60) == 0, "".join(log)
        stream.over.wait(10)
        init = stream.named("init")[0]
        assert init["seed"] == "1" and [s["kind"] for s in init["seats"]] == ["quickstart", "empty"]
        assert stream.named("over")[0]["tick"] == 25
    finally:
        p.kill()


@pytest.mark.parametrize("engine", ["official", "python", "cpp"])
def test_a_null_host_and_token_as_the_official_runner(tmp_path, engine):
    """The official configs (batch1.json): `host` null listens on 127.0.0.1, `token` null takes any token."""
    exe = {"official": ENGINE, "python": SIM, "cpp": CPP}[engine]
    if engine == "official" and not engine_runs():
        pytest.skip("the official engine cannot run here")
    if engine == "cpp" and not CPP.exists():
        pytest.skip("the C++ engine is not built")
    port = free_port()
    (tmp_path / "config.json").write_text(json.dumps(
        {"seed": 5, "game": {"Create": custom(20)}, "players": [tcp_seat(port), "QuickStart"]}))
    c = Client(port, token="whatever the client sends")
    c.start()
    r = subprocess.run([str(exe), "--config", str(tmp_path / "config.json"), "--batch-mode", "--save-results",
                        str(tmp_path / "results.json")], capture_output=True, text=True, timeout=120)
    c.join(30)
    assert r.returncode == 0, r.stderr
    assert json.loads((tmp_path / "results.json").read_text())["players"][0] == {"crashed": False, "comment": None}
    assert c.finished and c.ticks == list(range(20)) and not any(c.debug_available)


def streamed_states(events: list) -> list:
    """Every state the viewer rebuilds from the stream: (tick, entities by id, players by id)."""
    init = next(d for n, d in events if n == "init")
    entities = {e[0]: e for e in init["entities"]}
    players = {p[0]: p for p in init["players"]}
    out = [(init["tick"], dict(entities), dict(players))]
    for name, d in events:
        if name != "tick":
            continue
        for p in d.get("p", []):
            players[p[0]] = p
        for i in d.get("r", []):
            del entities[i]
        for e in d.get("u", []) + d.get("a", []):
            entities[e[0]] = e
        out.append((d["t"], dict(entities), dict(players)))
    return out


@needs_cpp
def test_a_replay_in_the_viewer(tmp_path):
    """`--replay FILE`: the states rebuilt from the stream are the file's, tick for tick (ours and official ones)."""
    config = {"seed": 9, "game": {"Create": custom(150, fog=True)}, "players": ["QuickStart"] * 4}
    (tmp_path / "config.json").write_text(json.dumps(config))
    ours = tmp_path / "ours.dat"
    subprocess.run([str(CPP), "--config", str(tmp_path / "config.json"), "--batch-mode", "--save-replay", str(ours)],
                   check=True, timeout=60)
    for path, every in [(ours, 1)] + [(p, 25) for p in replays()[:2]]:
        p = subprocess.Popen([str(CPP), "--replay", str(path), "--no-browser", "--viewer-port", "0"],
                             stderr=subprocess.PIPE, text=True)
        try:
            port = None
            for line in p.stderr:
                if line.startswith("INFO - Viewer: "):
                    port = int(line.rstrip().rstrip("/").rsplit(":", 1)[1])
                    break
            stream = Stream(port)
            stream.start()
            assert stream.over.wait(60)
        finally:
            p.kill()
        streamed = streamed_states(stream.events)
        recorded = list(replay_io.load(path).states())
        assert len(streamed) == len(recorded)
        for (tick, entities, players), m in list(zip(streamed, recorded))[::every]:
            assert tick == m.current_tick
            assert entities == {e.id: [e.id, e.player_id, int(e.entity_type), e.position.x, e.position.y, e.health,
                                       int(e.active)] for e in m.entities.values()}
            assert players == {q.id: [q.id, q.index, q.score, q.resource] for q in m.players.values()}

