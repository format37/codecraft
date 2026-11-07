"""The corpus check: every turn of the stored official games (data/games/*/replay.dat) played again by our rules,
following the record's own random draws, and compared with the record exactly.

    python3 -m oracle.corpus [--every N] [--games K] [--jobs J] [--report FILE]

A turn t -> t + 1 is played from the recorded state at t, with the stored actions of t + 1 as the orders (an order
answered to the view of tick t is stored in the delta of tick t + 1). Where our rules draw at random
(`rules._Turn`'s draw methods and the ties of the path searches), the choice is read off the record:

- the attack order from the order of the events, the target of an auto-attack from its event;
- the build order from the ids of the new entities (every attempt takes the next id, a failed one too);
- a unit's step from where it stands at t + 1, or from the cell it attacked (where a search is cut short by the
  node budget, other draws are tried until the recorded step comes out); a unit that stays put finds no step (a
  hunt whose node budget runs out before it reaches a target, in some draws), or takes a step it cannot make:
  into a cell the record shows taken at the end of the movement, or one a unit dying in the tick took first (a
  unit at 0 health still moves, then it is removed and the record cannot show where it went);
- the winner of a movement conflict from who got the cell.

The turn must then give the recorded state at t + 1 exactly: every entity (position, health, activity), every
player's score and resource, `next_id`, and the events in their order. A turn that differs points at a rule we
get wrong, or at a choice the official engine made that our rules cannot make (a step on none of our paths: the
notes name the unit).

With every turn checked (the default) a game is played from its first state, the official map and placements,
with our own state carried from turn to turn: only the orders and the random choices come from the record (after
a turn that differs the check goes on from the recorded state). A game without a difference is replayed exactly
by our engine, and its final scores must be those of its results.json. Copies of one game (same seed, same scores)
are checked once.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable

from codecraft_sim import replay as replay_io
from codecraft_sim import rules
from codecraft_sim.model import Entity, Event, Model, Vec2
from codecraft_sim.pathfind import FREE

ROOT = Path(__file__).resolve().parents[2]
GAMES = ROOT / "data" / "games"

# Draws tried for a step: one the record shows (a search cut short by the node budget may take it in one draw of
# sixty), one the record implies (a dying unit's, see `GuidedTurn.plan`), and for staying put (a hunt may run out of
# nodes before it finds a target in one draw of ten).
RECORDED_TRIES, FORCED_TRIES, STAY_TRIES = 600, 48, 48


class GuidedTurn(rules._Turn):
    """A turn of our rules whose random choices are those the record shows: `nxt` is the recorded state after the
    turn and `events` its events. Where the record shows a choice our rules cannot make, ours makes its own (and
    says so in `notes`); the comparison with the record tells the rest."""

    def __init__(self, model: Model, nxt: Model, events: list[Event]) -> None:
        super().__init__(model, random.Random(0))
        self.nxt, self.record = nxt, list(events)
        self.first_new = model.next_id
        start = model.entities
        self.dest = {i: e.position for i, e in nxt.entities.items() if i in start and e.position != start[i].position}
        self.target_of: dict[int, int] = {}
        self.rank: dict[int, int] = {}
        for k, ev in enumerate(events):
            self.target_of.setdefault(ev.a, ev.b)
            self.rank.setdefault(ev.a, k)
        # the cells taken at the end of the movement as the record shows them: the entities of the next state, and
        # those removed at the end of the tick where they stood at its start
        self.taken: set[int] = set()
        for e in list(nxt.entities.values()) + [e for i, e in start.items() if i not in nxt.entities]:
            self.taken.update(self.grid.square(e.position, self.props[e.entity_type].size))
        # units removed at the end of the tick that did not attack: they may still have moved (a unit at 0 health
        # moves), into a cell the record cannot show; one of them may have taken a cell a unit that stayed wanted
        self.dying = {i for i, e in start.items() if i not in nxt.entities and self.props[e.entity_type].can_move
                      and i not in self.target_of}
        self.dying_near: dict[int, list[int]] = {}
        for i in sorted(self.dying):
            for c in self.neighbours(start[i].position):
                self.dying_near.setdefault(self.grid.index(c), []).append(i)
        self.forced: dict[int, Vec2] = {}       # unit -> the step a dying unit must take (see `plan`)
        self.planned: dict[int, Vec2 | None] = {}
        self.notes: list[str] = []

    # pathfinding: the recorded step where the draws allow it ---------------------------------------------------
    def move_step(self, e, mv, prefer=None):
        return self.steer(e, lambda c: rules._Turn.move_step(self, e, mv, c))

    def hunt(self, e, auto, prefer=None):
        # with a move order as well, a hunt that finds no target leaves the step to the move (see `plan_unit`)
        then_move = self.order_of(e).move_action is not None
        return self.steer(e, lambda c: rules._Turn.hunt(self, e, auto, c), none_ok=then_move)

    def steer(self, e: Entity, search: Callable[[Vec2 | None], Vec2 | None], none_ok: bool = False) -> Vec2 | None:
        """The step of `e` from `search`, with the draws the record shows. `none_ok`: no step is fine too (a hunt
        followed by a move order)."""
        want = self.forced.get(e.id) or self.recorded_step(e)
        if want is not None:
            tries = FORCED_TRIES if e.id in self.forced else RECORDED_TRIES
            return self.attempt(search, want, lambda s: s == want or (none_ok and s is None), tries)
        if e.id in self.dying:
            return search(None)             # removed at the end of the tick: where it went does not show
        # it neither moves nor attacks in the record: no step (a search that ends where it started, a hunt whose node
        # budget runs out before it finds a target), or a step into a cell the record shows taken
        step = self.attempt(search, None, lambda s: s is None or self.stays(e, s) == 2, STAY_TRIES)
        if step is None or self.stays(e, step) == 2:
            return step
        # a step into a cell the record shows taken, with the search favouring it; else (less sure, see `plan`) a
        # cell a unit dying in the tick may have taken first
        for level in (2, 1):
            for c in self.neighbours(e.position):
                if self.stays(e, c) == level:
                    s = self.attempt(search, c, lambda s: s is None or self.stays(e, s) >= level, 4)
                    if s is None or self.stays(e, s) >= level:
                        return s
        self.notes.append(f"{self.who(e.id)}: stays put in the record, but every step we find moves it or attacks")
        return step

    def attempt(self, search, prefer: Vec2 | None, ok, tries: int) -> Vec2 | None:
        """`search` with `prefer` favoured among equal paths (with None, as our rules run it); in a search cut short
        by the node budget that is not always enough, so other draws are tried until one gives a step `ok` takes."""
        step = search(prefer)
        if ok(step):
            return step
        saved = self.rng
        try:
            for seed in range(tries):
                self.rng = random.Random(seed)
                s = search(prefer)
                if ok(s):
                    return s
        finally:
            self.rng = saved
        return step

    def recorded_step(self, e: Entity) -> Vec2 | None:
        """The cell the record shows `e` stepping into: where it went, or the cell of what it attacked instead of
        moving (a unit that gets a step has nothing to hit with its attack order)."""
        if e.id in self.dest:
            return self.dest[e.id]
        t = self.m.entities.get(self.target_of.get(e.id, -1))
        if t is not None:
            for i in self.grid.square(t.position, self.props[t.entity_type].size):
                c = Vec2(i % self.grid.size, i // self.grid.size)
                if abs(c.x - e.position.x) + abs(c.y - e.position.y) == 1:
                    return c
        return None

    def neighbours(self, p: Vec2) -> list[Vec2]:
        return [Vec2(x, y) for x, y in ((p.x + 1, p.y), (p.x - 1, p.y), (p.x, p.y + 1), (p.x, p.y - 1))
                if self.grid.inside(x, y)]

    def stays(self, e: Entity, step: Vec2) -> int:
        """Whether `e` with this step neither attacks nor moves, as the record shows it does neither: 2 when the cell
        is taken at the end of the movement or `e` builds or repairs, 1 when a dying unit may have taken the cell
        first, 0 when it would move or attack."""
        if not self.grid.inside(*step):
            return 2
        o = self.grid.occupant(step)
        if o != FREE and self.m.entities[o].player_id != e.player_id:
            return 0                                    # it would attack what stands there
        i = self.grid.index(step)
        if i in self.taken or self.does_other_work(e):
            return 2
        return int(bool(self.dying_near.get(i)))      # `plan` sends one of them there, if one can go

    def does_other_work(self, e: Entity) -> bool:
        """Whether the record shows `e` building or repairing this turn (then it does not move whatever its step)."""
        a = self.m.entity_actions.get(e.id)
        if a.build_action is not None:
            b = a.build_action
            size = self.props[e.entity_type].size
            for i in range(self.first_new, self.nxt.next_id):
                n = self.nxt.entities.get(i)
                if (n is not None and n.entity_type == b.entity_type and n.position == b.position
                        and rules.distance(e.position, size, b.position, self.props[b.entity_type].size) == 1):
                    return True
        rp = self.props[e.entity_type].repair
        if a.repair_action is not None and rp is not None:
            t = self.m.entities.get(a.repair_action.target)
            if (t is not None and t.id in self.nxt.entities and t.entity_type in rp.valid_targets
                    and rules.distance(e.position, 1, t.position, self.props[t.entity_type].size) == 1):
                return True
        return False

    def plan_unit(self, e: Entity) -> Vec2 | None:
        step = self.planned[e.id] = super().plan_unit(e)
        return step

    def plan(self) -> dict[int, Vec2]:
        steps = super().plan()
        self.explain_stays(steps)
        for i, cell in self.dest.items():
            if steps.get(i) != cell:
                self.notes.append(f"{self.who(i)}: steps to {fmt(cell)} in the record, {fmt(steps.get(i))} with us")
        return steps

    def explain_stays(self, steps: dict[int, Vec2]) -> None:
        """A unit that stays in the record with its step free at the end of the movement lost that cell to a unit
        that died in the tick (it moves, then it is removed): send a dying neighbour of the cell there (it wins the
        conflict, see `conflict_winner`). Where none can go, the unit took another of its steps, one a dying unit
        takes. The units with the fewest such cells go first; a dying unit sent somewhere stays sent."""
        todo = []
        for u, s in steps.items():
            if u in self.dest or u in self.dying or u in self.target_of or not self.grid.inside(*s):
                continue
            e = self.m.entities[u]
            if self.grid.index(s) in self.taken or self.does_other_work(e):
                continue
            cells = [s] + [c for c in self.neighbours(e.position) if c != s and self.grid.index(c) not in self.taken
                           and self.dying_near.get(self.grid.index(c))]
            todo.append((len(cells), u, cells))
        for _, u, cells in sorted(todo, key=lambda t: t[:2]):
            e, s = self.m.entities[u], steps[u]
            cells.sort(key=lambda c: not self.sent_to(c))       # a cell a dying unit is sent to already first
            for c in cells:
                if c != s:
                    self.forced[u] = c
                    if self.plan_unit(e) != c:
                        del self.forced[u]
                        continue
                if self.send_dying(c, steps):
                    steps[u] = c
                    break
                if c != s:
                    del self.forced[u]
            else:
                self.planned[u] = s
                self.notes.append(f"{self.who(u)}: stays put in the record, but its step {fmt(s)} is free at the end "
                                  f"and no unit dying in the tick can have taken it")

    def sent_to(self, cell: Vec2) -> bool:
        return any(self.forced.get(d) == cell for d in self.dying_near.get(self.grid.index(cell), ()))

    def send_dying(self, cell: Vec2, steps: dict[int, Vec2]) -> bool:
        """Send a unit dying in the tick, next to `cell`, there: one sent there already, one whose own step goes
        there, else one not sent anywhere yet that can step there."""
        near = self.dying_near.get(self.grid.index(cell), ())
        if self.sent_to(cell):
            return True
        for d in near:
            if d not in self.forced and steps.get(d) == cell:
                self.forced[d] = cell
                return True
        for d in near:
            if d in self.forced:
                continue
            before = self.planned.get(d)
            self.forced[d] = cell
            if self.plan_unit(self.m.entities[d]) == cell:
                steps[d] = cell
                return True
            del self.forced[d]
            self.planned[d] = before
        return False

    # the draws ------------------------------------------------------------------------------------------------
    def attack_order(self, ids: list[int]) -> Iterable[int]:
        return sorted(ids, key=lambda i: self.rank.get(i, len(self.record)))

    def auto_target(self, e: Entity, enemies: list[Entity]) -> Entity:
        want = self.target_of.get(e.id)
        for t in enemies:
            if t.id == want:
                return t
        self.notes.append(f"{self.who(e.id)}: auto-attacks {want} in the record, not among ours "
                          f"{[t.id for t in enemies]}")
        return enemies[0]

    def build_order(self, ids: list[int]) -> Iterable[int]:
        """Each id the record shows given to a new entity goes to an attempt that orders it and can build it then;
        each other id to an attempt that fails then, one not needed for a later recorded build if there is one."""
        m = self.m

        def order(i: int) -> tuple:
            b = m.entity_actions[i].build_action
            return b.entity_type, b.position

        def can(i: int) -> bool:
            return self.can_build(m.entities[i], m.entity_actions[i].build_action)

        pending = [i for i in ids if order(i)[0] in self.props[m.entities[i].entity_type].build.options]
        made = {i: (n.entity_type, n.position) for i in range(self.first_new, self.nxt.next_id)
                if (n := self.nxt.entities.get(i)) is not None}
        while pending:
            slot = m.next_id
            if slot in made:
                cands = sorted((i for i in pending if order(i) == made[slot]), key=lambda i: i in self.dest)
                pick = next((i for i in cands if can(i)), cands[0] if cands else None)
                if pick is None:
                    self.notes.append(f"new entity {slot} ({made[slot][0].json_name} at {fmt(made[slot][1])}): no "
                                      f"builder ordered it")
                    pick = pending[0]
            else:
                later = {made[k] for k in made if k > slot}
                fails = [i for i in pending if not can(i)]
                pick = next((i for i in fails if order(i) not in later), fails[0] if fails else pending[0])
            pending.remove(pick)
            yield pick
        yield from (i for i in ids if order(i)[0] not in self.props[m.entities[i].entity_type].build.options)

    def conflict_winner(self, cell: Vec2, eids: list[int]) -> int:
        """The unit the record shows in the cell, else a dying unit (it took the cell, then died)."""
        for i in eids:
            if self.dest.get(i) == cell:
                return i
        for i in eids:
            if i in self.dying:
                return i
        return eids[0]

    # notes ----------------------------------------------------------------------------------------------------
    def who(self, i: int) -> str:
        e = self.m.entities[i]
        a = self.m.entity_actions.get(i)
        orders = []
        if a is not None:
            if a.move_action is not None:
                mv = a.move_action
                orders.append(f"move to {fmt(mv.target)}" + (" break_through" if mv.break_through else ""))
            if a.attack_action is not None:
                at = a.attack_action
                if at.target is not None:
                    orders.append(f"attack {at.target}")
                if at.auto_attack is not None:
                    valid = ",".join(t.json_name for t in at.auto_attack.valid_targets)
                    orders.append(f"auto {at.auto_attack.pathfind_range} [{valid}]")
            if a.build_action is not None:
                orders.append(f"build {a.build_action.entity_type.json_name} at {fmt(a.build_action.position)}")
            if a.repair_action is not None:
                orders.append(f"repair {a.repair_action.target}")
        return f"{e.entity_type.json_name} {i} of P{e.player_id} at {fmt(e.position)} ({'; '.join(orders)})"


def fmt(v) -> str:
    return "-" if v is None else f"({v.x},{v.y})"


def differences(ours: Model, events: list[Event], nxt: Model, record: list[Event]) -> list[tuple[str, str]]:
    """(kind, text) for each way a turn of ours differs from the record."""
    out = []
    if ours.next_id != nxt.next_id:
        out.append(("next_id", f"next_id {ours.next_id}, recorded {nxt.next_id}"))
    for pid in sorted(set(ours.players) | set(nxt.players)):
        a, b = ours.players.get(pid), nxt.players.get(pid)
        for f in ("score", "resource"):
            if getattr(a, f, None) != getattr(b, f, None):
                out.append((f, f"P{pid} {f} {getattr(a, f, None)}, recorded {getattr(b, f, None)}"))
    for eid in sorted(set(ours.entities) | set(nxt.entities)):
        a, b = ours.entities.get(eid), nxt.entities.get(eid)
        if a == b:
            continue
        if a is None:
            out.append(("missing", f"{b.entity_type.json_name} {eid} at {fmt(b.position)} recorded, not with us"))
        elif b is None:
            out.append(("extra", f"{a.entity_type.json_name} {eid} at {fmt(a.position)} with us, not recorded"))
        else:
            for f in ("position", "health", "active", "player_id", "entity_type"):
                x, y = getattr(a, f), getattr(b, f)
                if x != y:
                    x, y = (fmt(x), fmt(y)) if f == "position" else (x, y)
                    out.append((f, f"{b.entity_type.json_name} {eid} {f} {x}, recorded {y}"))
    if events != record:
        k = next((k for k, (x, y) in enumerate(zip(events, record)) if x != y), min(len(events), len(record)))
        ours_k, recorded_k = [tuple(e[1:]) for e in events[k:k + 3]], [tuple(e[1:]) for e in record[k:k + 3]]
        out.append(("events", f"events differ from #{k} of {len(record)}: ours {ours_k}, recorded {recorded_k}"))
    return out


def play_turn(prev: Model, nxt: Model, events: list[Event]) -> tuple[Model, list[Event], list[str]]:
    """Play the turn from `prev` with the orders and draws of the record (`nxt`, `events`): (our model after it,
    our events, the notes)."""
    m = prev.copy()
    m.entity_actions = dict(nxt.entity_actions)
    turn = GuidedTurn(m, nxt, events)
    return m, turn.play(), turn.notes


def check_turn(prev: Model, nxt: Model, events: list[Event]) -> tuple[list[tuple[str, str]], list[str]]:
    """Play the turn prev -> nxt with the record's orders and draws: (the differences, the notes); no differences
    when our rules give the record exactly."""
    m, ours, notes = play_turn(prev, nxt, events)
    return differences(m, ours, nxt, events), notes


def check_game(path: Path, every: int = 1) -> dict:
    """With `every` 1 the game is played from its first state, our own state carried from turn to turn (only the
    orders and the draws come from the record; after a turn that differs we go on from the record), and our final
    scores are compared with the game's results.json."""
    rep = replay_io.load(path)
    states = list(rep.states())
    out = {"game": path.parent.name, "turns": 0, "differ": []}
    prev = states[0]
    for t in range(0, len(states) - 1, every):
        nxt, events = states[t + 1], rep.ticks[t].events
        m, ours, notes = play_turn(prev if every == 1 else states[t], nxt, events)
        diffs = differences(m, ours, nxt, events)
        out["turns"] += 1
        if diffs:
            out["differ"].append({"tick": states[t].current_tick, "kinds": sorted({k for k, _ in diffs}),
                                  "diffs": [d for _, d in diffs[:12]], "notes": notes[:12]})
        prev = nxt if diffs else m
    if every == 1:
        res = path.parent / "results.json"
        out["scores"] = [p.score for p in sorted(prev.players.values(), key=lambda p: p.index)]
        out["official"] = json.loads(res.read_text()).get("results") if res.exists() else None
    return out


def game_key(path: Path) -> str:
    """Copies of one game (the server stored some games several times) have the same seed and scores; their files
    differ (the official engine writes its hash maps in a new order every run)."""
    res = path.parent / "results.json"
    if res.exists():
        d = json.loads(res.read_text())
        return json.dumps([d.get("seed"), d.get("results")])
    return hashlib.sha256(path.read_bytes()).hexdigest()


def distinct_games(paths: list[Path]) -> tuple[list[Path], int]:
    seen, out = set(), []
    for p in paths:
        k = game_key(p)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out, len(paths) - len(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--every", type=int, default=1, help="check every N-th turn of each game")
    ap.add_argument("--games", type=int, default=0, help="only the first K distinct games (0 = all)")
    ap.add_argument("--only", nargs="*", default=[], help="only these games (folder names under data/games)")
    ap.add_argument("--jobs", type=int, default=0, help="processes (0 = one per CPU)")
    ap.add_argument("--report", type=Path, help="write every differing turn, with the notes, to this JSON file")
    args = ap.parse_args(argv)
    paths, copies = distinct_games(sorted(GAMES.glob("*/replay.dat")))
    if args.only:
        paths = [p for p in paths if p.parent.name in args.only]
    if args.games:
        paths = paths[:args.games]
    if not paths:
        print(f"no games under {GAMES}")
        return 1
    print(f"{len(paths)} distinct games ({copies} copies skipped), every {args.every}th turn", flush=True)
    start = time.time()
    turns, kinds, games_exact, report, same_scores = 0, Counter(), 0, [], 0
    with ProcessPoolExecutor(args.jobs or None) as pool:
        futures = [pool.submit(check_game, p, args.every) for p in paths]
        for f in as_completed(futures):
            r = f.result()
            turns += r["turns"]
            games_exact += not r["differ"]
            same_scores += r.get("scores") is not None and r["scores"] == r.get("official")
            report += [{"game": r["game"], **d} for d in r["differ"]]
            c = Counter(k for d in r["differ"] for k in d["kinds"])
            kinds.update(c)
            scores = "" if "scores" not in r else (
                "  final scores as official" if r["scores"] == r["official"]
                else f"  final scores {r['scores']}, official {r['official']}")
            print(f"{r['game']}: {r['turns']} turns, {len(r['differ'])} differ  "
                  f"{' '.join(f'{k}:{v}' for k, v in sorted(c.items()))}{scores}", flush=True)
    bad = len(report)
    print(f"\n{len(paths)} games, {turns} turns in {time.time() - start:.0f} s: {turns - bad} exact, {bad} differ "
          f"({100 * bad / max(turns, 1):.2f} %); {games_exact} games without a difference")
    if args.every == 1:
        print(f"  played from each game's first state with our own state carried forward: {same_scores} of "
              f"{len(paths)} games end with the official scores")
    for k, v in kinds.most_common():
        print(f"  {k:12s} in {v} turns")
    report.sort(key=lambda d: (d["game"], d["tick"]))
    for d in report[:20]:
        print(f"  e.g. {d['game']} tick {d['tick']}: {d['diffs'][0]}" + (f"  [{d['notes'][0]}]" if d["notes"] else ""))
    if args.report:
        args.report.write_text(json.dumps(report, indent=1))
        print(f"report: {args.report}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
