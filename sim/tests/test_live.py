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
- The server's limits for TCP seats (the server's numbers by default): as on the server, a seat slower than it
  allows, to answer a tick or in all, is cut off there; warn only (`--limits warn`, or the viewer's switch at any
  moment) lets it play on; the viewer gets every answer's time either way.
- A null host listens on 127.0.0.1 and a null token takes any token: the official engine, the Python and the C++ one.
- `--replay FILE` streams a replay file (ours, and the official engine's) state for state.
- Restart ends the game at once and plays its config again, waiting for the strategies to reconnect.
"""
from __future__ import annotations

import json
import os
import re
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
from test_cpp import CPP, make_version, needs_cpp
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


def live(tmp_path: Path, config: dict | None, *args: str, env: dict | None = None,
         no_browser: bool = True) -> tuple[subprocess.Popen, int, list]:
    """The GUI mode without a browser, on a free port; returns the process, the viewer's port and its log lines."""
    cmd = [str(CPP), *(["--no-browser"] if no_browser else []), "--viewer-port", "0", *args]
    if config is not None:
        (tmp_path / "config.json").write_text(json.dumps(config))
        cmd += ["--config", str(tmp_path / "config.json")]
    p = subprocess.Popen(cmd, cwd=tmp_path, stderr=subprocess.PIPE, text=True, env=env)
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
            page = r.read().decode()
            assert r.status == 200 and "<canvas" in page
            assert f"Engine version <code>{make_version()}</code>" in page          # the help names the engine
            # no game page's data in it (the server puts one there): the page follows this runner
            assert '<script id="cc-site" type="application/json">null</script>' in page
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


@needs_cpp
def test_restart(tmp_path):
    """The viewer's Restart: the game ends at once, its clients get Finish (as after a game), and it starts again with
    the same config, waiting for the strategies; `same_seed` keeps the seed; while waiting for connections it starts
    again at once; a strategy that does not answer has its connection cut (with the server's limits only warned
    about, as at a breakpoint: else it would be cut off after a second anyway)."""
    port = free_port()
    config = {"seed": None, "game": {"Create": custom(300)}, "players": [tcp_seat(port), "QuickStart"]}
    p, vport, log = live(tmp_path, config, "--viewer-speed", "20", "--limits", "warn")
    try:
        stream = Stream(vport)
        stream.start()
        inits = lambda: stream.named("init")  # noqa: E731
        a = Client(port)
        a.start()
        assert until(lambda: len(a.ticks) >= 3)
        post(vport, "/reset")
        assert until(lambda: a.finished and len(inits()) == 2, 5)
        assert inits()[1]["seats"][0]["status"] == "waiting" and inits()[1]["seed"] != inits()[0]["seed"]
        b = Client(port)
        b.start()
        assert until(lambda: len(b.ticks) >= 2)
        assert b.ticks[:2] == [0, 1]
        post(vport, "/reset", {"same_seed": True})
        assert until(lambda: b.finished and len(inits()) == 3, 5)
        assert inits()[2]["seed"] == inits()[1]["seed"]
        start = time.time()
        post(vport, "/reset")                                   # nobody connected yet
        assert until(lambda: len(inits()) == 4, 5) and time.time() - start < 2
        stuck = Client(port, script=lambda c, kind, tick: time.sleep(30) if (kind, tick) == ("get_action", 1) else 0)
        stuck.start()
        assert until(lambda: len(stuck.ticks) == 2)
        post(vport, "/reset")
        assert until(lambda: len(inits()) == 5, 6)
        assert p.poll() is None, "".join(log)
    finally:
        p.kill()



def server_limits() -> tuple[float, float]:
    """The server's limits for a strategy (config.yaml): seconds to answer a tick, CPU seconds for a game."""
    text = (ROOT / "config.yaml").read_text()
    tick = re.search(r"^\s+tick_timeout_sec:\s*([\d.]+)", text, re.M)
    game = re.search(r"^\s+cpu_time_sec:\s*([\d.]+)", text, re.M)
    return float(tick.group(1)), float(game.group(1))


def slow(seconds: float, ticks=None):
    """A client script: answer the ticks given (all, by default) after `seconds`."""
    return lambda c, kind, tick: time.sleep(seconds) if kind == "get_action" and (ticks is None or tick in ticks) \
        else None


@needs_cpp
def test_the_server_limits_by_default(tmp_path):
    """Without options the runner holds TCP seats to the server's own limits, cut off past them."""
    config = {"seed": 1, "game": {"Create": custom(20)}, "players": [tcp_seat(free_port()), "QuickStart"]}
    p, port, log = live(tmp_path, config)
    try:
        stream = Stream(port)
        stream.start()
        assert until(lambda: stream.named("init"), 10), "".join(log)
        tick, game = server_limits()
        assert stream.named("init")[0]["limits"] == {"server": True, "tick": tick, "game": game}
    finally:
        p.kill()


@needs_cpp
@pytest.mark.parametrize("case", ["tick", "game"])
def test_a_strategy_is_cut_off_past_the_server_limits(tmp_path, case):
    """Too long on one tick (0.5 s of 0.3), or in all (0.25 s a tick, 0.6 s a game: the third answer has 0.1 s
    left): the seat is cut off on that tick, tick 2, as the server would; the game goes on without it."""
    port = free_port()
    config = {"seed": 1, "game": {"Create": custom(12)}, "players": [tcp_seat(port), "QuickStart"]}
    c = Client(port, script=slow(0.5, {2}) if case == "tick" else slow(0.25))
    c.start()
    p, vport, log = live(tmp_path, config, "--viewer-speed", "0", "--viewer-exit", "--tick-limit", "0.3",
                         "--game-limit", "0.6", "--save-results", str(tmp_path / "results.json"))
    stream = Stream(vport)
    stream.start()
    assert p.wait(60) == 0, "".join(log)
    stream.join(10)                     # the runner is gone: all its events are in
    answers = stream.named("answers")
    assert [a["t"] for a in answers] == [0, 1, 2], answers
    ms = [a["ms"][0] for a in answers]
    assert all(a["ms"][1] is None for a in answers)                 # the built-in bot is not timed
    comment = ("Player crashed: no answer in 0.3 s, the server's limit for a tick" if case == "tick" else
               "Player crashed: 0.6 s of answers in all, the server's limit for a game (the server counts CPU time)")
    assert stream.named("seat")[-1] == {"s": 0, "status": "crashed", "comment": comment}
    if case == "tick":
        assert max(ms[:2]) < 250 and 290 <= ms[2] < 450, ms
    else:
        assert all(240 <= v < 350 for v in ms[:2]) and 80 <= ms[2] < 200, ms
        assert 0.58 <= answers[-1]["used"][0] < 0.75
    results = json.loads((tmp_path / "results.json").read_text())
    assert results["players"][0] == {"crashed": True, "comment": comment}


@needs_cpp
def test_warn_only_lets_a_strategy_think(tmp_path):
    """Switched to warn only (as the viewer does, before the strategy connects), a seat slower than the limits
    plays on, every answer timed; switched back mid-game, the next slow answer is cut off."""
    port = free_port()
    config = {"seed": 1, "game": {"Create": custom(30)}, "players": [tcp_seat(port), "QuickStart"]}
    p, vport, log = live(tmp_path, config, "--viewer-speed", "0", "--viewer-exit", "--tick-limit", "0.2",
                         "--game-limit", "0.5")
    try:
        stream = Stream(vport)
        stream.start()
        assert until(lambda: stream.named("init"), 10)
        assert post(vport, "/control", {"limits": "warn"})[0] == 200
        assert until(lambda: stream.named("limits") == [{"server": False}], 5)
        c = Client(port, script=slow(0.25))
        c.start()
        assert until(lambda: len(stream.named("answers")) >= 4, 20)
        assert stream.named("seat")[-1]["status"] == "connected"
        assert all(a["ms"][0] >= 240 for a in stream.named("answers"))
        assert stream.named("answers")[3]["used"][0] > 0.9           # past the game's 0.5 s: only warned about
        post(vport, "/control", {"limits": "server"})
        assert p.wait(60) == 0, "".join(log)
        stream.join(10)                 # the runner is gone: all its events are in
        assert stream.named("limits")[-1] == {"server": True}
        assert stream.named("seat")[-1]["status"] == "crashed"
        last = stream.named("answers")[-1]
        assert last["ms"][0] < 50                     # the game's limit was used up: no time left to answer
        assert stream.named("seat")[-1]["comment"].startswith("Player crashed: 0.5 s of answers in all")
    finally:
        p.kill()


def loopback_get(port: int, address: str, host: str) -> int:
    """GET / at a loopback address, with the Host header of the test's choosing (the browser's, behind docker -p)."""
    req = urllib.request.Request(f"http://{address}:{port}/", headers={"Host": host})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def listening(log: list) -> bool:
    return any("Waiting for connection on port" in line for line in log)


@needs_cpp
@pytest.mark.skipif(sys.platform != "linux", reason="every 127.x.y.z address is loopback on Linux only")
def test_bind_listens_on_every_address(tmp_path):
    """`--bind 0.0.0.0` (the container image's setting): the viewer and the TCP seats without a host answer on another
    loopback address too, and the Host check still wants the page's own address, with any port (docker -p remaps
    it). Without --bind they listen on 127.0.0.1 alone."""
    seat = free_port()
    config = {"seed": 1, "game": {"Create": "Round1"},
              "players": [tcp_seat(seat), "QuickStart", "QuickStart", "QuickStart"]}
    p, port, log = live(tmp_path, config, "--bind", "0.0.0.0", "--viewer-speed", "0", "--viewer-exit")
    try:
        assert loopback_get(port, "127.0.0.2", f"127.0.0.1:{port}") == 200
        assert loopback_get(port, "127.0.0.2", "localhost:31000") == 200         # docker -p 31000:<our port>
        assert loopback_get(port, "127.0.0.2", "[::1]") == 200
        assert loopback_get(port, "127.0.0.2", f"127.0.0.2:{port}") == 403        # not the page's own address
        assert loopback_get(port, "127.0.0.2", f"evil.example:{port}") == 403
        assert loopback_get(port, "127.0.0.2", f"localhost.evil.example:{port}") == 403
        assert until(lambda: listening(log))
        socket.create_connection(("127.0.0.2", seat), timeout=10).close()       # the seat, from another address
        assert p.wait(timeout=120) == 0, "".join(log)
        assert any("Got connection on port" in line for line in log), "".join(log)
    finally:
        p.kill()
    seat = free_port()
    config["players"][0] = tcp_seat(seat)
    p, port, log = live(tmp_path, config, "--viewer-exit")
    try:
        assert until(lambda: listening(log))
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.2", seat), timeout=5)
        with pytest.raises(urllib.error.URLError):
            urllib.request.urlopen(f"http://127.0.0.2:{port}/", timeout=5)
        assert loopback_get(port, "127.0.0.1", f"127.0.0.1:{port}") == 200
        socket.create_connection(("127.0.0.1", seat), timeout=10).close()
    finally:
        p.kill()


@needs_cpp
def test_the_container_image_defaults(tmp_path):
    """CODECRAFT_RUNNER_CONTAINER=1 (the image's environment) stands for --bind 0.0.0.0 --no-browser: the viewer's URL
    is printed and no browser is launched, while on a machine the browser opens on that URL; a flag on the command
    line wins over the environment."""
    opener = "open" if sys.platform == "darwin" else "xdg-open"       # what the runner launches the browser with
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / opener).write_text(f'#!/bin/sh\necho "$1" > "{tmp_path}/opened"\n')
    (bin_dir / opener).chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    container = {**env, "CODECRAFT_RUNNER_CONTAINER": "1"}
    config = {"seed": 1, "game": {"Create": "Round1"}, "players": ["QuickStart"] * 4}
    fast = ("--viewer-speed", "0", "--viewer-exit")

    p, port, log = live(tmp_path, config, *fast, env=container, no_browser=False)
    try:
        if sys.platform == "linux":
            assert loopback_get(port, "127.0.0.2", f"127.0.0.1:{port}") == 200   # 0.0.0.0
        assert p.wait(timeout=120) == 0, "".join(log)
    finally:
        p.kill()
    assert f"INFO - Viewer: http://127.0.0.1:{port}/\n" in log, "".join(log)
    assert not (tmp_path / "opened").exists()

    p, port, log = live(tmp_path, config, *fast, env=env, no_browser=False)
    try:
        assert p.wait(timeout=120) == 0, "".join(log)
    finally:
        p.kill()
    assert until(lambda: (tmp_path / "opened").exists(), 5)
    assert (tmp_path / "opened").read_text().strip() == f"http://127.0.0.1:{port}/"

    if sys.platform == "linux":
        p, port, log = live(tmp_path, config, *fast, "--bind", "127.0.0.1", env=container)
        try:
            with pytest.raises(urllib.error.URLError):
                urllib.request.urlopen(f"http://127.0.0.2:{port}/", timeout=5)
            assert p.wait(timeout=120) == 0, "".join(log)
        finally:
            p.kill()
