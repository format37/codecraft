# sim-cpp: our CodeCraft 2020 engine in C++

The C++ clone of the Python engine in [`../sim`](../sim/README.md), for the ladder: the same rules, command line,
config, log lines, `results.json`, replay format and TCP protocol as the official runner `aicup2020` 1.3.0, and
**exactly the games of the Python engine**: from the same config and seed, with the same players, it writes the
same replay and results.json, byte for byte. It draws from a copy of CPython's random generator (MT19937 seeded by
`init_by_array`, `random()`, `getrandbits`, `_randbelow`, `shuffle`, `choice`), makes the same draws in the same
order, and keeps the Python engine's orders of iteration (players, entities and orders by id; the property table in
its own order). The Python engine stays the executable spec: a change of the rules is made there first, then here.

It is also **the local runner**: without `--batch-mode`, as the official runner opens its window, it shows the game
live in the browser, with the official GUI's debug interface for your strategy (below).

```
make                                   # build/codecraft-sim-cpp (g++, C++17, the standard library only)
build/codecraft-sim-cpp --config game.json --batch-mode --save-results results.json --save-replay replay.dat
build/codecraft-sim-cpp --version            # the version of its source, as the server tags the engine image
```

The server builds the engine image from the Dockerfile and the files it copies (Makefile, src, viewer) and tags it
with their hash: `codecraft/sim-cpp:<version>`, the version that `--version`, `make version` and the viewer's help
show. Each game page on the server names the engine it was played on, so a local runner with the same version
plays the games of the server. The server builds a new version before its next game, with no tag to bump.

## The local runner

```
make -C sim-cpp
sim-cpp/build/codecraft-sim-cpp                        # a config screen in the browser: the game, the seats, the seed
sim-cpp/build/codecraft-sim-cpp --config batch1.json   # the official runner's config file, as it is
sim-cpp/build/codecraft-sim-cpp --replay replay.dat    # watch a replay (ours, Ctrl-S, or the official runner's)
```

The viewer opens at http://127.0.0.1:31000/ (`--viewer-port`; `--no-browser` does not open it). A TCP seat waits
for your strategy: start it as usual (the clients connect to 127.0.0.1:31001 with the token `0000000000000000`
unless told otherwise; a config's `"host": null` listens on 127.0.0.1 and `"token": null` takes any token, as in
the official runner). The game starts when every TCP seat is connected. **↻ Restart** ends the game at any moment
and starts it again with the same config, waiting for your strategy: the connected strategies get Finish (the
official clients end as after a game; one that does not answer, at a breakpoint, has its connection cut after
1.5 s), so you change your code, run it again, and play on without restarting the runner; with **same seed** the
new game has the same map and dice. "New game…" after a game opens the config screen.

In the viewer: **P** pauses and plays, **←/→** step a tick (→ at the newest tick plays one more), **Shift** for
ten, the slider and the speed (1 to 100 ticks a second, or as fast as the strategies answer) move through the game,
the wheel zooms, a right drag or Shift+drag moves the camera, **Home** fits the map, a click shows an entity (its
position also in its owner's frame, the one its strategy sees) and its stored order; **Ctrl-S** saves the replay
so far, **Ctrl-E** the state of the tick shown as a `LoadFrom` file (both into the runner's working directory).

The debug interface is the official GUI's (how the official runner answers it was probed on its binary:
`docs/private/engine-probes/step9`):

- `GetAction` comes with `debug_available` true, so the client passes your strategy its debug interface;
- what the strategy draws while answering a tick (`Log`, `Primitives` lines and triangles, `PlacedText`; world
  positions in its own frame, or none for the screen's bottom-left corner, plus a screen offset in pixels) belongs to
  that tick: it shows whenever that tick is on screen, history included; `Clear` clears that tick's drawings;
- `debug_update` is called while the game waits for the next tick (about 14 times a second, at least once between
  ticks) with the view of the tick on screen; what it draws goes to one persistent layer shown on every tick, which
  its `Clear` empties (the starter's `debug_update` clears only that layer);
- with autoflush off, drawings wait for `Flush`, then land on the tick they were sent for;
- `get_state()` returns the canvas size, the mouse in pixels (origin bottom-left) and in world coordinates (in the
  strategy's frame), the keys held (the official names: `A`, `Num1`, `Space`, `LShift`, `LCtrl`, `Left`, `F1`...),
  the camera (looking straight down) and the player's index.

The game in this mode is the batch mode's: from the same config and seed, the same replay and results.

```
src/
  rng.hpp            CPython's random.Random, exactly
  json.*             configs, LoadFrom state files, results.json (as Python's json.dumps(indent=2))
  model.*            the game state and the property table (sim/codecraft_sim/model.py); LoadFrom files both ways
  trans.*            the binary encoding of the protocol (trans.py)
  replay.*           the replay writer (replay.py) and reader (--replay)
  pathfind.*         the A* of moves and hunts (pathfind.py)
  rules.*            one turn (rules.py)
  view.*             what a seat is sent, and the way back for its orders (view.py)
  mapgen.*           the presets and our maps (mapgen.py)
  quickstart.*       the built-in bot (quickstart.py)
  players.*          the seats: TCP clients (with the debug interface), Empty, QuickStart; the log (players.py)
  debug.*            the debug messages and DebugState
  engine.*           the game loop (engine.py), the GUI mode's loop
  live.*             the viewer's HTTP server: the page, the game as server-sent events, the viewer's input
  history.*          the game's states, tick by tick, for the viewer, its rewinding and Ctrl-E
  jsonout.hpp        compact JSON for the event stream
  main.cpp           the command line (cli.py)
viewer/viewer.html   the viewer (built into the binary)
```

Tests (`sim/tests/test_cpp.py` and `test_live.py`, run with the Python tests from `sim/`): every fixture of the
official engine, as the Python engine is checked; the same bytes as the Python engine on every fixture with other
seeds (puppets over TCP included), on generated maps, on games of QuickStart bots, and from states of the stored
official games; and the GUI mode: the batch mode's game, the debug interface through the official Python client's
classes, the viewer's controls, saves and config screen, replays of ours and of the official engine.
`SIM_SLOW=1` adds full 1000-tick games of the three presets and more seeds and states.

Speed: a Round1 game of four QuickStart bots takes 2 to 6 s on one core (the Python engine: 1 to 3 minutes), most
of it in the bots; memory under 5 MB.
