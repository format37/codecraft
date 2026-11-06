# CodeCraft platform — kickoff and status (updated 2026-11-06)

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

Delivered (`server/codecraft/`):

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
- `cli.py` — `worker` (alias `loop`), `submit`, `users`, `admin`, `invite`, `replay`, `rename`, plus the
  old commands. `monitoring.py` — Sentry, on when `CW_SENTRY_DSN` is set (errors only, no request bodies,
  addresses and tokens scrubbed), inited in `cli._context` so `web` and `worker` both report. `docker-compose.yml`: services `web`, `worker`, `cli`; `.env` for secrets.
- 47 tests (`test_web.py` drives the app through FastAPI's TestClient). Verified live: sign-in with a console code, zip submission → built by the worker →
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

### Leagues (Alex's idea, 2026-11-06)

Players range from frontier-LLM strategies to hand-written code and small local models, and all of
these are welcome. To give each level a race of its own, split the ladder into leagues by strength
rather than by what people say they use, since that cannot be checked. Once there are at least 40
players with settled ratings, cluster the ratings once a week. Try k = 2..K and keep the k whose
split is clearest (1-D optimal k-means with silhouette, or a Gaussian mixture chosen by BIC). League 0
is the strongest. Each league has its own standings and its own Phase 2 tournament, where promotion
and relegation happen. Sandbox matchmaking can stay global.

Agreed with Alex:

- A league member is a user, ranked by their best strategy.
- A league has at least 4 players, so it can fill a 4-seat Round 1 game; 8 for a full tournament.
- Only settled ratings count, including towards the 40. 10 sandbox games per build give a noisy Elo.
- Boundaries have hysteresis, so a player sitting on a break does not change league every week.
- Against sandbagging (a deliberately weak version submitted to drop into a lower league), players
  are placed by their best rating over the week, not by their latest version.
- When the ratings have no real gaps, the chosen k can jump from week to week. Fall back to the
  previous week's k, or to quantiles.

## Phase 3 — deployment (done 2026-11-05)

Renamed from CodeWars on 2026-11-05 (repo, domain, package, images, DB file, Resend domain, Sentry
project); codewars.latentstate.dev 301-redirects here and github.com/format37/codewars redirects to
the new repo.

Live at https://codecraft.latentstate.dev: the home node (8 cores, 14 GB, docker, **no NVIDIA GPU**
for now — the GPU is not attached because of the machine's power budget, so `gpu.enabled: auto`
resolves to false and the CUDA languages are hidden; the RTX 4090 stays on the dev machine) runs
`web` + `worker` from a clone of the public repo; the web port is published on the node's WireGuard
address only (since 2026-11-06; before, to the home LAN) and reached through that tunnel by the VPS Caddy
(`codecraft.latentstate.dev` site block, auto-TLS, Cloudflare grey-cloud A record), whose address is the one
trusted proxy (`CW_TRUSTED_PROXIES`). `.env` on the node: `CW_WEB_PORT`,
`CW_BASE_URL=https://codecraft.latentstate.dev`, `CW_ADMINS`, `CW_REGISTRATION=open` (invite-only
during the first hours, open since 2026-11-05 evening), the Resend key + `CW_MAIL_FROM` (domain
`codecraft.latentstate.dev` verified in Resend), `DOCKER_GID` of the node, `CW_SENTRY_DSN` (Sentry
org personal-htg, project `codecraft`, since 2026-11-05). Deploy a change: push to GitHub, on the node `git pull && docker compose build && docker
compose up -d --force-recreate web worker`. The image holds only the requirements and the code is mounted from the checkout, so a plain
`up -d` leaves a code-only change unloaded; the worker finishes its running games before it stops (new seed strategies: `docker compose run
--rm cli build`).
Machine-specific facts (addresses, helper scripts, tokens) live in the private ops notes, not here.
Later: nightly backup of `data/` (SQLite + replays), log rotation, uptime check.

Other changes this round: cover page at `/` (featured replay autoplay, the 2020 story), rules page,
bounded sandbox (`worker.sandbox_games_per_build`, default 10: the worker idles once every build
has played its share), README rewritten for a public repository.

Opening up (2026-11-05, after Alex's first look): registration open; the mail button signs in at
once and only a new address is asked for a name (changeable at `/me`); the 2020 entries used for
testing were renamed `format37-2020`, `dragoonxen-2020`, `thermit-2020` (+ `format37-2020-cuda`) and
disabled, so the authors can take their names when they register (Alex plays from a non-admin
account). Seeds' `enabled` follows the manifest at every worker start, so re-enabling a sparring
partner means editing its `strategy.yaml`, not only the admin page. With no enabled seed but
QuickStart, the first user strategy plays its sandbox games against QuickStart fillers.

Starter (2026-11-05 evening): github.com/format37/codecraft-starter, one folder per language (the official
client + the same ~100-line strategy, ported to all 15 languages and built through the real pipeline);
the builder takes the folder named after the chosen language when a submission's root holds only docs
(`builder.language_subdir`, `python-pypy` -> `python/`). Linked from the submit page and the README.
Building all 15 languages through the pipeline exposed more 2020 base-image rot, fixed with template
overrides: csharp/fsharp (debian:buster repositories gone -> `mcr.microsoft.com/dotnet/sdk:5.0`), rust
(rust:1.48 cargo cannot read edition-2021 crates -> `rust:1.82`), java/kotlin-graalvm-native
(oracle/graalvm-ce gone from Docker Hub -> `ghcr.io/graalvm/graalvm-ce:ol8-java11-22.3.3` + Maven 3.6.3;
the 20.2.0 native-image plugin still works). Every port was checked for equivalence with a seed-fixed
Round1 game of four copies of itself: identical score vectors across all languages.

## Phase 3b — coding agents (done 2026-11-05)

A coding agent works with a user's API token: MCP at `/mcp/` (streamable HTTP, stateless, JSON responses;
token as `/mcp/<token>/` or `Authorization: Bearer`) and the same 13 tools as `GET`/`POST /api/v1/<tool>`.
Tools: `whoami`, `rules`, `leaderboard`, `strategy`, `version` and `game` (both with `wait_s` up to 50 s,
polled on the event loop), `games`, `game_timeline`, `game_log`, `upload_link` (a 15-minute ticket for
`curl -T strategy.zip`), `submit_strategy`, `play` (unrated custom game), `set_strategy`. Everything runs
inside the `web` service: `agents.py` (tokens, uploads, tool functions, the custom-game rules shared with
the form), `mcp_server.py` (FastMCP wrappers and the agent-facing texts), the `/agents` page (tokens, setup
lines per client, a first prompt). Tokens: `cc_` + 40 characters, sha256 in `api_tokens`, 5 per user,
120 requests a minute each; a token never acts as an admin when it writes. `replay.timeline()` samples
score, resource, population, units and buildings per seat and counts built / lost / killed between samples;
kills are attributed by replaying each tick's attack events against the health before the tick (the
first hit that takes a target to zero), which matches the engine's destroy score. Tokens and upload
tickets are masked in the access log and in Sentry events. `mcp==1.29.0`.

## Phase 3c — hardening (done 2026-11-06)

A review of what uploaded code and the public site can reach on the home node, then two deployed batches
(commit bb087c4, then the commit that adds this section):

- **Builds**: a strategy compiles in a container made from its language's base image
  (`codecraft/base-<language>:<hash>`: the template or client Dockerfile with the official client, built once
  with the network; `cw build` warms all of them), with no network, `build.memory_mb` / `cpus` / `pids`, no
  capabilities, `no-new-privileges` and a hard `build.timeout_sec`; the container is committed as
  `codecraft/strategy-<name>:<hash>` (the hash covers the base image, the sources, merge mode and a seed's
  prebuild). The base images keep Maven offline; the starter builds offline in every language. The build log
  keeps its last `build.log_max_mb`.
- **Git**: https on `submissions.git_hosts` only, fetched one commit deep (a short commit id falls back to the
  whole branches), each file git writes capped at 1.5 x `max_unpacked_mb`, no redirects, a minimal
  environment; symbolic links are checked out as plain files and refused by name, and git refs cannot start
  with `-`.
- **Games**: every TCP seat has an internal network of its own (a /24 of `docker.seat_subnets`, the host
  without an address on it), the engine joins all of them at fixed addresses and listens for each seat on
  that seat's network only; players run read-only with a 256 MB `/tmp`, and docker keeps the last 10 MB of a
  player's or the engine's output. The game folder's `config.json` has the seat tokens masked.
- **Web**: no Docker socket in `web` (a GPU host sets `CW_GPU_ENABLED=true`), the code mounted read-only and
  only `data/` writable, the port published on one address (`CW_WEB_PORT=ADDRESS:PORT`), `X-Forwarded-For`
  honoured from `web.trusted_proxies` only, `game_log` grep as plain text, sign-in `next` kept on the site, at
  most 20 sign-in codes a day per address, nosniff / frame / referrer / HSTS headers.

The node moved to an LTS release the same day. Its first reboot showed that the web port, published on the
WireGuard address, needs `net.ipv4.ip_nonlocal_bind=1` on the host: the interface comes up after Docker, and
Docker cannot publish a port on an address that does not exist yet, so the web service had started without it.

gVisor runs every strategy and every compile on the node since the same day (`CW_DOCKER_RUNTIME=runsc`, commit
0cc931d): the starter in all 15 languages compiles offline under it and plays the seed-7 game to the runc
scores, the probe reaches nothing, and the price is CPU time (about +10-25% for most starters, x3.5 for D's).
Players now dial the engine at its address on the seat network, since gVisor's network stack does not reach
Docker's embedded DNS.

Still open: quotas per person rather than per account; pruning old strategy and base images.

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
