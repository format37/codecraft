"""Fixtures: a starting state, the players, and what the official engine made of them, as language-neutral JSON
(the format is described in sim/fixtures/README.md). The Python engine and the C++ clone are both checked
against these files.

    fx = record(scenario)               # run the scenario on the oracle -> fixture dict
    write(fx)                           # sim/fixtures/<name>.json
    problems = compare(fx, fresh)       # [] when a run (oracle or ours) reproduces the fixture
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from codecraft_sim.model import (DEFAULT_PROPERTIES, EntityProperties, EntityType, Event, Model, action_json,
                                 entity_json, model_from_json, model_json, player_json, properties_json, view_json)

from . import loadfrom
from .puppet import Puppet

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ENGINE_VERSION = "aicup2020 1.3.0"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# engine log lines worth keeping in a fixture: everything but the port bookkeeping (ports are random)
NOTABLE_RE = re.compile(r"^(?:INFO|WARN|ERROR) - (?!Waiting for connection|Got connection|Dropping tcp player|"
                        r"Stop listening|Timeout accepting player)")


def snapshot(m: Model, events: list[Event]) -> dict:
    """The part of a model that changes during a game, with the events of the tick that produced it."""
    return {
        "tick": m.current_tick,
        "next_id": m.next_id,
        "players": [player_json(p) for _, p in sorted(m.players.items())],
        "entities": [entity_json(e) for _, e in sorted(m.entities.items())],
        "entity_actions": [{"entity_id": k, "action": action_json(a)} for k, a in sorted(m.entity_actions.items())],
        "events": [list(e) for e in events],
    }


def state_json(m: Model) -> dict:
    """The model as stored in a fixture: `model_json`, with "default" for the doc's property table."""
    d = model_json(m)
    if _is_default(m.entity_properties):
        d["entity_properties"] = "default"
    return d


def state_from_json(d: dict) -> Model:
    if d.get("entity_properties") == "default":
        d = dict(d, entity_properties={t.json_name: properties_json(p) for t, p in DEFAULT_PROPERTIES.items()})
    return model_from_json(d)


def _is_default(props: dict[EntityType, EntityProperties]) -> bool:
    return dict(props) == DEFAULT_PROPERTIES


def notable_log(log: str) -> list[str]:
    out = []
    for line in ANSI_RE.sub("", log).splitlines():
        line = line.strip()
        if NOTABLE_RE.match(line):
            out.append(line)
    return out


def record(sc) -> dict:
    """Run a scenario (oracle.scenarios.Scenario) on the official engine and return the fixture."""
    model, players = sc.build()
    run = loadfrom.run(model, players, seed=sc.seed, timeout=sc.timeout, accept_timeout=sc.accept_timeout)
    if run.replay is None and run.results is not None:
        raise RuntimeError(f"{sc.name}: no replay written; log:\n{run.log}")
    fx = {
        "name": sc.name,
        "about": sc.about,
        "questions": sc.questions,
        "engine": ENGINE_VERSION,
        "seed": sc.seed,
    }
    if any(isinstance(p, Puppet) for p in players):
        fx["timeout"] = sc.timeout
        fx["accept_timeout"] = sc.accept_timeout
    fx["players"] = [p.spec() if isinstance(p, Puppet) else p for p in players]
    if sc.random:
        fx["random"] = sc.random
    fx["state"] = state_json(model)
    expected: dict = {"returncode": run.returncode, "results": run.results, "log": notable_log(run.log)}
    puppets = {}
    for seat, p in run.puppets.items():
        views = [v.current_tick for v in p.views]
        entry = {"view_ticks": views, "finished": p.finished}
        if sc.record_views:
            entry["views"] = [view_json(v) for v in p.views]
        puppets[str(seat)] = entry
    if puppets:
        expected["puppets"] = puppets
    expected["ticks"] = [snapshot(m, ev) for m, ev in zip(run.states, run.events)]
    fx["expected"] = expected
    return fx


def compare(fx: dict, fresh: dict) -> list[str]:
    """The differences between a fixture and a fresh recording of it (both fixture dicts)."""
    a, b = fx["expected"], fresh["expected"]
    out = []
    for key in ("returncode", "results", "log"):
        if a.get(key) != b.get(key):
            out.append(f"{key}: expected {a.get(key)!r}, got {b.get(key)!r}")
    if a.get("puppets") != b.get("puppets"):
        out.append(f"puppets: expected {a.get('puppets')!r}, got {b.get('puppets')!r}")
    ta, tb = a["ticks"], b["ticks"]
    if len(ta) != len(tb):
        out.append(f"tick count: expected {len(ta)}, got {len(tb)}")
    for sa, sb in zip(ta, tb):
        for key in ("tick", "next_id", "players", "entities", "entity_actions", "events"):
            if sa[key] != sb[key]:
                out.append(f"tick {sa['tick']} {key}: expected {_short(sa[key])}, got {_short(sb[key])}")
                break
        if len(out) > 20:
            out.append("...")
            break
    return out


def _short(v) -> str:
    s = json.dumps(v, separators=(",", ":"))
    return s if len(s) < 400 else s[:400] + "..."


# Files ---------------------------------------------------------------------------------------------------
def path_of(name: str) -> Path:
    return FIXTURES / f"{name}.json"


def write(fx: dict) -> Path:
    path = path_of(fx["name"])
    path.write_text(dumps(fx) + "\n")
    return path


def load(name_or_path: str | Path) -> dict:
    p = Path(name_or_path)
    if not p.suffix:
        p = path_of(str(name_or_path))
    return json.loads(p.read_text())


def all_fixtures() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(FIXTURES.glob("*.json"))]


def dumps(obj, indent: int = 0, width: int = 200, col: int | None = None) -> str:
    """JSON with one small object per line: a value stays on one line when it fits in `width` columns (`col` is
    the column it starts at)."""
    col = indent if col is None else col
    flat = json.dumps(obj, separators=(", ", ": "))
    if not isinstance(obj, (dict, list)) or not obj or col + len(flat) <= width:
        return flat
    pad = " " * (indent + 1)
    if isinstance(obj, dict):
        items = []
        for k, v in obj.items():
            key = json.dumps(k) + ": "
            items.append(pad + key + dumps(v, indent + 1, width, indent + 1 + len(key)))
        return "{\n" + ",\n".join(items) + "\n" + " " * indent + "}"
    items = [pad + dumps(v, indent + 1, width) for v in obj]
    return "[\n" + ",\n".join(items) + "\n" + " " * indent + "]"
