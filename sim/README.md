# sim: our own CodeCraft 2020 engine

A re-implementation of the CodeCraft 2020 game simulator, to replace the closed official binary
(`engine/aicup2020`, the "oracle") without the server noticing: same rules, same CLI, same `results.json`, same
replay format, same TCP protocol. Python first, as the executable spec; a C++ clone later for the ladder.

Status: the oracle tooling, the fixtures, the binary formats and the rules of a tick are done. Every turn of the
92 distinct stored official games replays exactly through our rules when they make the record's random choices,
and every game played from its first state with our own state carried forward ends with the official scores
(`oracle/corpus.py`). The player views, the game loop, the seats, the CLI and the map generator are not written
yet.

```
codecraft_sim/        the engine (Python 3.10+, standard library only)
  model.py            the game state as dataclasses + the engine's JSON form (LoadFrom state files)
  trans.py            the binary encoding: the TCP protocol of the 15 clients
  replay.py           replay.dat reader and writer (byte-exact on every stored official replay)
  rules.py            one turn of the game: pathfinding, attacks, builds, repairs, movement, the end of the tick
  pathfind.py         the A* of move orders and hunting auto-attacks, as the official engine runs it
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

## Tests

```
cd sim
../server/.venv/bin/python -m pytest              # ~40 s; the oracle tests need engine/aicup2020 to run
SIM_SLOW=1 ../server/.venv/bin/python -m pytest   # + the replay round trip over all of data/games and every
                                                  #   fixture turn replayed on the official engine (~4 min)
```

The rules tests (`tests/test_rules.py`) play every turn of every fixture through `rules.py` with many seeds: each
outcome must be one the official engine gives, and each of its outcomes must come out (the same rules, our own
random draws); and turns of two stored games (the opening of one, every 40th turn of one with fog of war) must
come out exactly as recorded when our rules make the record's random choices. The replay tests use the stored
games in `data/games/` (skipped when there are none) and cross-check against the server's decoder
`server/codecraft/replay.py`. The fixture tests re-run every scenario on the official binary and compare with the
stored file, so a fixture can never silently drift from what the engine does.

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
