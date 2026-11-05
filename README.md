# CodeWars — a self-hosted CodeCraft 2020 sandbox

[Russian AI Cup 2020 "CodeCraft"](https://github.com/MailRuChamps/raic-2020) was a contest where
bots fight on an 80×80 grid: gather resources, build bases, houses and turrets, make units,
destroy the opponents. The official server (cups.online) is gone, but the official **local runner**
(`aicup2020` 1.3.0, the very engine the server used) survived. CodeWars is the missing piece: it
builds strategies into Docker images, plays them against each other in a loop, keeps a sandbox-style
Elo ladder and stores replays for the official viewer.

```
                 ┌──────────── docker network cw-<game> (internal, no internet) ───────────┐
                 │  engine (aicup2020 --batch-mode)  ◄── TCP ──  strategy containers ×4     │
                 └───────────────────────────────────────────────────────────────────────────┘
   ./cw loop ──► matchmaking ──► game ──► results.json / replay.dat ──► SQLite ──► web UI :8080
```

## Quick start

Requirements: Docker (with the `nvidia` runtime if you want GPUs), Python 3.10+ for the CLI
(or run everything through `docker compose`).

```bash
python3 -m venv server/.venv && server/.venv/bin/pip install -r server/requirements.txt
./cw build                                   # engine image + every enabled strategy
./cw play --format Round2 --players format37 thermit dragoonxen quickstart
./cw loop --max-games 10                     # sandbox: random formats, rating-aware matchmaking
./cw serve                                   # http://localhost:8080 — ladder, games, replays
./cw ladder                                  # the same in the terminal
./cw watch 3                                 # open game #3 in the official viewer (needs a display)
```

Or without a local Python:

```bash
docker compose build
docker compose run --rm cli build
docker compose up -d web loop                # UI on :8080 and the sandbox loop, restart on reboot
docker compose run --rm cli play --format Finals --players thermit dragoonxen
docker compose logs -f loop
```

Everything a game produces is in `data/games/<id>/`: `config.json` (what the engine got),
`results.json`, `replay.dat`, `engine.log`, one `player-<seat>-<name>.log` per strategy with its
stdout/stderr, exit code and CPU time, and `meta.json`.

## Strategies

Each strategy lives in `strategies/<name>/strategy.yaml` (see [strategies/README.md](strategies/README.md)).
Sources are pulled from git at a pinned commit (or from a local directory) and built exactly the way
the 2020 server did: the official client of the language (`clients/<Lang>`) + the participant's files
+ the client's `compile.sh`; at game time the client's `run.sh host port token` is executed.

Included:

| name | language | source |
|------|----------|--------|
| `format37` | Python | https://github.com/format37/russianaicup2020 |
| `dragoonxen` | C++ | https://bitbucket.org/DragoonXen/codecraft |
| `thermit` | Java | https://github.com/ThermIt/CodeCraft-2020 |
| `quickstart` | built-in engine bot | — |

To add a friend's strategy: create `strategies/<name>/strategy.yaml`, point `source.git` (or
`source.dir`) at the code, run `./cw build <name>`. All 15 official client languages work out of
the box; `templates/` holds base-image overrides where the 2020 images no longer build
(Python: `sklearn` → `scikit-learn`; C++: `gcc:13` instead of the dead buster image).

## Time configuration and other limits

`config.yaml` → `limits` (every strategy container):

| key | default | 2020 official | meaning |
|-----|---------|---------------|---------|
| `tick_timeout_sec` | 1.0 | 1 s | reply budget per tick, enforced by the engine (`null` = unlimited) |
| `cpu_time_sec` | 40 | 40 s | CPU seconds per game, enforced with `RLIMIT_CPU` (`null` = unlimited) |
| `memory_mb` | 1024 | 256 MB | docker memory cap; counts the whole container (a `-Xmx250m` JVM peaks at ~560 MB) |
| `cpus` | 1.0 | 1 core | docker CPU quota |
| `accept_timeout_sec` | 60 | — | how long the engine waits for a strategy to connect |
| `game_wall_timeout_sec` | auto | — | hard wall-clock cap per game |

Formats (`Round1` 4 players / `Round2` 4 players + fog of war / `Finals` 1v1) are chosen at random
with the `formats` weights; `points` holds the official place → points tables; the rating is a
multiplayer Elo (`rating.k`, decaying to `k_min` with games played, as the sandbox's "inertia").

## CUDA

The engine is a closed binary and only talks to a strategy over TCP, so GPU code lives entirely in
the strategy's own process and container. Two things make that work here:

1. `gpu: true` in `strategy.yaml` — the container is started with all host GPUs (needs the nvidia
   container runtime; this is the `--gpus all` equivalent).
2. A base image with a CUDA toolchain, picked with `language:`:
   - `cpp-cuda` — official C++ client on `nvidia/cuda:12.4-devel`: **`.cu` kernels compiled by
     nvcc at build time**. The strategy ships its own `CMakeLists.txt` enabling the CUDA language;
     use `source.merge: true` so your files are laid over the official client instead of replacing it.
   - `python-cuda` — PyTorch 2.5 + CuPy: `cupy.RawKernel("...CUDA C...", "name")` compiles kernel
     source at start-up (NVRTC) and launches it like any CUDA kernel; PyTorch/numba.cuda work too.

Two disabled demo strategies prove it end to end with the same hand-written kernel (per-cell distance
to the nearest enemy and number of enemies that can hit the cell, recomputed every tick on the GPU):

```bash
./cw build cuda-demo-cpp cuda-demo-py
./cw play --format Round1 --players cuda-demo-cpp cuda-demo-py dragoonxen quickstart
grep '\[gpu\]' data/games/<id>/player-*.log      # device name, kernel timings per 100 ticks
```

Things to know: a CUDA context costs a few hundred MB of host memory (the demos set
`limits: {memory_mb: 2048}` — `strategy.yaml` may override any `limits` key for that strategy);
create the context and warm the kernel up *before* connecting to the engine, because the per-tick
clock starts with the first message (the demos do this in `main.cpp` / at import time); two
strategies may share the GPU in one game.

## Watching replays

`replay.dat` files are in the official format; `engine/aicup2020 --replay <file>` opens the
official 3D viewer (the binary chdirs to its own directory, so `engine/assets` is kept next to it).
`./cw watch <game id>` does that for you on a desktop.

## Layout

```
engine/      aicup2020 1.3.0 binary (+ GUI assets) and its Dockerfile
clients/     official v1.3.0 clients, GPL-3.0 (MailRuChamps/raic-2020)
templates/   base-image overrides per language (python, python-cuda, cpp)
strategies/  one directory per strategy (manifest, overlay files, prebuild script)
server/      the orchestrator: codewars package, tests, Dockerfile
docs/        official rules (en/ru), changelog, tournament formats
data/        SQLite db, build cache, game directories (git-ignored)
```

Tests: `server/.venv/bin/python -m pytest server/tests`.

## Engine notes (black box)

- `aicup2020` chdirs to its own directory at start: always pass absolute paths.
- `--batch-mode` needs no display and no assets; a 4-bot game takes ~10 s of engine time.
- Config: `{"seed": null|int, "game": {"Create": "Round1|Round2|Finals"}, "players": [...]}` with
  players `"QuickStart"`, `{"Empty": null}` or `{"Tcp": {"host", "port", "accept_timeout",
  "timeout", "token"}}`; `timeout` is per tick. At most 4 players. `{"Create": {"Custom": {...}}}`
  exists (needs `map_size`, `initial_resource`, `max_tick_count`, `fog_of_war`, …) for custom rules.
- Results: `{"players": [{"crashed", "comment"}], "results": [scores], "seed"}`.
- Seeds are unsigned 64-bit (stored as text in SQLite).

## Measured on this machine (one 1-core container each, Round1/Round2)

| strategy | CPU per game | peak memory |
|----------|--------------|-------------|
| `dragoonxen` (C++) | 5–10 s | ~40 MB |
| `format37` (Python) | 11–29 s | ~80 MB |
| `thermit` (Java) | 21–25 s | ~560 MB |

A 4-player game takes 30–60 s wall-clock; a 1v1 about 15–25 s.
