#!/usr/bin/env bash
# The game every runner binary plays before a release (.github/workflows/runner.yml), from the repository's root:
# Round 2 (fog of war), seed 1; seat 1 is the starter's python/ folder over TCP (clients/Python with
# sim/tests/starter/my_strategy.py), the other three the engine's built-in bot. It is played twice, in batch mode as
# the server plays and in the viewer (GUI mode: the debug interface on, the page served, the Host check asked), and
# both must give the same replay and results. The release then compares them across every binary. Ports 31100 (the
# viewer) and 31101 (the seat), so that a runner open on its usual ports does not stand in the way.
#   .github/runner-game.sh BINARY OUTDIR [PYTHON]       (writes OUTDIR/replay.dat and OUTDIR/results.json)
set -euo pipefail
bin=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
out=$2
py=${3:-python3}
root=$(pwd)

rm -rf "$out"
mkdir -p "$out/client"
cp -R "$root/clients/Python/." "$out/client/"
cp "$root/sim/tests/starter/my_strategy.py" "$out/client/my_strategy.py"
cd "$out"
cat > config.json <<'EOF'
{"seed": 1, "game": {"Create": "Round2"},
 "players": [{"Tcp": {"host": "127.0.0.1", "port": 31101, "accept_timeout": 120, "timeout": null, "token": null}},
             "QuickStart", "QuickStart", "QuickStart"]}
EOF
trap 'kill $(jobs -p) 2>/dev/null || true' EXIT

# until the runner's log has this line (or the runner is gone)
await() {
    local log=$1 pid=$2 line=$3 _
    for _ in $(seq 600); do
        grep -q "$line" "$log" && return 0
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1
    done
    cat "$log"
    echo "no \"$line\" from the runner" >&2
    return 1
}

# the runner listens for the strategy, the strategy plays its game, the runner ends
play() {
    local log=$1 pid=$2
    await "$log" "$pid" "Waiting for connection on port 31101"
    (cd client && "$py" main.py 127.0.0.1 31101)
    wait "$pid" || { cat "$log"; echo "the runner failed" >&2; return 1; }
}

status() {      # the viewer's answer to a page request with this Host header (000: none)
    curl -s -o /dev/null -w '%{http_code}' -H "Host: $1" http://127.0.0.1:31100/ || true
}

"$bin" --config config.json --batch-mode --save-replay replay.dat --save-results results.json 2> batch.log &
play batch.log $!

"$bin" --config config.json --no-browser --viewer-exit --viewer-speed 0 --limits warn --viewer-port 31100 \
    --save-replay gui-replay.dat --save-results gui-results.json 2> gui.log &
pid=$!
await gui.log $pid "Viewer: "
own=$(status 127.0.0.1:31100) evil=$(status evil.example:31100)
echo "viewer: $own for its own Host, $evil for another"
[ "$own" = 200 ] && [ "$evil" = 403 ]
play gui.log $pid

cmp replay.dat gui-replay.dat
cmp results.json gui-results.json
grep -q '"results"' results.json
sha256sum replay.dat results.json 2>/dev/null || shasum -a 256 replay.dat results.json
