# CodeWars platform — kickoff and status (updated 2026-11-05)

Entry point for a working session. Read this, then `README.md`, then `config.yaml`.

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

## Phase 0 — engine orchestration (done, commit 09e904e)

`engine/` official local runner `aicup2020` 1.3.0 (closed Rust binary = the 2020 server engine,
confirmed by its author @kuviman), headless `--batch-mode`. Builder (official clients + sources),
isolated per-game docker networks with resource limits, Elo + points, CUDA templates verified on
the RTX 4090. Facts: the engine `chdir`s to its own directory (absolute paths); seeds are unsigned
64-bit; `timeout` in the Tcp player config is per tick; `compile.sh` wipes `/project` when the source
has >1 file (we keep `run.sh` outside); `{"Empty": null}` fills a seat; 5 players crash the engine;
`Custom` game options exist.

## Phase 1 — platform core (done, this session)

Decisions taken with Alex: e-mail code sign-in like Phonyx (no passwords); zip + git submissions;
replay viewer **decodes the official `replay.dat`** (format fully reverse-engineered, see
`docs/replay-format.md`); SQLite stays.

Delivered (`server/codewars/`):

- `db.py` — users, login_codes, sessions, invites, strategies (owner, kind seed/user, active version,
  limits), strategy_versions (build queue), games (kind, rated, queued status), game_players
  (version, label). Additive migrations for the v0.1 database.
- `auth.py` — codes (hashed, 10 min, 5 attempts), sessions in the DB, rate limits, registration
  open/invite/closed, admins from config/`CW_ADMINS`; mail via Resend HTTP API / SMTP / console.
- `submissions.py` — zip (safe extraction, single-dir unwrap, limits) or public git URL; quotas.
- `worker.py` — one process: builder thread (queue of versions, per-version logs, build timeout,
  success = active version) + game threads (queued custom games first, then rated sandbox games);
  GPU detection and the `gpu.max_per_game` cap in matchmaking.
- `game.py` — `PlayerSpec` seats (strategy + image + version + label), pre-created queued games,
  unrated games, container hardening (no-new-privileges, cap-drop ALL).
- `replay.py` — decoder + JSON export; `web.py` + templates + `static/viewer.js` — leaderboard, games
  with filters, game page with the canvas viewer, strategy page (versions, activate, pause), submit,
  custom game form, login, profile, admin (users, invites, queues, limits).
- `preview.py` — Open Graph link previews: the field + scoreboard as a 1200x630 PNG (`/games/<id>/preview.png`,
  `?tick=` keyframes), cached next to `replay.dat`, pre-rendered by the worker; `og:` tags on every page.
- `cli.py` — `worker` (alias `loop`), `submit`, `users`, `admin`, `invite`, `replay`, plus the old
  commands. `docker-compose.yml`: services `web`, `worker`, `cli`; `.env` for secrets.
- 36 tests. Verified live: sign-in with a console code, zip submission → built by the worker →
  custom game queued from the web → played; the viewer renders game 15 in Chrome.

Known gaps / small follow-ups:

- Mail is not configured yet (console provider): put `CW_RESEND_API_KEY` + `CW_MAIL_FROM` (a verified
  domain) or `mail.smtp` into `.env`/`config.yaml` before inviting friends.
- Rate limiters and sessions are per process; fine for one `web` replica.
- Admin "retry build" is only a POST endpoint (no button yet); the admin page could show build logs
  and per-user quotas.
- The viewer shows the god view also for fog games (no per-player fog rendering).
- No per-user e-mail notifications (build failed, game finished).
- `cuda-demo-*` seeds are disabled; enabling them needs `gpu.max_per_game` ≥ 1 (default) — matchmaking
  already keeps one GPU strategy per game.

## Phase 2 — tournaments (next)

Official formats as scheduled events: Round1/Round2 waves where every strategy plays exactly once
per wave, Finals round-robin; points 8/4/2/1 and 2/0 (already in `rating.py`); freeze submissions
during a round; round pages with standings. Tables `tournaments`/`rounds`/`waves`; game kind
`tournament` already exists in the schema.

## Phase 3 — deployment (done 2026-11-05)

Live at https://codewars.latentstate.dev: the home node (8 cores, 14 GB, docker, **no NVIDIA GPU**
for now — the GPU is not attached because of the machine's power budget, so `gpu.enabled: auto`
resolves to false and the CUDA languages are hidden; the RTX 4090 stays on the dev machine) runs
`web` + `worker` from a clone of the public repo; the web port is published to the home LAN only
and reached through the existing WireGuard tunnel by the VPS Caddy (`codewars.latentstate.dev`
site block, auto-TLS, Cloudflare grey-cloud A record). `.env` on the node: `CW_WEB_PORT`,
`CW_BASE_URL=https://codewars.latentstate.dev`, `CW_ADMINS`, `CW_REGISTRATION=invite`, the Resend
key + `CW_MAIL_FROM` (domain `codewars.latentstate.dev` verified in Resend), `DOCKER_GID` of the
node. Deploy a change: push to GitHub, on the node `git pull && docker compose build && docker
compose up -d web worker` (new seed strategies: `docker compose run --rm cli build`).
Machine-specific facts (addresses, helper scripts, tokens) live in the private ops notes, not here.
Later: nightly backup of `data/` (SQLite + replays), log rotation, uptime check.

Other changes this round: cover page at `/` (featured replay autoplay, the 2020 story), rules page,
bounded sandbox (`worker.sandbox_games_per_build`, default 10: the worker idles once every build
has played its share), README rewritten for a public repository.

## Phase 4 — other game mechanics

Introduce a `GameKind` abstraction: engine image + command, seats per format, config generator,
results parser, replay decoder/viewer. CodeCraft becomes one kind; Alex's own game (2D RTS with
Beyond-All-Reason-style metal/energy economy) another, with its own engine (any language — only
the orchestrator contract matters: TCP players, results file, replay). GPU fractions per player
(CUDA MPS / time slicing) belong here too.

## Kaggle

A Kaggle "simulation competition" (e.g. Lux AI, Halite) needs the game as a `kaggle-environments`
Python environment with an interpreter running inside Kaggle's episodes; the closed Rust engine
cannot be used there. So Kaggle = own engine (phase 4) ported to that interface, plus public
replays. Not before the private server has been used for a while.

## Conventions

- `./cw` is the CLI (venv at `server/.venv`, or `docker compose run --rm cli`). Tests:
  `server/.venv/bin/python -m pytest server/tests`.
- Absolute paths for the engine; game artefacts live in `data/games/<id>/`; `data/` and `.env` are
  git-ignored.
- No commits without Alex asking. Alex writes RU/EN interchangeably; code and docs are in English.
