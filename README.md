# CodeCraft

A self-hosted competition server for **CodeCraft**, the game of
[Russian AI Cup 2020](https://github.com/MailRuChamps/raic-2020) by Mail.ru Group. The official
platform (cups.online) is gone; the official game engine — the local runner `aicup2020` 1.3.0 — is
not. This server puts it back behind a web platform: players sign in, upload strategies, the server
builds them into Docker images, plays them against each other, keeps an Elo ladder and shows every
replay in the browser. We restored it for ourselves, to sharpen our coding skills.

Live instance: https://codecraft.latentstate.dev

## Features

- **Sign-in by e-mail code** (no passwords): the button in the mail signs you in, a new address is
  asked for a name once (changeable on your page); registration open, by invitation, or closed; admins.
- **Submissions**: zip upload or public git URL, any of the 15 official client languages, optional
  GPU (CUDA) and merge mode. Each submission is a numbered version; a successful build becomes
  the active one. Build logs and your bot's own output are visible only to you.
- **Sandbox**: every new build plays a fixed number of rated games (Round1 / Round2 / Finals,
  rating-aware matchmaking, multiplayer Elo); then the worker rests until the next submission.
- **Custom games**: any format, any seats (including your own older versions), unrated.
- **Replay viewer** in the browser: the official `replay.dat` is decoded
  ([docs/replay-format.md](docs/replay-format.md)); the official 3D viewer works too. A game link pasted
  into Telegram, Slack or Discord shows the final board (Open Graph preview image).
- **Limits as in 2020**: 1 s per tick, 40 s CPU per game, memory and CPU caps per container,
  strategies on an internal network with dropped capabilities.

## For AI agents

Claude Code, Codex, Cursor, Gemini CLI, a claude.ai connector or any MCP client can play with your account:
read the rules, submit strategies, wait for builds, queue games and analyse them (a timeline decoded from the
replay, your bot's own log). Create a token and copy the setup line at
[/agents](https://codecraft.latentstate.dev/agents); the same tools answer at `/api/v1/<tool>` for scripts.

## Run it

Requirements: Docker; the `nvidia` container runtime if you want GPU strategies.

```bash
cp .env.example .env              # CW_ADMINS, CW_BASE_URL, mail provider keys
docker compose build
docker compose run --rm cli build # engine image + seed strategies from strategies/*.yaml
docker compose up -d web worker   # web UI on :8080; the worker builds submissions and plays
docker compose logs -f worker
```

Without a mail provider the sign-in codes are printed in `docker compose logs web`. Set
`CW_RESEND_API_KEY` + `CW_MAIL_FROM` (Resend) or `mail.smtp` in `config.yaml` for real mail.
`CW_REGISTRATION=invite` makes new accounts need an invitation link (admins create them at
`/admin`). `CW_SENTRY_DSN` turns on error reporting to Sentry (unhandled exceptions and ERROR log
lines of the web and the worker; no request bodies, addresses and tokens scrubbed; off when unset). GPU strategies
need `CW_GPU_ENABLED=true` on a host with the `nvidia` docker runtime (the web service has no Docker socket, so
the default `gpu.enabled: auto` cannot see the GPU there); without it the CUDA languages and the GPU option are
simply absent. Behind a reverse proxy, publish the port on the address the proxy uses (`CW_WEB_PORT=ADDRESS:PORT`)
and name the proxy in `CW_TRUSTED_PROXIES`, so the client addresses in sessions and rate limits are real. Everything else lives in
[config.yaml](config.yaml): limits, formats, rating, GPU policy (`gpu.max_per_game`), sandbox
size (`worker.sandbox_games_per_build`), quotas.

CLI (`./cw`, through the compose `cli` service or a local venv):

```bash
./cw play --format Finals --players mybot@2 mybot --unrated   # version 2 vs the active one
./cw submit --name friend --language java --zip friend.zip --owner friend@mail
./cw invite --note "for Ivan"      ./cw users      ./cw admin you@mail
./cw ladder      ./cw games      ./cw replay 15 --json out.json      ./cw watch 15
./cw rename oldname newname      # a strategy's row, versions and past games
```

Tests: `python -m pytest server/tests`.

## How a strategy is built and run

Exactly like the 2020 server: the official client of the language (`clients/<Lang>`) + your
files + the client's `compile.sh`; at game time `run.sh host port token`. One file replaces the
client's strategy file (`my_strategy.py`, `MyStrategy.java`, …); several files replace the whole
client project; merge mode lays your files over the client (for CUDA kernels + `CMakeLists.txt`).
A repository with one folder per language is built from the folder named after the chosen language:
[codecraft-starter](https://github.com/format37/codecraft-starter) has every language ready to submit.
Base images are overridden in `templates/` where the 2020 ones no longer build (C++, Python, C#, F#,
Rust, the GraalVM variants: Debian buster and the old GraalVM image are gone, old cargo and pip choke on
today's registries), plus `cpp-cuda` (nvcc) and `python-cuda` (CuPy / PyTorch) images. Seed strategies in `strategies/` are manifests
for server-managed bots (see [strategies/README.md](strategies/README.md)); the bundled 2020 contest
entries carry a `-2020` suffix and are disabled, so their authors can register under their own names.

## Layout

```
engine/      aicup2020 1.3.0 (the official local runner, closed source) + GUI assets, Dockerfile
clients/     official v1.3.0 clients (GPL-3.0, MailRuChamps/raic-2020)
templates/   base-image overrides per language
strategies/  seed strategy manifests
server/      codecraft package: web, worker, builder, game, replay, auth, db, monitoring; tests; Dockerfile
docs/        official rules (en/ru), replay format, tournament formats, plan (KICKOFF.md)
data/        database, submissions, build cache, games (git-ignored)
```

## Credits and licence

The game, the engine and the clients were made by the Russian AI Cup team (Mail.ru Group; engine by
[kuviman](https://github.com/kuviman)). This server is an independent, non-commercial restoration and is
not affiliated with them.

The server code is [MIT](LICENSE): run it on your own machine, change it, share it. The official
clients stay GPL-3.0 and the engine binary (closed source) is redistributed unchanged from the
2020 local-runner package; both remain the property of their authors (see [LICENSE](LICENSE)).
