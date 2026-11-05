# CodeWars platform — kickoff (2026-11-05)

Entry point for the next working session. Read this, then `README.md`, then `config.yaml`.

## Vision

A self-hosted competition platform, first for CodeCraft 2020 (Russian AI Cup 2020), later for
Alex's own game(s) with other mechanics and player counts:

- players register, upload strategies, the server builds them and plays them against each other
  in a loop (sandbox, Elo rating), plus tournaments in the official formats and custom games;
- a web replay viewer, leaderboard, game history, build logs;
- GPU for strategies, with a sharing policy;
- runs on a dedicated machine in Alex's flat, published through a VPN to a VPS at
  `latentstate.dev`; first used privately with friends to shake out the rough edges;
- a Kaggle-style competition is a later idea (see "Kaggle" below).

## Where we are (phase 0, done)

`/home/alex/projects/codewars` (git initialised, **nothing committed yet**):

- `engine/` — official local runner `aicup2020` 1.3.0 (closed Rust binary, the very engine the
  2020 server used; confirmed by its author @kuviman). Used headless via `--batch-mode`.
  Max 4 players, formats Round1/Round2 (4p) and Finals (1v1), 1000 ticks, 80x80.
- `server/codewars/` — orchestrator (Python 3.10+, docker SDK): builds strategies from official
  clients + sources (`builder.py`), runs a game in an isolated docker network with resource limits
  (`game.py`), sandbox loop with rating-aware matchmaking (`loop.py`, `matchmaking.py`),
  multiplayer Elo + tournament points (`rating.py`), SQLite (`db.py`), minimal FastAPI UI
  (`web.py`), CLI `./cw` (`cli.py`). 16 unit tests. `docker-compose.yml` runs `web` and `loop`.
- Strategies: `format37` (Python), `dragoonxen` (C++), `thermit` (Java), `quickstart` (engine bot),
  CUDA demos `cuda-demo-cpp` (nvcc kernel) and `cuda-demo-py` (CuPy RawKernel) — both verified on
  the RTX 4090. 15 test games played; ratings reset afterwards.
- Limits are configurable (`config.yaml` → `limits`, per-strategy overrides in `strategy.yaml`).
  Official 2020 values: 1 s/tick real time, 40 s CPU/game, 256 MB (we default to 1024 MB because
  a docker cgroup counts the whole container; ThermIt's JVM peaks at ~560 MB).

Facts worth remembering: the engine `chdir`s to its own directory (absolute paths only); engine
seeds are unsigned 64-bit (stored as text); `timeout` in the Tcp player config is per tick;
`compile.sh` of the official clients wipes `/project` when the source has >1 file (we keep `run.sh`
outside); `{"Empty": null}` fills a seat; 5 players crash the engine; `Custom` game options exist
(`map_size`, `initial_resource`, `max_tick_count`, `fog_of_war`, …).

## Phase 1 — platform core (next)

1. **Data model** (SQLite stays until it hurts; add a tiny migration mechanism, `db._migrate`
   already exists): `users` (id, login, password hash, role, invite), `strategies` (owner, name,
   language, gpu, enabled, limits), `strategy_versions` (strategy, number, source kind
   zip/git, build status queued/building/ok/failed, image, build log path, created, accepted),
   `games` + `game_players` (exist; add `kind` custom/sandbox/tournament, `created_by`),
   `rating_history`, `tournaments`/`rounds`/`waves` (phase 2).
2. **Auth**: invite-code registration + password (argon2 via `pwdlib`), signed session cookie,
   roles user/admin. GitHub OAuth is optional later.
3. **Submission pipeline**: upload a zip (or a git URL + ref) + language + flags → `strategy_versions`
   row → a **worker** service builds it with `Builder` (queue in the DB, one builder at a time),
   stores the log, marks the version accepted → the newest accepted version plays in the sandbox
   (like the 2020 sandbox: "last accepted strategy"). Users see build status/logs on their page.
   Keep the directory-based `strategies/*.yaml` as the admin/seed path.
4. **Services**: `web` (FastAPI + Jinja, sessions), `worker` (build queue + sandbox loop + custom
   game queue), optional `viewer` static assets. Both share `data/` and the docker socket.
5. **GPU policy**: `gpu.max_per_game` (default 1) enforced in matchmaking and custom-game
   validation → one CUDA strategy gets the whole GPU. Later: fractions by player count
   (needs CUDA MPS `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` or time slicing; docker cannot split a
   consumer GPU by itself).
6. **Custom games**: a user picks a format and opponents (any accepted versions, own older
   versions too) → queued → played by the worker → page with results and replay.
7. **Web replay viewer**: 2D canvas, 80x80 grid, entities by type/owner, health, scores per tick,
   scrubber/play/speed, click an entity to see its action. Data source — decide (see
   `docs/replay-format.md`): (a) decode the official `replay.dat` (exact god view, needs reverse
   engineering of the diff encoding) or (b) a TCP proxy in `game.py` that records every
   `PlayerView` and `Action` (engine-agnostic, also gives per-player actions for debugging; union
   of views in fog games). Recommendation: (b) first, (a) as a stretch goal.
8. **UI pages**: home/leaderboard, games (filters), game (viewer, per-player table, logs for the
   owner), strategy (versions, builds, history), profile, submit, admin (users, invites, enable/
   disable strategies, limits, queue).
9. **Hardening for a shared server**: strategy containers already run on an internal network with
   limits; add `read_only` rootfs + tmpfs where the clients allow, `no-new-privileges`, default
   seccomp, upload size limits, per-user submission rate limits, build timeouts. Note the worker
   talks to the docker socket (root-equivalent on its host) — the dedicated machine isolates that.

## Phase 2 — tournaments

Official formats as scheduled events: Round1/Round2 waves where every strategy plays exactly once
per wave, Finals round-robin; points 8/4/2/1 and 2/0 (already in `rating.py`); freeze submissions
during a round; round pages with standings.

## Phase 3 — deployment

Dedicated machine (GPU) at home → WireGuard to the VPS → Caddy on the VPS proxies
`codewars.latentstate.dev` (TLS) to the tunnel. `docker compose` with restart policies, nightly
backup of `data/` (SQLite + replays), log rotation, monitoring (at least uptime). The `ovh` skill
in `~/.claude/skills` knows the VPS/Caddy setup.

## Phase 4 — other game mechanics

Introduce a `GameKind` abstraction: engine image + command, seats per format, config generator,
results parser, replay recorder/decoder. CodeCraft becomes one kind; Alex's own game (2D RTS with
Beyond-All-Reason-style metal/energy economy) another, with its own engine (any language — only
the orchestrator contract matters: TCP players, results file, replay).

## Kaggle

A Kaggle "simulation competition" (e.g. Lux AI, Halite) needs the game as a `kaggle-environments`
Python environment with an interpreter running inside Kaggle's episodes; the closed Rust engine
cannot be used there. So Kaggle = own engine (phase 4) ported to that interface, plus public
replays. Not before the private server has been used for a while.

## Open decisions (ask Alex at the start of the next session)

1. Auth: invite + password (recommended for friends) or GitHub OAuth?
2. Submission: zip upload, git URL, or both? Which languages to expose (all 15 clients work)?
3. Replay viewer data source: proxy recording first (recommended) or decode the official format?
4. Keep SQLite (fine for tens of users) or move to Postgres now?
5. Commit the current state before starting (recommended: yes — nothing is committed).

## Conventions

- `./cw` is the CLI (venv at `server/.venv`, or `docker compose run --rm cli`). Tests:
  `server/.venv/bin/python -m pytest server/tests`.
- Absolute paths for the engine; game artefacts live in `data/games/<id>/`; `data/` is git-ignored.
- No commits without Alex asking. Alex writes RU/EN interchangeably; code and docs are in English.
