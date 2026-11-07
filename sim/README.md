# sim: our own CodeCraft 2020 engine

A re-implementation of the CodeCraft 2020 game simulator, to replace the closed official binary
(`engine/aicup2020`, the "oracle") without the server noticing: same rules, same CLI, same `results.json`, same
replay format, same TCP protocol. Python first, as the executable spec; a C++ clone later for the ladder.

Status: a complete engine. Every turn of the 92 distinct stored official games replays exactly through our rules
when they make the record's random choices, and every game played from its first state with our own state
carried forward ends with the official scores (`oracle/corpus.py`); every view the official engine sends, fog of
war and the seat's frame included, is ours (`tests/test_view.py`); and every fixture played by our engine as a
whole, in place of the official binary (seats over TCP, crashes, log lines, results.json), gives what the official
engine gave (`tests/test_engine.py`). The C++ clone in [`../sim-cpp`](../sim-cpp/README.md) plays exactly the
games of this engine, seed for seed, 20 to 40 times faster (`tests/test_cpp.py`): this one stays the executable
spec, a change of the rules is made here first.

```
codecraft_sim/        the engine (Python 3.10+, standard library only)
  model.py            the game state as dataclasses + the engine's JSON form (LoadFrom state files)
  trans.py            the binary encoding: the TCP protocol of the 15 clients
  replay.py           replay.dat reader and writer (byte-exact on every stored official replay)
  rules.py            one turn of the game: pathfinding, attacks, builds, repairs, movement, the end of the tick
  pathfind.py         the A* of move orders and hunting auto-attacks, as the official engine runs it
  view.py             what a seat is sent: fog of war, its own frame, and the way back for its actions
  mapgen.py           the presets (Round1, Round2, Finals, Custom) and our map generator
  players.py          the seats: TCP clients (the official protocol, timeouts, crashes), Empty, QuickStart
  quickstart.py       the built-in bot: the starter strategy of format37/codecraft-starter
  engine.py           the game loop: seats, views, orders, turns, the replay, results.json
  cli.py              the official runner's command line (`python3 -m codecraft_sim --config ... --batch-mode`)
codecraft-sim         that command line from a checkout (the oracle tools run it in place of engine/aicup2020)
Dockerfile            the engine image codecraft/sim (tagged with the hash of its source; engine.context: sim)
oracle/               tools that interrogate the official binary
  state.py            build small states by hand (or take one from a replay tick)
  loadfrom.py         run the binary from a state, read back every tick
  puppet.py           a scripted TCP player: plays a script, records its views, misbehaves on cue
  fixture.py          record / compare / store fixtures
  scenarios.py        the scenarios behind the fixtures, and the CLI (list, record, check, alternatives, show)
  corpus.py           every turn of the stored official games (data/games) through our rules, compared exactly
fixtures/             one JSON per rule question, recorded from the binary (format: fixtures/README.md)
tests/                pytest
```

## Running it

```
cd sim
./codecraft-sim --config game.json --batch-mode --save-results results.json --save-replay replay.dat
docker build -t codecraft/sim:dev . && docker run --rm -v $PWD:/game codecraft/sim:dev --config /game/game.json \
    --batch-mode --save-results /game/results.json --save-replay /game/replay.dat
```

`game.json` is the official runner's config: `{"seed": 7, "game": {"Create": "Round1"}, "players": ["QuickStart",
"QuickStart", "QuickStart", "QuickStart"]}`; a seat is `{"Tcp": {"host", "port", "accept_timeout", "timeout",
"token"}}` (a null host listens on 127.0.0.1, a null token takes any token), `"QuickStart"` or `{"Empty": null}`;
the game `{"Create": "Round1" | "Round2" | "Finals" | {"Custom": {...}}}` or `{"LoadFrom": "/abs/state.json"}`. The
server runs it when config.yaml says `engine: {image: codecraft/sim, context: sim}` (or CW_ENGINE_IMAGE /
CW_ENGINE_CONTEXT); without a tag of its own the image is tagged with the hash of its source. A 4-player game of
1000 ticks takes about a minute in pure Python, plus the players' time.

## Tests

```
cd sim
../server/.venv/bin/python -m pytest              # ~90 s; the oracle tests need engine/aicup2020 to run
SIM_SLOW=1 ../server/.venv/bin/python -m pytest   # + the replay round trip over all of data/games, every fixture
                                                  #   turn replayed on the official engine, full games (~15 min)
```

The rules tests (`tests/test_rules.py`) play every turn of every fixture through `rules.py` with many seeds: each
outcome must be one the official engine gives, and each of its outcomes must come out (the same rules, our own
random draws); and turns of two stored games (the opening of one, every 40th turn of one with fog of war) must
come out exactly as recorded when our rules make the record's random choices. The view tests (`tests/test_view.py`)
compare every view a recording puppet got in the fixtures with ours, and with the engine, every view of two stored
fog games played on from some of their states. The engine tests (`tests/test_engine.py`) play every fixture again
with `codecraft-sim` in place of the binary, through the same oracle tools: the exit code, the log lines, the
crashes and their comments, each puppet's views and `Finish`, and every tick until a random turn takes another of
the official outcomes, must be what the official engine gave. The map tests (`tests/test_mapgen.py`) check the
presets' starting entities against the stored official games, id for id, and our maps' symmetry, free corners and
paths. The C++ tests (`tests/test_cpp.py`, after `make -C ../sim-cpp`, which they run) check the C++ engine on every
fixture as the Python engine is checked, and that it writes the same bytes as this engine from the same config and
seed: on the fixtures with other seeds, on maps, on games of QuickStart bots and from stored official states. The
local runner's tests (`tests/test_live.py`) check the C++ engine's GUI mode: the batch mode's game, the official
GUI's debug interface through the official Python client's classes, the viewer's controls and saves, replays; and
the official configs' null host and token in the official engine and in both of ours. The
replay tests use the stored games in `data/games/` (skipped when there are none) and cross-check against the
server's decoder `server/codecraft/replay.py`. The fixture tests re-run every scenario on the official
binary and compare with the stored file, so a fixture can never silently drift from what the engine does.

## The oracle

```
cd sim
python3 -m oracle.scenarios list            # scenarios and the rule questions they answer
python3 -m oracle.scenarios show move-free  # a fixture, tick by tick
python3 -m oracle.scenarios record NAME     # (re)record from the binary, alternatives included
python3 -m oracle.corpus                    # every turn of the stored games, exact (24 minutes on 8 cores)
python3 -m oracle.corpus --every 20 --games 16 --report /tmp/r.json   # a quick sample, differences to a file
```

A new rule question becomes a scenario in `oracle/scenarios.py` (a small `State` and its players), then a fixture
with `record`. Everything runs on the host: the binary takes a state with `"game": {"LoadFrom": ...}` and plays
it in milliseconds; puppets connect over 127.0.0.1.
