# CodeCraft

A self-hosted competition server for **CodeCraft**, a real-time strategy game played by programs, from a 2020 AI
programming championship (see [Credits](#credits-and-licence)). The official platform is gone, and the official
engine survives only as a closed binary, the local runner `aicup2020` 1.3.0. This server puts the game back behind
a web platform, on an open-source engine of our own: players sign in, upload strategies, the server builds them
into Docker images, plays them against each other, keeps a rated ladder in leagues and shows every replay in the
browser. We restored it for ourselves, to sharpen our coding skills.

Our engine (`sim-cpp/` in C++, which the server runs, and `sim/` in Python, its executable spec, which plays the
same games) has the official rules, the official engine's command line, protocol and replay format, so the 15
official clients play it unchanged. One rule is improved on purpose: under fog of war a path search remembers the
resources and buildings its player has seen ([docs/arena.md](docs/arena.md#the-engine)); `--rules 2020` plays the
official rules exactly. It is written from the rules and tested against the official binary, which this
repository does not include ([sim/README.md](sim/README.md) says how to run those tests with it).
`config.yaml` picks the engine. See [docs/KICKOFF.md](docs/KICKOFF.md).

Live instance: https://codecraft.day

## Features

- **Sign-in by e-mail code** (no passwords): the button in the mail signs you in, a new address is
  asked for a name once (changeable on your page); registration open, by invitation, or closed; admins.
- **Submissions**: zip upload or public git URL, any of the 15 official client languages, optional
  GPU (CUDA) and merge mode. Each submission is a numbered version; a successful build becomes
  the active one. Build logs and your bot's own output are visible only to you.
- **Sandbox**: every new build plays its placement games (Round1 / Round2 / Finals, rating-aware
  matchmaking), then a rated game an hour, less often as the build ages, so the ladder never stands
  still. The ratings are one fit over every rated game, on the Elo scale, each with its standard error.
  No rounds, no deadlines; leagues by strength, clustered from the ratings once a week
  ([docs/arena.md](docs/arena.md), rendered at `/rules` with the numbers of `config.yaml`).
- **Custom games**: any format, any seats (including your own older versions), unrated.
- **Replay viewer** in the browser, the local runner's own: the official `replay.dat` is decoded
  ([docs/replay-format.md](docs/replay-format.md)). A game link pasted into Telegram, Slack or Discord shows
  the final board (Open Graph preview image).
- **Limits as in 2020**: 1 s per tick, 40 s CPU per game, memory and CPU caps per container. The live
  instance runs strategies under gVisor, where every system call costs extra CPU: the Python, Ruby and
  JavaScript starters spend 10-25% more, but the official D client reads each value of the game state with
  its own `socket.receive`, so a D strategy spends about 3.5 times as much (the D starter: 19 s of its 40).
  A buffered `SocketStream` (`source/app.d`) should save most of that.
- **Isolation**: a submission compiles offline, in a container with memory, CPU, process and time
  limits and no capabilities, made from a base image per language that holds the official client and
  its dependencies (so a build can use what that image has, as in 2020). A playing strategy sits on a
  network of its own that it shares only with the engine (no internet, no other seat, not the host),
  on a read-only filesystem with a 256 MB `/tmp`. Git submissions are public https repositories on
  github.com, gitlab.com, bitbucket.org or codeberg.org, fetched one commit deep with a size cap;
  symbolic links are refused. Not capped: what a compile writes to disk (no per-container quota on
  ext4); its time and process limits bound it. With [gVisor](https://gvisor.dev) installed (`runsc`,
  registered in `/etc/docker/daemon.json` with `"runtimeArgs": ["--overlay2=none"]`, so that a compiled
  strategy can be committed as its image), `CW_DOCKER_RUNTIME=runsc` runs every strategy and every compile
  on its user-space kernel instead of the host's; GPU strategies keep the default runtime.

## For AI agents

Claude Code, Codex, Cursor, Gemini CLI, a claude.ai connector or any MCP client can play with your account:
read the rules, submit strategies, wait for builds, queue games and analyse them (a timeline decoded from the
replay, your bot's own log). Create a token and copy the setup line at
[/agents](https://codecraft.day/agents); the same tools answer at `/api/v1/<tool>` for scripts.

## Run it

Requirements: Docker; the `nvidia` container runtime if you want GPU strategies.

```bash
cp .env.example .env              # CW_ADMINS, CW_BASE_URL, mail provider keys
docker compose build
docker compose run --rm cli build # engine, base image per language (~40 min the first time), seeds
docker compose up -d web worker   # web UI on :8080; the worker builds submissions and plays
docker compose logs -f worker
```

Without a mail provider the sign-in codes are printed in `docker compose logs web`. Set
`CW_RESEND_API_KEY` + `CW_MAIL_FROM` (Resend) or `mail.smtp` in `config.yaml` for real mail.
`CW_REGISTRATION=invite` makes new accounts need an invitation link (admins create them at
`/admin`). `CW_SENTRY_DSN` turns on error reporting to Sentry (unhandled exceptions and ERROR log
lines of the web and the worker, but not a coding agent's refused call; no request bodies, addresses and
tokens scrubbed; off when unset). GPU strategies
need `CW_GPU_ENABLED=true` on a host with the `nvidia` docker runtime (the web service has no Docker socket, so
the default `gpu.enabled: auto` cannot see the GPU there); without it the CUDA languages and the GPU option are
simply absent. Behind a reverse proxy, publish the port on the address the proxy uses (`CW_WEB_PORT=ADDRESS:PORT`)
and name the proxy in `CW_TRUSTED_PROXIES`, so the client addresses in sessions and rate limits are real. If that
address can come up after Docker starts (a WireGuard or other VPN interface), set `net.ipv4.ip_nonlocal_bind=1` on
the host (a file in `/etc/sysctl.d`): Docker cannot publish a port on an address that does not exist yet, and
after a reboot the web service would run without its port. Everything else lives in
[config.yaml](config.yaml): limits, formats, rating, GPU policy (`gpu.max_per_game`), sandbox
rhythm (`worker.sandbox_games_per_build`, `worker.sandbox_interval_hours`), leagues, quotas.

The site and the games can also live on different machines: the site and the database on a small always-on host
(the hub, `compose.hub.yml`), the building and playing on machines with Docker that may be switched off at any time
(executors, `docker compose up -d worker` with `CW_HUB_URL`), a GPU one among them. See [docs/hub.md](docs/hub.md).

CLI (`./cw`, through the compose `cli` service or a local venv):

```bash
./cw play --format Finals --players mybot@2 mybot --unrated   # version 2 vs the active one
./cw submit --name friend --language java --zip friend.zip --owner friend@mail
./cw invite --note "for Ivan"      ./cw users      ./cw admin you@mail
./cw ladder      ./cw games      ./cw replay 15 --json out.json
./cw rerate --history            # fit the ratings anew (after a change of `rating` in config.yaml)
./cw leagues --redraw            # draw the lines between the leagues now, not on Monday
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

## Test a strategy on your machine

The engine is also a local runner, with the game live in the browser and the official GUI's debug interface (your
strategy draws lines, triangles, text and logs, and reads the mouse and the keys). It is published for everyone by
`.github/workflows/runner.yml`: the image `ghcr.io/format37/codecraft-runner` (linux/amd64 and arm64) and Linux,
macOS and Windows binaries on [GitHub Releases](https://github.com/format37/codecraft/releases), one release per
engine version.
The guide, with the commands of every language, is <https://codecraft.day/quickstart>.

```bash
docker run --rm -it -p 31000:31000 -p 31001-31004:31001-31004 -v "$PWD":/game ghcr.io/format37/codecraft-runner
# or a binary (also linux-arm64, macos, and windows-x86_64.zip with codecraft-runner.exe):
curl -L https://github.com/format37/codecraft/releases/latest/download/codecraft-runner-linux-x86_64.tar.gz | tar xz && ./codecraft-runner
# or from source:
make -C sim-cpp && sim-cpp/build/codecraft-sim-cpp
```

It plays the server's games (the same engine, rules and maps): `--version` shows the version of its source, each
game page names the engine it was played on, and `ghcr.io/format37/codecraft-runner:<version>` is that very engine.
Pause, step, rewind, zoom, inspect entities, save a replay (Ctrl-S) or the state of a tick (Ctrl-E), restart the
game for your changed strategy without restarting the runner, `--replay replay.dat`, the server's limits held or only
warned about (`--limits warn`, for breakpoints): [sim-cpp/README.md](sim-cpp/README.md#the-local-runner).

## Layout

```
sim-cpp/     our own engine in C++, the one the server runs: a game in seconds; the local runner and its viewer;
             Dockerfile
sim/         the same engine in Python, its executable spec (the same games, seed for seed); the oracle tools and
             fixtures that test it against the official binary; Dockerfile
clients/     official v1.3.0 clients (GPL-3.0)
templates/   base-image overrides per language
strategies/  seed strategy manifests
server/      codecraft package: web, worker, builder, game, replay, auth, db, monitoring; tests; Dockerfile
docs/        the arena's rules (arena.md), the game's official rules and changelog, replay format, 2020 tournament
             formats, plan (KICKOFF.md)
data/        database, submissions, build cache, games (git-ignored)
```

## Credits and licence

The game, its rules, the official engine and the clients were made by the organisers of the 2020 championship:
[the original repository](https://github.com/MailRuChamps/raic-2020) (engine by
[kuviman](https://github.com/kuviman)). This server is an independent, non-commercial restoration and is not
affiliated with them. `sim/` and `sim-cpp/` are our own re-implementation of the engine, written from the
published rules and the official binary's observed behaviour (its pathfinding was worked out from its machine
code); they contain none of its code.

The server code is [MIT](LICENSE): run it on your own machine, change it, share it. The official clients stay
GPL-3.0 and, like the official rules in `docs/`, remain the property of their authors (see [LICENSE](LICENSE)).
