# The hub and its executors

One machine can do everything (`docker compose up -d web worker`). Or the work is split in two:

- **the hub**: the site, the MCP server, sign-in, submissions, the database, the ratings and leagues, the replays.
  A small machine that is always on (a VPS). It has no Docker socket and runs no strategy code.
- **executors**: machines with Docker that build strategies and play games. They may be switched off at any time:
  a reboot, a power cut, a workstation that is only sometimes on.

```
            players, agents                      executors (Docker)
                 |                       +---------------------------+
                 v          HTTPS        |  node: games and builds   |
   +------------------------+  <-------  |  (CPU)                    |
   |  hub (VPS)             |   asks     +---------------------------+
   |  site, MCP, database,  |   for      +---------------------------+
   |  ladder, replays       |   work     |  gpu-box: GPU strategies  |
   +------------------------+  <-------  |  only (sometimes on)      |
                                         +---------------------------+
```

The hub never calls an executor. An executor asks it for work with a shared token (`CW_HUB_TOKEN`): the queues,
the strategies and the results are calls to the hub's database (`/internal/rpc`, only the methods the worker
needs), and what a game or a build leaves behind is sent up (`/internal/games/<id>/files/`,
`/internal/versions/<id>/log`). Code: `server/codecraft/hub.py` (the hub's side), `remote.py` (the executor's),
`worker.py` (the same loops either way).

## When an executor goes away

- **Its work goes back to the queue.** An executor that restarts gives back at once what it was doing; one that
  stays silent loses it after `hub.lease_sec` (10 minutes). A custom game or a build is done again from the start;
  a sandbox game, made up on the spot, ends as an error that counts for nobody. A late result from a machine that
  lost its game is refused.
- **The site goes on.** The ladder, the games played and their replays, submissions and new games (they wait in
  the queue). Every page shows a banner while no executor has been heard of for `hub.offline_sec` (2 minutes), and
  the MCP tools `whoami`, `version` and `game` say so too.
- **Before a planned reboot**, stop the worker so that running games finish first:
  `docker compose stop worker` (it waits up to 30 minutes, takes no new work, and its last heartbeat says it is
  stopping: the banner shows at once). Then reboot; the worker comes back with Docker (`restart: unless-stopped`).

## Replays

The executor that played a game keeps its replay for good, compressed (`replay.dat.xz`, about a fifteenth of the
engine's file). The hub keeps a copy `hub.replay_days` (30 days); after that only the game's results, logs and
preview image stay there. When somebody opens an older replay, the hub asks that executor for it (its next
heartbeat, at most 15 seconds) and the viewer reloads itself until it is there; if the executor is off, it waits
for it to come back. A replay fetched back stays another `replay_days`.

## GPU

A version or a game that needs a GPU waits for an executor with one (`gpu: true` in its heartbeat; Docker's nvidia
runtime). An executor kept for the GPU takes only that work: `CW_WORKER_SANDBOX=gpu` (it starts only sandbox games
of GPU strategies; the other seats are anybody's) and `CW_WORKER_BUILDS=gpu`. When GPU work has waited
`hub.gpu_alert_minutes` (10) with no GPU executor online, the hub sends a Telegram message (`CW_TELEGRAM_TOKEN`,
`CW_TELEGRAM_CHAT`), again every 6 hours while it waits. The hub says `CW_GPU_ENABLED=true`, so that the site
offers the CUDA languages.

Images: an executor that has to play a strategy built elsewhere builds it again from the same sources (the
uploaded archive from the hub, or the very commit) and tags it with the name the ladder knows the build by.

## Setting it up

The hub (on the VPS, behind the reverse proxy):

```bash
git clone https://github.com/format37/codecraft && cd codecraft
cp .env.example .env     # CW_BASE_URL, CW_ADMINS, mail, Sentry; CW_HUB_TOKEN=$(openssl rand -hex 32);
                         # CW_TRUSTED_PROXIES=<the proxy's network>, CW_GPU_ENABLED=true, CW_TELEGRAM_*
docker compose -f compose.hub.yml build
docker compose -f compose.hub.yml up -d      # the web joins the proxy's network (CW_PROXY_NETWORK)
```

and the proxy sends the site to `codecraft-web-1:8080`. An executor:

```bash
git clone https://github.com/format37/codecraft && cd codecraft
cp .env.example .env     # CW_HUB_URL=https://<the site>, CW_HUB_TOKEN, CW_EXECUTOR_NAME, CW_DOCKER_RUNTIME, DOCKER_GID
docker compose build
docker compose run --rm cli build          # the engine and the toolchains (seed images are rebuilt on demand too)
docker compose up -d worker
```

A second worker on the same Docker (a GPU executor next to a development stack) takes its own names:
`CW_DOCKER_PREFIX`, `CW_DOCKER_LABEL`, `CW_SEAT_SUBNETS`, and its own compose project (`docker compose -p ...`) in
its own clone, so that its `data/` is its own.

Admins see every executor, its GPU and when it was last heard of on `/admin`.
