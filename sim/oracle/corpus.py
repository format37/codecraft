"""The corpus check: turns of the stored official games (data/games/*/replay.dat) played again by our rules.

    python3 -m oracle.corpus [--every N] [--games K] [--seeds S] [--jobs J]

For each sampled turn t -> t + 1: the state at t, with the stored actions of t + 1 as the orders (an order answered
to the view of tick t is stored at t + 1), is played by rules.py and compared with the state at t + 1 on figures
that depend little on the random draws:

- `attackers`: which entities attacked (the attackers of the events, as a set);
- `next_id`: the ids used up, i.e. the number of build attempts;
- `entities`: how many entities there are after the turn;
- `resource`: each player's resource.

The official draws (attack order and targets, build order, paths, conflicts) are its own, so a turn that differs
with seed 1 is played again with other seeds: if one of them gives all four figures of the record, the difference
was the draws (`random`); if none does within `--seeds`, it is `unexplained`, which is where a rule our engine
gets wrong shows up. Identical replays are checked once. An exact turn-by-turn check that follows the record's own
draws is step 4 of the kickoff plan.
"""
from __future__ import annotations

import argparse
import hashlib
import random
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from codecraft_sim import replay as replay_io
from codecraft_sim import rules

ROOT = Path(__file__).resolve().parents[2]
GAMES = ROOT / "data" / "games"
CHECKS = ("attackers", "next_id", "entities", "resource")


def figures(m, events) -> dict:
    return {
        "attackers": frozenset(e.a for e in events),
        "next_id": m.next_id,
        "entities": len(m.entities),
        "resource": tuple(sorted((p.id, p.resource) for p in m.players.values())),
    }


def play(prev, orders, seed: int):
    m = prev.copy()
    m.entity_actions = orders
    events = rules.process_turn(m, random.Random(seed))
    return m, events


def check_turn(prev, nxt, events_record, seeds: int) -> tuple[str, list[str]]:
    """("ok" | "random" | "unexplained", the checks that differ with seed 1)."""
    want = figures(nxt, events_record)
    orders = dict(nxt.entity_actions)
    got = figures(*play(prev, orders, 1))
    differ = [k for k in CHECKS if got[k] != want[k]]
    if not differ:
        return "ok", []
    for seed in range(2, seeds + 1):
        if figures(*play(prev, orders, seed)) == want:
            return "random", differ
    return "unexplained", differ


def check_game(path: Path, every: int, seeds: int) -> dict:
    rep = replay_io.load(path)
    states = list(rep.states())
    out = {"game": path.parent.name, "turns": 0, "ok": 0, "random": 0, "unexplained": Counter(), "examples": []}
    for t in range(0, len(states) - 1, every):
        status, differ = check_turn(states[t], states[t + 1], rep.ticks[t].events, seeds)
        out["turns"] += 1
        if status in ("ok", "random"):
            out[status] += 1
        else:
            out["unexplained"].update(differ)
            if len(out["examples"]) < 5:
                out["examples"].append(f"{path.parent.name} tick {states[t].current_tick}: {', '.join(differ)}")
    return out


def distinct_games(paths: list[Path]) -> tuple[list[Path], int]:
    seen, out = set(), []
    for p in paths:
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        if digest not in seen:
            seen.add(digest)
            out.append(p)
    return out, len(paths) - len(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--every", type=int, default=10, help="check every N-th turn of each game")
    ap.add_argument("--games", type=int, default=0, help="only the first K distinct games (0 = all)")
    ap.add_argument("--seeds", type=int, default=40, help="seeds to try on a turn that differs")
    ap.add_argument("--jobs", type=int, default=0, help="processes (0 = one per CPU)")
    args = ap.parse_args(argv)
    paths, dupes = distinct_games(sorted(GAMES.glob("*/replay.dat")))
    if args.games:
        paths = paths[:args.games]
    if not paths:
        print(f"no games under {GAMES}")
        return 1
    print(f"{len(paths)} distinct games ({dupes} duplicates skipped), every {args.every}th turn", flush=True)
    start = time.time()
    turns = ok = rnd = 0
    unexplained: Counter = Counter()
    examples: list[str] = []
    with ProcessPoolExecutor(args.jobs or None) as pool:
        n = len(paths)
        for r in pool.map(check_game, paths, [args.every] * n, [args.seeds] * n):
            turns, ok, rnd = turns + r["turns"], ok + r["ok"], rnd + r["random"]
            unexplained.update(r["unexplained"])
            examples += r["examples"]
            flags = " ".join(f"{k}:{v}" for k, v in r["unexplained"].items())
            print(f"{r['game']}: {r['turns']} turns, {r['random']} random  {flags}", flush=True)
    bad = turns - ok - rnd
    print(f"\n{len(paths)} games, {turns} turns in {time.time() - start:.0f} s: {ok} as recorded with seed 1, "
          f"{rnd} with another seed, {bad} unexplained ({100 * bad / max(turns, 1):.2f} %)")
    for k in CHECKS:
        print(f"  {k:10s} differs in {unexplained[k]} unexplained turns")
    for e in examples[:30]:
        print("  e.g.", e)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
