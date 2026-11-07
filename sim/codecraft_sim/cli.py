"""The command line of the official local runner (`aicup2020 1.3.0`), batch mode only:

    python3 -m codecraft_sim --config /game/config.json --batch-mode --save-results /game/results.json \\
        --save-replay /game/replay.dat --log-level info --player-names a b c d

The options of the official runner that only its GUI or its client generator uses are accepted and ignored. There is
no GUI: `--config` is required, with or without `--batch-mode` (the official runner without it opens a window).
"""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .engine import PANIC, run
from .players import Log


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="codecraft-sim", description="CodeCraft 2020 game engine (our own), batch mode")
    p.add_argument("-V", "--version", action="version", version=f"codecraft-sim {__version__}")
    p.add_argument("--config", help="the game config (JSON): seed, game, players")
    p.add_argument("--batch-mode", action="store_true", help="no GUI (the only mode there is)")
    p.add_argument("--save-results", help="write results.json here")
    p.add_argument("--save-replay", help="write the replay here")
    p.add_argument("--log-level", default="info", help="off, error, warn, info (default), debug, trace")
    p.add_argument("--player-names", nargs="*", help="ignored (shown by the official GUI)")
    for name in ("--repeat", "--replay", "--lang", "--client-gen", "--client-gen-options",
                 "--client-gen-host-from-docker", "--client-gen-test-mode"):
        p.add_argument(name, help=argparse.SUPPRESS)
    p.add_argument("--client-gen-language", nargs="*", help=argparse.SUPPRESS)
    p.add_argument("--client-gen-preserve-files", action="store_true", help=argparse.SUPPRESS)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    log = Log(args.log_level)
    if args.replay or args.client_gen:
        log.error("panicked at 'this engine has no GUI and no client generator'")
        return PANIC
    if not args.config:
        log.error("panicked at 'Batch mode can only be used with --config option'")
        return PANIC
    try:
        with open(args.config) as f:
            config = json.load(f)
    except (OSError, ValueError) as exc:
        log.error(f"panicked at 'Could not load config: {exc}'")
        return PANIC
    if not isinstance(config, dict):
        log.error("panicked at 'Could not load config: not a JSON object'")
        return PANIC
    return run(config, args.save_results, args.save_replay, log)


if __name__ == "__main__":
    sys.exit(main())
