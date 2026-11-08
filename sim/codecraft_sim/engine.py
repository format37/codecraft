"""The game loop of `aicup2020 --batch-mode`, as the server runs it (docs/KICKOFF.md, server/codecraft/game.py):

    code = run(config, save_results="/game/results.json", save_replay="/game/replay.dat", log=Log("info"))
    code = run(config, ..., rules="2020")      # the 2020 rules exactly (default "codecraft": rules.FogMemory)

1. The game: `{"Create": "Round1" | "Round2" | "Finals" | {"Custom": {...}}}` (mapgen.py), or `{"LoadFrom": path}`, a
   state file: the engine's serde JSON of a Model (model.py).
2. The seats (players.py): every TCP seat listens, then they are all accepted at once.
3. Every tick until the game is over (rules.game_over): each seat, one after another in seat order, is sent the view
   of its player (view.py; whether the player still owns anything or not) and answers with orders for its
   entities. An order for an entity that does not exist or is not the player's is dropped with an error line; the
   others are stored, turned back into the global frame, until replaced. Then the turn is played (rules.py) and
   written to the replay (replay.py): the orders answered to the view of tick t are in the record of tick t + 1.
4. At the end each seat still playing gets Finish, and results.json says per seat whether it crashed and why, its
   score, and the seed.

A game over at the start (fewer than two players own entities) writes a replay holding only its first state and no
results, and exits with 0, as the official engine does. A config or state the engine cannot read ends it with 101,
the code of a Rust panic.

Randomness: one random.Random(seed) makes the map and plays the game, so a seed and deterministic players replay a
game exactly; a null seed draws one, which goes to results.json.
"""
from __future__ import annotations

import json
import random
import secrets
import threading
from pathlib import Path

from . import mapgen, rules
from .model import Model, model_from_json
from .players import EmptyPlayer, Log, QuickStartPlayer, Seat, TcpPlayer
from .replay import ReplayWriter
from .view import action_from_view, player_view, sight_map

PANIC = 101


class ConfigError(ValueError):
    """A config or a state the engine cannot play."""


def load_game(config: dict, seats: int, rng: random.Random) -> Model:
    game = config.get("game")
    if not isinstance(game, dict) or len(game) != 1:
        raise ConfigError("`game` must be {\"Create\": ...} or {\"LoadFrom\": \"/path/state.json\"}")
    (kind, value), = game.items()
    if kind == "LoadFrom":
        try:
            model = model_from_json(json.loads(Path(value).read_text()))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ConfigError(f"Could not load the state {value}: {exc}") from None
        if sorted(p.index for p in model.players.values()) != list(range(seats)):
            raise ConfigError(f"the state has players for the seats {sorted(p.index for p in model.players.values())}, "
                              f"the config {seats} seats")
        return normalized(model)
    if kind == "Create":
        try:
            if isinstance(value, str):
                options = mapgen.preset(value)
            elif isinstance(value, dict) and set(value) == {"Custom"}:
                options = mapgen.options_from_json(value["Custom"])
            else:
                raise ValueError(f"unknown game {value!r}")
            return mapgen.create(options, seats, rng)
        except (ValueError, KeyError, TypeError) as exc:
            raise ConfigError(f"Could not load config: {exc}") from None
    raise ConfigError(f"unknown variant `{kind}`, expected `Create` or `LoadFrom`")


def normalized(m: Model) -> Model:
    """The model with its players, entities and stored orders by id (a state file may list them in any order; the
    rules do not depend on it, the replay is simpler to compare so)."""
    m.players = dict(sorted(m.players.items()))
    m.entities = dict(sorted(m.entities.items()))
    m.entity_actions = dict(sorted(m.entity_actions.items()))
    return m


def make_seat(spec, log: Log) -> Seat:
    if spec in ("Empty", "QuickStart"):
        spec = {spec: None}
    if not isinstance(spec, dict) or len(spec) != 1:
        raise ConfigError(f"unknown player {spec!r}")
    (kind, value), = spec.items()
    if kind == "Empty":
        return EmptyPlayer()
    if kind == "QuickStart":
        return QuickStartPlayer()
    if kind == "Tcp":
        try:
            return TcpPlayer(value.get("host"), value["port"], value.get("accept_timeout"), value.get("timeout"),
                             value.get("token"), log)
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"bad Tcp player {value!r}: {exc}") from None
    raise ConfigError(f"unknown player `{kind}`, expected `Tcp`, `QuickStart` or `Empty`")


def connect(seats: list[Seat], log: Log) -> None:
    """Listen on every TCP seat, then accept them all at once; a seat that fails is crashed."""
    tcp = [s for s in seats if isinstance(s, TcpPlayer)]
    for s in tcp:
        s.listen()
    threads = [threading.Thread(target=s.start, daemon=True) for s in tcp]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for s in tcp:
        if s.failure is not None:
            s.crash(s.failure, log)


def take_orders(model: Model, player_id: int, seat: int, actions: dict, log: Log) -> None:
    for eid, action in actions.items():
        e = model.entities.get(eid)
        if e is None:
            log.error("Got action for non-existent entity")
        elif e.player_id != player_id:
            log.error("Got action for entity not belonging to the player")
        else:
            model.entity_actions[eid] = action_from_view(seat, action, model)


RULES = ("codecraft", "2020")      # the arena's rules (the default), the 2020 ones exactly


def play(model: Model, seats: list[Seat], rng: random.Random, replay: ReplayWriter | None, log: Log,
         rules_name: str = "codecraft") -> None:
    """Play the game from `model` (changed in place) to its end."""
    by_seat = {p.index: p.id for p in model.players.values()}
    memory = rules.FogMemory() if rules_name == "codecraft" else None
    while not rules.game_over(model):
        seen = {}
        if model.fog_of_war:
            seen = {pid: sight_map(model, pid) for pid in model.players}
        asked = False
        for seat, s in enumerate(seats):
            if s.crashed or isinstance(s, EmptyPlayer):
                continue
            pid = by_seat[seat]
            try:
                actions = s.get_action(player_view(model, pid, seen.get(pid)))
            # a seat's failure or a bot's bug: the seat is out, the game goes on
            except Exception as exc:  # noqa: BLE001
                s.crash(str(exc) if exc.args else type(exc).__name__, log)
                continue
            take_orders(model, pid, seat, actions, log)
            asked = True
        if asked:
            model.entity_actions = dict(sorted(model.entity_actions.items()))
        events = rules.process_turn(model, rng, seen, memory)
        if replay is not None:
            replay.tick(events, model)


def results(model: Model, seats: list[Seat], seed: int) -> dict:
    score = {p.index: p.score for p in model.players.values()}
    return {"players": [{"crashed": s.crashed, "comment": s.comment} for s in seats],
            "results": [score[i] for i in range(len(seats))], "seed": seed}


def run(config: dict, save_results: str | None = None, save_replay: str | None = None, log: Log | None = None,
        rules_name: str = "codecraft") -> int:
    log = log or Log()
    if rules_name not in RULES:
        log.error(f"panicked at 'unknown rules {rules_name!r}, expected codecraft or 2020'")
        return PANIC
    try:
        seed = config.get("seed")
        if seed is None:
            seed = secrets.randbits(64)
        elif not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed < 1 << 64:
            raise ConfigError(f"the seed must be an unsigned 64-bit number, not {seed!r}")
        specs = config.get("players")
        if not isinstance(specs, list) or not 1 <= len(specs) <= 4:
            raise ConfigError("`players` must list 1 to 4 seats")
        rng = random.Random(seed)
        model = load_game(config, len(specs), rng)
        seats = [make_seat(spec, log) for spec in specs]
    except ConfigError as exc:
        log.error(f"panicked at '{exc}'")
        return PANIC
    try:
        connect(seats, log)
    except OSError as exc:
        log.error(f"panicked at 'Could not listen: {exc}'")
        return PANIC
    over_at_start = rules.game_over(model) and model.current_tick < model.max_tick_count
    replay = None
    try:
        if save_replay:
            replay = ReplayWriter(save_replay)
            replay.start(model)
        if not rules.game_over(model):
            play(model, seats, rng, replay, log, rules_name)
    except Exception as exc:  # noqa: BLE001 - a bug of ours: no results, the code of a panic
        log.error(f"panicked at '{type(exc).__name__}: {exc}'")
        return PANIC
    finally:
        for s in seats:
            if not s.crashed:
                s.finish()
        if replay is not None:
            replay.close()
    if save_results and not over_at_start:
        Path(save_results).write_text(json.dumps(results(model, seats, seed), indent=2))
    return 0
