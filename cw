#!/usr/bin/env bash
# CodeCraft CLI launcher. Uses server/.venv when present (dev), otherwise the docker image.
set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -z "$CW_DOCKER" ] && [ -x "$ROOT/server/.venv/bin/python" ]; then
    export CW_ROOT="$ROOT" PYTHONPATH="$ROOT/server"
    exec "$ROOT/server/.venv/bin/python" -m codecraft "$@"
fi
exec docker compose -f "$ROOT/docker-compose.yml" run --rm --no-deps cli "$@"
