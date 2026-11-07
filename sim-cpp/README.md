# sim-cpp: our CodeCraft 2020 engine in C++

The C++ clone of the Python engine in [`../sim`](../sim/README.md), for the ladder: the same rules, command line,
config, log lines, `results.json`, replay format and TCP protocol as the official runner `aicup2020` 1.3.0, and
**exactly the games of the Python engine**: from the same config and seed, with the same players, it writes the
same replay and results.json, byte for byte. It draws from a copy of CPython's random generator (MT19937 seeded by
`init_by_array`, `random()`, `getrandbits`, `_randbelow`, `shuffle`, `choice`), makes the same draws in the same
order, and keeps the Python engine's orders of iteration (players, entities and orders by id; the property table in
its own order). The Python engine stays the executable spec: a change of the rules is made there first, then here.

```
make                                   # build/codecraft-sim-cpp (g++, C++17, the standard library only)
build/codecraft-sim-cpp --config game.json --batch-mode --save-results results.json --save-replay replay.dat
docker build -t codecraft/sim-cpp:0.1.0 .    # the image the server runs (config.yaml: engine.image, engine.context)
```

```
src/
  rng.hpp            CPython's random.Random, exactly
  json.*             configs, LoadFrom state files, results.json (as Python's json.dumps(indent=2))
  model.*            the game state and the property table (sim/codecraft_sim/model.py)
  trans.*            the binary encoding of the protocol (trans.py)
  replay.*           the replay writer (replay.py)
  pathfind.*         the A* of moves and hunts (pathfind.py)
  rules.*            one turn (rules.py)
  view.*             what a seat is sent, and the way back for its orders (view.py)
  mapgen.*           the presets and our maps (mapgen.py)
  quickstart.*       the built-in bot (quickstart.py)
  players.*          the seats: TCP clients, Empty, QuickStart; the log (players.py)
  engine.*           the game loop (engine.py)
  main.cpp           the command line (cli.py)
```

Tests (`sim/tests/test_cpp.py`, run with the Python tests from `sim/`): every fixture of the official engine, as
the Python engine is checked; and the same bytes as the Python engine on every fixture with other seeds (puppets
over TCP included), on generated maps, on games of QuickStart bots, and from states of the stored official games.
`SIM_SLOW=1` adds full 1000-tick games of the three presets and more seeds and states.

Speed: a Round1 game of four QuickStart bots takes 2 to 6 s on one core (the Python engine: 1 to 3 minutes), most
of it in the bots; memory under 5 MB.
