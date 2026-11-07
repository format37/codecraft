"""The scenarios behind sim/fixtures: each is a small state + players that asks the official engine one rule
question (the numbers are the rule questions listed in sim/fixtures/README.md).

    python -m oracle.scenarios list                 # the scenarios and their questions
    python -m oracle.scenarios record [NAME...]     # run them on the oracle, (re)write sim/fixtures/NAME.json
    python -m oracle.scenarios check [NAME...]      # run them again and compare with the stored fixtures
    python -m oracle.scenarios alternatives [NAME...]   # (re)find every outcome of the turns that depend on the seed
    python -m oracle.scenarios show NAME            # a fixture tick by tick, as a human reads it

Run from sim/ (or with sim/ on PYTHONPATH). Map 20x20 unless said otherwise; player 1 = seat 0 in the corner
(0, 0), player 2 = seat 1. A fixture with `random` set records the outcome of one seed: another engine may
legitimately end elsewhere, and the field says what is random.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable

from codecraft_sim.model import Model
from codecraft_sim.view import to_view

from . import fixture as fixture_io
from .puppet import Puppet
from .state import NOTHING, State, attack, build, combine, move, repair


@dataclass
class Scenario:
    name: str
    questions: list[int]
    about: str
    make: Callable[[], tuple]
    seed: int = 1
    timeout: float | None = 1.0
    accept_timeout: float | None = 5.0
    random: str | None = None
    record_views: bool = False

    def build(self) -> tuple[Model, list]:
        state, players = self.make()
        return (state.model if isinstance(state, State) else state), players


SCENARIOS: dict[str, Scenario] = {}


def scenario(name: str, questions: list[int], about: str, **kw):
    def deco(f):
        if name in SCENARIOS:
            raise ValueError(f"duplicate scenario {name}")
        SCENARIOS[name] = Scenario(name, questions, about, f, **kw)
        return f
    return deco


E2 = ["Empty", "Empty"]


def two_builders(ticks: int, **kw) -> tuple[State, int, int]:
    """Player 1's builder A at (2, 2), player 2's builder B at (17, 17)."""
    s = State(ticks=ticks, **kw)
    return s, s.add("BuilderUnit", 1, (2, 2)), s.add("BuilderUnit", 2, (17, 17))


# Protocol: when actions count, what a seat may send --------------------------------------------------------
@scenario("actions-timing", [2], "A puppet sends a move at view tick 0, another at 3, an empty Action at 6 and an "
          "all-None EntityAction at 7. The action answered to view t is stored in the delta of tick t+1 and moves "
          "the unit in that same tick; an empty Action keeps the stored actions; an all-None EntityAction replaces "
          "the stored one (an update, not a removal) and the unit stops.",
          random="which first step the unit takes among those of equally short paths, a new draw every tick")
def _():
    s, a, b = two_builders(10)
    return s, [Puppet({0: {a: move((2, 10))}, 3: {a: move((10, 2))}, 6: {}, 7: {a: NOTHING}}), "Empty"]


@scenario("actions-foreign", [2], "View 0 is answered with orders for the seat's own unit and for the opponent's "
          "unit, view 1 with an order for an id that does not exist: the bad entries are dropped one by one (the "
          "own order of the same Action is kept), each logs an ERROR line, the seat is not crashed.")
def _():
    s, a, b = two_builders(4)
    return s, [Puppet({0: {a: move((2, 10)), b: move((17, 10))}, 1: {999: move((5, 5))}}), "Empty"]


@scenario("actions-stored-empty-seat", [2], "An Empty seat never answers: the actions stored in the state drive its "
          "units (a builder walking for 8 ticks).")
def _():
    s, a, b = two_builders(8)
    s.order(a, move((2, 12)))
    return s, E2


# Crashed seats -----------------------------------------------------------------------------------------------
@scenario("crash-disconnect", [9], "The seat closes the connection at view tick 2: crashed with the IO error as "
          "comment, the game goes on, its unit keeps walking on its stored action.")
def _():
    s, a, b = two_builders(6)
    return s, [Puppet({0: {a: move((2, 10))}}, crash_at=2), "Empty"]


@scenario("crash-timeout", [9], "The seat answers view tick 2 after 1.5 s with a 1 s budget: crashed (a socket read "
          "timeout, os error 11); the late order is not applied, the stored one goes on.")
def _():
    s, a, b = two_builders(6)
    return s, [Puppet({0: {a: move((2, 10))}, 2: {a: move((10, 2))}}, sleep_at={2: 1.5}), "Empty"]


@scenario("crash-garbage", [9], "The seat answers view tick 2 with an unknown ClientMessage tag: crashed.")
def _():
    s, a, b = two_builders(6)
    return s, [Puppet({0: {a: move((2, 10))}}, garbage_at=2), "Empty"]


@scenario("crash-token", [9], "The seat sends a wrong token: crashed before the first tick, the game is played "
          "without it.")
def _():
    s, a, b = two_builders(4)
    s.order(a, move((2, 10)))
    return s, [Puppet(token="wrong"), "Empty"]


@scenario("crash-no-connect", [9], "Nobody connects to the seat within accept_timeout (1 s): crashed, the game is "
          "played, the seat's stored actions still run.", accept_timeout=1.0)
def _():
    s, a, b = two_builders(4)
    s.order(a, move((2, 10)))
    return s, [Puppet(connect=False), "Empty"]


# Movement ------------------------------------------------------------------------------------------------------
@scenario("move-free", [5], "A builder walks from (1, 1) to (10, 10) on an empty map. The path is planned anew "
          "every tick and only its first step taken; among the equally short paths the engine draws one at random "
          "every tick, so the steps form an irregular staircase (two possible steps from each cell on the way).",
          random="which first step the unit takes among those of equally short paths, a new draw every tick")
def _():
    s = State(ticks=20)
    a = s.add("BuilderUnit", 1, (1, 1))
    s.add("BuilderUnit", 2, (18, 18))
    s.order(a, move((10, 10)))
    return s, E2


@scenario("move-free-other-ways", [5], "Four builders each walk to a cell 6 columns and 6 rows away, one per "
          "diagonal direction: in every direction each step is one of the two that start a shortest path, drawn "
          "at random.",
          random="which of the two first steps of the shortest paths each builder takes, every tick")
def _():
    s = State(ticks=13)
    for start, target in (((9, 9), (3, 3)), ((10, 9), (16, 3)), ((9, 10), (3, 16)), ((10, 10), (16, 16))):
        s.order(s.add("BuilderUnit", 1, start), move(target))
    s.add("BuilderUnit", 2, (19, 0))
    return s, E2


@scenario("move-adjacent", [5], "Target next to the unit: one step, then it stays.")
def _():
    s, a, b = two_builders(3)
    s.order(a, move((3, 2)))
    return s, E2


@scenario("move-occupied-target", [4, 5], "Target cell holds a resource, find_closest_position true for one builder "
          "and false for the other: both walk next to it and then mine it with no attack order (the next cell of "
          "the path holds an entity that is not theirs, so it is attacked).",
          random="which first step each unit takes among those of equally short paths")
def _():
    s = State(ticks=12)
    s.add("Resource", None, (10, 2))
    s.add("Resource", None, (10, 12))
    s.order(s.add("BuilderUnit", 1, (2, 2)), move((10, 2), closest=True))
    s.order(s.add("BuilderUnit", 1, (2, 12)), move((10, 12), closest=False))
    s.add("BuilderUnit", 2, (18, 18))
    return s, E2


@scenario("move-unreachable", [5], "Target inside a closed ring of resources: both units, with find_closest_position "
          "true and false, go to the reachable cell closest to it (the flag changes nothing) and do not attack the "
          "ring.")
def _():
    s = State(ticks=12)
    ring = [(x, y) for x in range(8, 13) for y in range(8, 13) if x in (8, 12) or y in (8, 12)]
    s.resources(ring)
    s.order(s.add("BuilderUnit", 1, (2, 10)), move((10, 10), closest=True))
    s.order(s.add("BuilderUnit", 1, (18, 10)), move((10, 10), closest=False))
    s.add("BuilderUnit", 2, (19, 19))
    return s, E2


@scenario("move-detour", [5], "A wall of resources with one gap between the unit and its target: the path goes "
          "through the gap.",
          random="which first step the unit takes among those of equally short paths, a new draw every tick")
def _():
    s = State(ticks=32)
    s.resources([(10, y) for y in range(0, 20) if y != 15])
    s.order(s.add("BuilderUnit", 1, (5, 5)), move((15, 5)))
    s.add("BuilderUnit", 2, (18, 18))
    return s, E2


@scenario("move-node-cap", [5], "max_pathfind_nodes = 10 and a target behind a wall with its gap 10 rows away: the "
          "capped search still steps towards the target, both units (find_closest_position true and false) walk "
          "straight to the wall and stay there.")
def _():
    s = State(ticks=6, max_pathfind_nodes=10)
    s.resources([(10, y) for y in range(0, 20) if y != 15])
    s.order(s.add("BuilderUnit", 1, (5, 5)), move((15, 5), closest=True))
    s.order(s.add("BuilderUnit", 1, (5, 3)), move((15, 3), closest=False))
    s.add("BuilderUnit", 2, (18, 18))
    return s, E2


@scenario("move-train", [1, 5], "Three units in a column all walk the same way. Pathfinding sees every entity, own "
          "units included, at its cell at the start of the tick: the front unit steps forward, the two behind it "
          "step sideways around the unit ahead.",
          random="which way each unit behind steps around the unit ahead")
def _():
    s = State(ticks=3)
    for y in (5, 6, 7):
        s.order(s.add("MeleeUnit", 1, (5, y)), move((5, 15)))
    s.add("BuilderUnit", 2, (18, 18))
    return s, E2


@scenario("move-swap", [1, 5], "Two adjacent units are sent to each other's cell: neither moves.")
def _():
    s = State(ticks=3)
    a = s.add("MeleeUnit", 1, (5, 5))
    c = s.add("MeleeUnit", 1, (6, 5))
    s.order(a, move((6, 5)))
    s.order(c, move((5, 5)))
    s.add("BuilderUnit", 2, (18, 18))
    return s, E2


@scenario("move-conflict", [1, 5], "Two units step into the same free cell in the same tick: one of them gets it.",
          random="which of the two units (ids 3 and 4) takes the cell (10, 10)")
def _():
    s = State(ticks=2)
    s.order(s.add("MeleeUnit", 1, (9, 10)), move((10, 10), closest=False))
    s.order(s.add("MeleeUnit", 1, (11, 10)), move((10, 10), closest=False))
    s.add("BuilderUnit", 2, (18, 18))
    return s, E2


@scenario("move-into-leaving-cell", [1, 5], "A unit's target is the cell of the unit in front of it, which walks "
          "away in the same tick: movement runs in steps, the leaver moves first and the follower steps into the "
          "freed cell in the same tick.")
def _():
    s = State(ticks=2)
    a = s.add("MeleeUnit", 1, (5, 5))
    c = s.add("MeleeUnit", 1, (5, 6))
    s.order(a, move((5, 6)))          # adjacent target: remembered, no pathfinding
    s.order(c, move((5, 10)))
    s.add("BuilderUnit", 2, (18, 18))
    return s, E2


@scenario("move-break-through", [5], "A wall across the map at x = 10: player 2's Walls in rows 0..9, resources in "
          "rows 10..19. With break_through, a melee unit (row 4) walks up to the wall, destroys the Wall in its way "
          "(10 ticks, +10 destroy score) and walks through; a builder (row 14) mines its way into the resources. "
          "Without break_through (row 7) the unit waits at the reachable cell closest to its target, and takes the "
          "breach once it is free of units (pathfinding sees units where they stand at the start of the tick).",
          random="the order of the attacks (the events list), and the choice among equally short paths")
def _():
    s = State(ticks=24)
    for y in range(10):
        s.add("Wall", 2, (10, y))
    s.resources([(10, y) for y in range(10, 20)])
    s.order(s.add("MeleeUnit", 1, (5, 4)), move((15, 4), break_through=True))
    s.order(s.add("MeleeUnit", 1, (5, 7)), move((15, 7), break_through=False))
    s.order(s.add("BuilderUnit", 1, (5, 14)), move((15, 14), break_through=True))
    return s, E2


@scenario("move-onto-enemy", [4, 5], "Melee units with only a move order whose target is an enemy builder's cell: "
          "the one next to it attacks it at once, the one three cells away walks up and then attacks (the next cell "
          "holds an enemy); both kill their builder and score its destroy score, then step into the cell.")
def _():
    s = State(ticks=5)
    s.add("BuilderUnit", 2, (6, 5))
    s.add("BuilderUnit", 2, (8, 12))
    s.order(s.add("MeleeUnit", 1, (5, 5)), move((6, 5)))
    s.order(s.add("MeleeUnit", 1, (5, 12)), move((8, 12)))
    return s, E2


# Attacks ---------------------------------------------------------------------------------------------------------
@scenario("attack-melee", [4], "A melee unit (5 damage) hits an adjacent builder (10 hp) for two ticks: removed "
          "at the end of the tick it reaches 0, destroy score 100 to the attacker.")
def _():
    s = State(players=2, ticks=4)
    m = s.add("MeleeUnit", 1, (5, 5))
    b = s.add("BuilderUnit", 2, (6, 5))
    s.add("BuilderUnit", 2, (18, 18))
    s.order(m, attack(b))
    return s, E2


@scenario("attack-range-metric", [3, 4], "Ranged units (range 5) against targets at Manhattan distance 5 (straight "
          "and diagonal) and 6: the range is Manhattan, inclusive.",
          random="the order of the attacks (the events list)")
def _():
    s = State(ticks=2)
    pairs = [((2, 2), (7, 2)), ((2, 6), (5, 8)), ((2, 12), (8, 12)), ((2, 16), (6, 18))]   # d = 5, 5, 6, 6
    for src, dst in pairs:
        t = s.add("MeleeUnit", 2, dst)
        s.order(s.add("RangedUnit", 1, src), attack(t))
    return s, E2


@scenario("attack-range-to-building", [3, 4], "A ranged unit 5 cells from the nearest cell of a 5x5 base (9 from "
          "its corner) and another 6 from the nearest cell: the distance to a building is to its nearest cell.",
          random="the order of the attacks (the events list)")
def _():
    s = State(ticks=2)
    base = s.add("MeleeBase", 2, (10, 10))         # cells 10..14 x 10..14
    s.order(s.add("RangedUnit", 1, (14, 5)), attack(base))     # nearest cell (14, 10): 5
    s.order(s.add("RangedUnit", 1, (14, 4)), attack(base))     # nearest cell (14, 10): 6 -> no
    s.order(s.add("RangedUnit", 1, (8, 15)), attack(base))     # nearest cell (10, 14): 2 + 1 = 3
    s.order(s.add("RangedUnit", 1, (4, 12)), attack(base))     # nearest cell (10, 12): 6 -> no
    return s, E2


@scenario("attack-out-of-range-with-move", [4], "A melee unit ordered to attack a target 4 cells away, with and "
          "without a move action in the same EntityAction: an out-of-range attack does nothing by itself; the move "
          "is done instead.")
def _():
    s = State(ticks=4)
    t1 = s.add("BuilderUnit", 2, (10, 5))
    t2 = s.add("BuilderUnit", 2, (10, 12))
    s.order(s.add("MeleeUnit", 1, (6, 5)), attack(t1))
    s.order(s.add("MeleeUnit", 1, (6, 12)), combine(attack(t2), move((10, 12))))
    return s, E2


@scenario("attack-auto", [4], "Melee units with auto_attack: one with pathfind_range 10 and an enemy 6 cells away "
          "(walks there and hits), one with pathfind_range 3 (does nothing), one with valid_targets [Wall] only.",
          random="the choice among equally short paths while a unit hunts")
def _():
    s = State(ticks=10)
    s.add("BuilderUnit", 2, (10, 3))
    s.add("BuilderUnit", 2, (10, 10))
    s.add("BuilderUnit", 2, (10, 17))
    s.order(s.add("MeleeUnit", 1, (4, 3)), attack(auto_range=10))
    s.order(s.add("MeleeUnit", 1, (4, 10)), attack(auto_range=3))
    s.order(s.add("MeleeUnit", 1, (4, 17)), attack(auto_range=10, valid=["Wall"]))
    return s, E2


@scenario("attack-turret-idle", [4], "Turrets and units without any action next to enemies: nothing attacks on its "
          "own; a turret with an auto_attack does.")
def _():
    s = State(ticks=3)
    s.add("Turret", 1, (5, 5))                     # cells 5..6 x 5..6, enemy at distance 2: no action
    s.add("MeleeUnit", 2, (8, 5))
    s.order(s.add("Turret", 1, (5, 12)), attack(auto_range=0))
    s.add("MeleeUnit", 2, (8, 12))
    s.add("MeleeUnit", 1, (15, 15))                # no action, enemy adjacent
    s.add("MeleeUnit", 2, (16, 15))
    return s, E2


@scenario("attack-own", [4], "A melee unit ordered to attack its own player's builder: friendly fire works, the "
          "builder dies in two ticks, nobody scores.")
def _():
    s = State(ticks=2)
    b = s.add("BuilderUnit", 1, (6, 5))
    s.order(s.add("MeleeUnit", 1, (5, 5)), attack(b))
    s.add("BuilderUnit", 2, (18, 18))
    return s, E2


@scenario("attack-overkill", [1, 4], "Two melee units of player 1 and one of player 3 hit a 5 hp builder of player "
          "2 in the same tick: one killing blow, the destroy score goes to the first hit in the random order.",
          random="the attack order (the events list) and so who gets the 100 destroy score")
def _():
    s = State(players=3, ticks=2)
    b = s.add("BuilderUnit", 2, (10, 10), health=5)
    s.add("BuilderUnit", 2, (18, 18))
    for pos, pl in (((9, 10), 1), ((11, 10), 1), ((10, 11), 3)):
        s.order(s.add("MeleeUnit", pl, pos), attack(b))
    return s, ["Empty"] * 3


@scenario("attack-auto-choice", [4], "Ranged units with auto_attack (pathfind_range 5) and several enemies in attack "
          "range: one has a melee unit 4 away and a builder 3 away, the other two builders 3 away. Every tick each "
          "picks one of the enemies in range at random, whatever the distance (40 seeds give 14 of the 16 "
          "combinations).", random="which enemy in range each ranged unit hits, every tick")
def _():
    s = State(ticks=2)
    s.add("MeleeUnit", 2, (9, 5))
    s.add("BuilderUnit", 2, (5, 8))
    s.order(s.add("RangedUnit", 1, (5, 5)), attack(auto_range=5))
    s.add("BuilderUnit", 2, (15, 18))
    s.add("BuilderUnit", 2, (18, 15))
    s.order(s.add("RangedUnit", 1, (15, 15)), attack(auto_range=5))
    return s, E2


@scenario("attack-turret-range", [3, 4], "Turrets (2x2) with an explicit target: the one 5 from the turret's "
          "nearest cell (7 from its corner) is hit, the one 6 from it is not: range counts from the attacker's "
          "nearest cell too.")
def _():
    s = State(ticks=1)
    e1 = s.add("MeleeUnit", 2, (11, 6))
    s.order(s.add("Turret", 1, (5, 5)), attack(e1))            # cells 5..6 x 5..6, nearest (6, 6): 5
    e2 = s.add("MeleeUnit", 2, (12, 13))
    s.order(s.add("Turret", 1, (5, 12)), attack(e2))           # cells 5..6 x 12..13, nearest (6, 13): 6
    return s, E2


@scenario("attack-auto-range-metric", [4], "auto_attack pathfind_range 8: an enemy 6 away in Manhattan distance but "
          "behind a wall of resources (a walk of about 20) is ignored; an enemy 9 away on an open field is hunted, "
          "as a cell next to it is 8 steps away. pathfind_range is a walking budget to a cell from which the "
          "target is in attack range.")
def _():
    s = State(ticks=4)
    s.resources([(8, y) for y in range(0, 13)])
    s.add("BuilderUnit", 2, (11, 5))
    s.order(s.add("MeleeUnit", 1, (5, 5)), attack(auto_range=8))
    s.add("BuilderUnit", 2, (14, 17))
    s.order(s.add("MeleeUnit", 1, (5, 17)), attack(auto_range=8))
    return s, E2


@scenario("attack-auto-empty-valid", [4, 11], "A builder with auto_attack and an empty valid_targets next to resources "
          "only: it does nothing. An empty list means every enemy type; resources must be asked for.")
def _():
    s, a, b = two_builders(3)
    s.resources([(3, 2), (2, 4)])
    s.order(a, attack(auto_range=10))
    return s, E2


@scenario("attack-auto-valid-targets", [4], "auto_attack with valid_targets [MeleeUnit]: the builder 2 away is "
          "ignored, the melee unit 4 away is hit.")
def _():
    s = State(ticks=2)
    s.add("BuilderUnit", 2, (5, 7))
    s.add("MeleeUnit", 2, (9, 5))
    s.order(s.add("RangedUnit", 1, (5, 5)), attack(auto_range=5, valid=["MeleeUnit"]))
    return s, E2


# Mining -----------------------------------------------------------------------------------------------------------
@scenario("mine-resource", [11], "A builder mines an adjacent resource (30 hp): -1 hp, +1 resource, +1 score per tick; "
          "the resource is removed in the tick it reaches 0, no destroy score.")
def _():
    s, a, b = two_builders(32)
    r = s.add("Resource", None, (3, 2))
    s.order(a, attack(r))
    return s, E2


@scenario("mine-last-hp", [11], "Two builders hit a resource with 1 hp in the same tick: 1 resource and 1 score in "
          "all (only health above 0 is collected), both hits are events.",
          random="the order of the two hits (the events list)")
def _():
    s = State(ticks=2)
    r = s.add("Resource", None, (10, 10), health=1)
    for pos in ((9, 10), (11, 10)):
        s.order(s.add("BuilderUnit", 1, pos), attack(r))
    s.add("BuilderUnit", 2, (18, 18))
    return s, E2


@scenario("mine-auto", [4, 11], "A builder with auto_attack (pathfind_range 20, valid_targets [Resource]), as in the "
          "starter strategy: walks to the nearest resource and mines it.")
def _():
    s, a, b = two_builders(12)
    s.resources([(8, 2), (2, 9)])
    s.order(a, attack(auto_range=20, valid=["Resource"]))
    return s, E2


# Building ---------------------------------------------------------------------------------------------------------
@scenario("build-house", [6], "A builder next to a free 3x3 site with 100 resource builds a House: it appears with "
          "init_health 5, inactive, resource -50, no score yet.")
def _():
    s, a, b = two_builders(3, resource=100)
    s.order(a, build("House", (3, 2)))
    return s, E2


@scenario("build-house-placement", [6], "Four Houses that must not be built, each by another player with money "
          "enough: the site touches the builder only at a corner, overlaps a resource, lies partly off the map, or "
          "is one cell away from the builder. None is built.")
def _():
    s = State(players=4, ticks=2, resource=200)
    s.order(s.add("BuilderUnit", 1, (2, 2)), build("House", (3, 3)))      # touches only at a corner
    s.add("Resource", None, (14, 4))
    s.order(s.add("BuilderUnit", 2, (12, 2)), build("House", (13, 2)))    # overlaps the resource
    s.order(s.add("BuilderUnit", 3, (18, 12)), build("House", (19, 12)))  # off the map
    s.order(s.add("BuilderUnit", 4, (2, 12)), build("House", (4, 12)))    # one free cell between
    return s, ["Empty"] * 4


@scenario("build-house-money", [6], "Two builders of one player each try a House (50) with 60 resource in the same "
          "tick: only one is built.", random="which builder builds when only one can")
def _():
    s = State(ticks=2, resource=60)
    s.order(s.add("BuilderUnit", 1, (2, 2)), build("House", (3, 2)))
    s.order(s.add("BuilderUnit", 1, (2, 10)), build("House", (3, 10)))
    s.add("BuilderUnit", 2, (18, 18))
    return s, E2


@scenario("build-unit", [6], "An active BuilderBase builds a BuilderUnit next to it: the unit appears active with "
          "full health in the same tick; cost = initial_cost + the player's units of that type.")
def _():
    s = State(ticks=3, resource=100)
    base = s.add("BuilderBase", 1, (2, 2))         # cells 2..6
    s.add("BuilderUnit", 1, (10, 10))
    s.order(base, build("BuilderUnit", (7, 2)))
    s.add("BuilderUnit", 2, (18, 18))
    return s, E2


@scenario("build-unit-population", [6], "A BuilderBase (provides 5) with 5 units already: no room for a sixth. An "
          "inactive House would add 5 more but inactive buildings provide nothing.")
def _():
    s = State(ticks=2, resource=100)
    base = s.add("BuilderBase", 1, (2, 2))
    s.add("House", 1, (10, 14), health=5, active=False)
    for x in range(10, 15):
        s.add("BuilderUnit", 1, (x, 10))
    s.order(base, build("BuilderUnit", (7, 2)))
    s.add("BuilderUnit", 2, (18, 18))
    return s, E2


@scenario("build-house-cost-count", [6], "Building cost with buildings of the type already there: a second House "
          "costs 50 and a third Wall 10, as the first ones. Only unit costs grow with the count.",
          random="the order of the builds, and so which new entity gets which id")
def _():
    s = State(players=3, ticks=1, resource=100)
    s.add("House", 1, (10, 10))
    s.order(s.add("BuilderUnit", 1, (2, 2)), build("House", (3, 2)))
    s.add("Wall", 2, (15, 0))
    s.add("Wall", 2, (16, 0))
    s.order(s.add("BuilderUnit", 2, (12, 5)), build("Wall", (13, 5)))
    s.add("BuilderUnit", 3, (18, 18))
    return s, ["Empty"] * 3


@scenario("build-unit-population-inactive", [6], "A BuilderBase (provides 5) with 4 active units and 1 inactive "
          "unit: no build, so inactive units use population.")
def _():
    s = State(ticks=1, resource=100)
    base = s.add("BuilderBase", 1, (2, 2))
    for x in range(10, 14):
        s.add("BuilderUnit", 1, (x, 10))
    s.add("BuilderUnit", 1, (14, 10), health=5, active=False)
    s.order(base, build("BuilderUnit", (7, 2)))
    s.add("BuilderUnit", 2, (18, 18))
    return s, E2


@scenario("build-unit-cost-inactive", [6], "An inactive BuilderUnit at full health is on the map: it counts in the "
          "cost (12 = 10 + 2 units), and at the end of the tick it becomes active and scores its build score (10) "
          "next to the new unit's (10).")
def _():
    s = State(ticks=2, resource=100)
    base = s.add("BuilderBase", 1, (2, 2))
    s.add("House", 1, (10, 14))
    s.add("BuilderUnit", 1, (10, 10))
    s.add("BuilderUnit", 1, (12, 10), active=False)
    s.order(base, build("BuilderUnit", (7, 2)))
    s.add("BuilderUnit", 2, (18, 18))
    return s, E2


# Repair and activation ------------------------------------------------------------------------------------------
@scenario("repair-house", [7], "A builder repairs an inactive House from 45 hp: +1 per tick, active and +50 build "
          "score the tick it reaches 50.")
def _():
    s, a, b = two_builders(7)
    h = s.add("House", 1, (3, 2), health=45, active=False)
    s.order(a, repair(h))
    return s, E2


@scenario("repair-unit", [7], "A builder repairs a damaged active melee unit (45/50): +1 per tick up to 50, no "
          "score.")
def _():
    s, a, b = two_builders(7)
    m = s.add("MeleeUnit", 1, (3, 2), health=45)
    s.order(a, repair(m))
    return s, E2


@scenario("repair-two-builders", [7], "Two builders repair one inactive House from 45 hp: +2 per tick, clamped at "
          "50 (45, 47, 49, 50), then active with its build score.")
def _():
    s = State(ticks=4)
    h = s.add("House", 1, (5, 5), health=45, active=False)       # cells 5..7
    s.order(s.add("BuilderUnit", 1, (4, 5)), repair(h))
    s.order(s.add("BuilderUnit", 1, (8, 7)), repair(h))
    s.add("BuilderUnit", 2, (18, 18))
    return s, E2


# The order of the phases within a tick ----------------------------------------------------------------------
@scenario("order-attack-before-move", [1], "A ranged unit hits an enemy 5 away that walks off in the same tick: "
          "attacks come before movement, so the hit lands; from 6 away the next tick, none.")
def _():
    s = State(ticks=3)
    e = s.add("MeleeUnit", 2, (10, 5))
    s.order(e, move((18, 5)))
    s.order(s.add("RangedUnit", 1, (5, 5)), attack(e))
    return s, E2


@scenario("order-build-before-move", [1, 6], "A builder builds a House on a site that one of its player's units "
          "is walking out of: builds come before movement, so the build fails while the unit is still inside at "
          "the start of the tick (ticks 1 and 2) and succeeds at tick 3.")
def _():
    s = State(ticks=3, resource=100)
    s.order(s.add("BuilderUnit", 1, (2, 2)), build("House", (3, 2)))
    s.order(s.add("MeleeUnit", 1, (4, 3)), move((4, 12)))     # inside the site, walking out
    s.add("BuilderUnit", 2, (18, 18))
    return s, E2


@scenario("order-attack-blocks-build", [1, 6], "A builder with an attack order (an adjacent resource) and a build "
          "order in the same EntityAction: it mines, and does not build while the attack succeeds.")
def _():
    s = State(ticks=2, resource=100)
    r = s.add("Resource", None, (1, 2))
    s.order(s.add("BuilderUnit", 1, (2, 2)), combine(attack(r), build("House", (3, 2))))
    s.add("BuilderUnit", 2, (18, 18))
    return s, E2


# Fog of war, passive income ----------------------------------------------------------------------------------
@scenario("view-fog", [8], "Fog of war on, both seats puppets recording their views: a builder (sight 10) with "
          "enemies 9, 10 and 11 away, an enemy House whose nearest cell is 11 away, resources 5, 9 and 12 away; "
          "scores and resources differ per player. Sight is Manhattan and inclusive: seat 0 sees the enemies 9 and "
          "10 away and the resources 5 and 9 away, not what is 11 or 12 away; the players list shows everyone's "
          "score and resource. Seat 1's view is rotated by 180 degrees (see view-frames).", record_views=True)
def _():
    s = State(ticks=2, fog=True, score=[11, 22], resource=[33, 44])
    s.add("BuilderUnit", 1, (5, 5))
    s.add("MeleeUnit", 2, (5, 14))
    s.add("MeleeUnit", 2, (5, 15))
    s.add("MeleeUnit", 2, (5, 16))
    s.add("House", 2, (16, 3))          # cells 16..18 x 3..5
    s.resources([(5, 0), (14, 5), (12, 10)])
    return s, [Puppet(), Puppet()]


@scenario("view-frames", [8], "Four seats, each with a builder, a Turret and a second builder placed as the mirror "
          "of seat 0's; each seat, in the frame it is shown, moves the first builder 5 cells up and builds a House "
          "to the right of the second. Every PlayerView is rotated so the seat sees its corner at (0, 0): seat 1 "
          "by 180 degrees, seat 2 by 90 ((x, y) -> (y, N - x - size)), seat 3 by 270 ((x, y) -> (N - y - size, x)); "
          "move targets and build positions are turned back on receipt (with the built entity's size), and the "
          "replay stores them in global coordinates.", record_views=True,
          random="the order of the builds, and so which House gets which id")
def _():
    n = 20
    s = State(map_size=n, players=4, ticks=3, resource=100)
    mirrored = {0: (False, False), 1: (True, True), 2: (True, False), 3: (False, True)}

    def place(seat, x, y, size=1):
        mx, my = mirrored[seat]
        return (n - x - size if mx else x, n - y - size if my else y)

    script = {}
    for seat in range(4):
        walker = s.add("BuilderUnit", seat + 1, place(seat, 2, 3))
        s.add("Turret", seat + 1, place(seat, 8, 8, 2))
        builder = s.add("BuilderUnit", seat + 1, place(seat, 2, 10))
        seen_w = to_view(seat, place(seat, 2, 3), 1, n)
        seen_b = to_view(seat, place(seat, 2, 10), 1, n)
        script[seat] = {0: {walker: move((seen_w.x, seen_w.y + 5)),
                            builder: build("House", (seen_b.x + 1, seen_b.y))}}
    return s, [Puppet(script[seat]) for seat in range(4)]


@scenario("view-no-fog", [8], "Fog of war off, both seats puppets: each view holds every entity, rotated for seat "
          "1.", record_views=True)
def _():
    s = State(ticks=1)
    s.add("BuilderUnit", 1, (2, 2))
    s.add("MeleeUnit", 2, (17, 17))
    s.add("House", 2, (10, 3))
    s.add("Resource", None, (12, 15))
    return s, [Puppet(), Puppet()]


@scenario("view-fog-inactive", [8], "Fog of war on: player 1's only entities are an inactive House (sight 5) and "
          "a builder far away; the enemy unit 3 from the House is seen: inactive entities give sight.",
          record_views=True)
def _():
    s = State(ticks=1, fog=True)
    s.add("House", 1, (3, 3), health=5, active=False)          # cells 3..5
    s.add("BuilderUnit", 1, (2, 18))
    s.add("MeleeUnit", 2, (8, 5))                              # 3 from (5, 5), 13+ from the builder
    return s, [Puppet(), "Empty"]


@scenario("view-fog-building-edge", [8, 3], "Fog of war on: a builder at (5, 5) (sight 10) and enemy buildings with "
          "only some of their cells in sight. A House whose nearest cell is 10 away (its position 12) and a "
          "MeleeBase whose nearest cell is 10 away (its position 14) are in the view, a House whose nearest cell is "
          "12 away is not: an entity is shown when any of its cells is in sight.", record_views=True)
def _():
    s = State(ticks=1, fog=True)
    s.add("BuilderUnit", 1, (5, 5))
    s.add("House", 2, (15, 3))           # cells 15..17 x 3..5: (15, 5) is 10 away, (15, 3) 12
    s.add("House", 2, (16, 6))           # cells 16..18 x 6..8: (16, 6) is 12 away
    s.add("MeleeBase", 2, (0, 14))       # cells 0..4 x 14..18: (4, 14) is 10 away, (0, 14) 14
    return s, [Puppet(), "Empty"]


@scenario("view-order", [8], "The state file lists the entities by decreasing id; at tick 0 a melee unit kills a "
          "builder and a base builds a unit (a new id). Every view lists the entities by increasing id, whatever the "
          "order of the file, after a death and a birth alike.", record_views=True)
def _():
    s = State(ticks=3, resource=100)
    base = s.add("MeleeBase", 1, (2, 2))
    s.order(base, build("MeleeUnit", (7, 2)))
    killer = s.add("MeleeUnit", 1, (10, 10))
    victim = s.add("BuilderUnit", 2, (10, 11), health=5)
    s.add("BuilderUnit", 2, (17, 17))
    s.add("BuilderUnit", 2, (15, 17))
    s.order(killer, attack(victim))
    m = s.model
    m.entities = {i: m.entities[i] for i in sorted(m.entities, reverse=True)}
    return m, [Puppet(), Puppet()]


@scenario("view-no-entities", [8, 12], "Four seats under fog of war: player 4 owns nothing from the start, player 3 "
          "loses its only unit at tick 0, players 1 and 2 keep the game going. Seats 2 and 3 are still sent a view "
          "every tick, with no entity in it, until the game ends and they get Finish. (Without fog of war such a seat "
          "sees every entity, as any seat does.)", record_views=True)
def _():
    s = State(players=4, ticks=3, fog=True)
    killer = s.add("MeleeUnit", 1, (5, 5))
    s.add("BuilderUnit", 1, (2, 2))
    s.add("BuilderUnit", 2, (17, 17))
    victim = s.add("BuilderUnit", 3, (5, 6), health=5)
    s.order(killer, attack(victim))
    return s, [Puppet() for _ in range(4)]


@scenario("passive-income", [10], "passive_income 3 (0 in every preset): every player's resource grows by 3 per "
          "tick, the score does not.")
def _():
    s, a, b = two_builders(3, passive_income=3)
    return s, E2


# Game end ----------------------------------------------------------------------------------------------------------
@scenario("end-finals-bonus", [12], "Two players: player 1 kills player 2's last entity (player 2 has 777 score): "
          "the game ends that tick and player 1 gets the destroy score plus player 2's score.")
def _():
    s = State(ticks=6, score=[0, 777])
    b = s.add("BuilderUnit", 2, (6, 5))
    s.order(s.add("MeleeUnit", 1, (5, 5)), attack(b))
    return s, E2


@scenario("end-elimination-3p", [12], "Three players: player 1 kills player 3's last entity at tick 2 (the game goes "
          "on) and player 2's at tick 4 (the game ends); scores start at 0, 500, 300. No survivor bonus with three "
          "players: player 1 ends with its two destroy scores only.",
          random="the order of the attacks (the events list)")
def _():
    s = State(players=3, ticks=8, score=[0, 500, 300])
    b3 = s.add("BuilderUnit", 3, (6, 5))
    b2 = s.add("BuilderUnit", 2, (6, 12), health=20)
    s.order(s.add("MeleeUnit", 1, (5, 5)), attack(b3))
    s.order(s.add("MeleeUnit", 1, (5, 12)), attack(b2))
    return s, ["Empty"] * 3


@scenario("end-mutual", [1, 12], "Two players whose last units (5 hp each) attack each other: the unit hit first "
          "still attacks (health is checked on the target, not the attacker), both score the destroy score, nobody "
          "is left and the game ends at tick 1 without a bonus.",
          random="the order of the two attacks (the events list)")
def _():
    s = State(ticks=4, score=[100, 200])
    a = s.add("MeleeUnit", 1, (5, 5), health=5)
    b = s.add("MeleeUnit", 2, (6, 5), health=5)
    s.order(a, attack(b))
    s.order(b, attack(a))
    return s, E2


@scenario("end-over-at-start", [12], "Only player 1 owns entities at the start: the official engine writes no "
          "results.json and exits 0.")
def _():
    s = State(ticks=4)
    s.add("BuilderUnit", 1, (2, 2))
    return s, E2


# Finer points, settled while writing rules.py ---------------------------------------------------------------
@scenario("attack-named-before-auto", [4], "A ranged unit whose attack order names a target in range and also has "
          "an auto_attack, with another enemy in range: it always hits the named target.")
def _():
    s = State(ticks=2)
    r, t = s.add("RangedUnit", 1, (5, 5)), s.add("MeleeUnit", 2, (8, 5))
    s.add("MeleeUnit", 2, (5, 8))
    s.order(r, attack(t, auto_range=0))
    return s, E2


@scenario("attack-named-out-of-range-auto", [4], "Two ranged units whose attack order names a target out of range "
          "(one far away, one an id that does not exist) and also has an auto_attack: each hits the enemy within "
          "its range instead.")
def _():
    s = State(ticks=2)
    a, b = s.add("RangedUnit", 1, (2, 2)), s.add("RangedUnit", 1, (15, 15))
    s.add("MeleeUnit", 2, (2, 5))
    far = s.add("MeleeUnit", 2, (15, 12))
    s.order(a, attack(far, auto_range=0))
    s.order(b, attack(999, auto_range=0))
    return s, E2


@scenario("attack-named-out-of-range-hunts", [4], "A melee unit whose attack order names a target out of range and "
          "has an auto_attack (pathfind_range 10), with an enemy builder 6 cells away: it hunts the builder, as an "
          "auto_attack alone would.")
def _():
    s = State(ticks=3)
    m, far = s.add("MeleeUnit", 1, (5, 10)), s.add("MeleeUnit", 2, (18, 1))
    s.add("BuilderUnit", 2, (11, 10))
    s.order(m, attack(far, auto_range=10))
    return s, E2


@scenario("attack-auto-ranged-reach", [4], "Ranged units (attack range 5) with an auto_attack, each with an enemy 8 "
          "cells away on an open field: the one with pathfind_range 7 hunts it, the one with 6 does not. The reach is "
          "counted to a cell next to the enemy (7 steps), not to a cell within attack range (3 steps).")
def _():
    s = State(map_size=30, ticks=3)
    a, b = s.add("RangedUnit", 1, (5, 5)), s.add("RangedUnit", 1, (5, 25))
    s.add("BuilderUnit", 2, (13, 5))
    s.add("BuilderUnit", 2, (13, 25))
    s.order(a, attack(auto_range=7))
    s.order(b, attack(auto_range=6))
    return s, E2


@scenario("attack-auto-over-move", [4, 5], "A melee unit with an auto_attack (pathfind_range 10) and a move order "
          "the other way, an enemy builder 6 cells away: it hunts the builder and ignores the move order.")
def _():
    s = State(ticks=3)
    m = s.add("MeleeUnit", 1, (5, 10))
    s.add("BuilderUnit", 2, (11, 10))
    s.order(m, combine(attack(auto_range=10), move((0, 10))))
    return s, E2


@scenario("attack-auto-dead-target", [1, 4], "Two ranged units auto-attack, with a 5 hp builder and a melee unit in "
          "range of both: an enemy already at 0 health in the tick can still be picked, so both may hit the "
          "builder (one killing blow, 100 destroy score) or split.",
          random="which enemy each unit hits, and the attack order")
def _():
    s = State(ticks=2)
    a, b = s.add("RangedUnit", 1, (5, 5)), s.add("RangedUnit", 1, (5, 7))
    s.add("BuilderUnit", 2, (8, 6), health=5)
    s.add("MeleeUnit", 2, (9, 6))
    s.order(a, attack(auto_range=0))
    s.order(b, attack(auto_range=0))
    return s, E2


@scenario("repair-enemy", [7], "A builder repairs another player's inactive House: a repair is not limited to the "
          "builder's own player (+1 per tick, no score for anyone).")
def _():
    s, a, b = two_builders(3)
    s.order(a, repair(s.add("House", 2, (3, 2), health=45, active=False)))
    return s, E2


@scenario("repair-full-target", [7], "Two builders ordered to repair a full-health House and to move: the one next "
          "to the House stays (repairing a target at full health still takes its turn), the one too far from it "
          "walks.")
def _():
    s, a, b = two_builders(3)
    h = s.add("House", 1, (3, 2))
    s.order(a, combine(repair(h), move((2, 12))))
    s.order(s.add("BuilderUnit", 1, (10, 2)), combine(repair(h), move((10, 12))))
    return s, E2


@scenario("repair-dead-target", [1, 7], "A builder repairs its player's melee unit (5 hp) while an enemy kills it "
          "in the same tick: attacks come first and only a target with health above 0 is repaired, so the unit "
          "dies; the builder, whose repair failed, follows its move order.")
def _():
    s = State(ticks=2)
    m, e = s.add("MeleeUnit", 1, (10, 10), health=5), s.add("MeleeUnit", 2, (11, 10))
    s.order(e, attack(m))
    s.order(s.add("BuilderUnit", 1, (9, 10)), combine(repair(m), move((9, 15))))
    s.add("BuilderUnit", 2, (18, 18))
    return s, E2


@scenario("order-attack-blocks-repair", [1, 7], "A builder with an attack order (an adjacent resource) and a repair "
          "order (a damaged melee unit next to it): it mines and does not repair.")
def _():
    s = State(ticks=2)
    b, r = s.add("BuilderUnit", 1, (5, 5)), s.add("Resource", None, (6, 5))
    s.order(b, combine(attack(r), repair(s.add("MeleeUnit", 1, (4, 5), health=40))))
    s.add("BuilderUnit", 2, (18, 18))
    return s, E2


@scenario("order-build-before-repair", [1, 7], "A builder builds a House while another, next to the site, repairs "
          "the id the House is going to get: repairs come after builds, so the House ends its first tick at 6 hp.")
def _():
    s = State(ticks=2, resource=100)
    a, c = s.add("BuilderUnit", 1, (4, 5)), s.add("BuilderUnit", 1, (8, 6))
    s.add("BuilderUnit", 2, (18, 18))
    s.order(a, build("House", (5, 5)))
    s.order(c, repair(s.model.next_id))
    return s, E2


@scenario("order-repair-before-move", [1, 7], "A builder repairs a damaged melee unit that walks away in the same "
          "tick: repairs come before movement, so the unit gets +1 that tick and none afterwards.")
def _():
    s = State(ticks=3)
    b, m = s.add("BuilderUnit", 1, (5, 5)), s.add("MeleeUnit", 1, (6, 5), health=40)
    s.order(b, repair(m))
    s.order(m, move((15, 5)))
    s.add("BuilderUnit", 2, (18, 18))
    return s, E2


@scenario("build-same-site", [6], "Two builders of one player build a House on the same site in the same tick: one "
          "House; the other attempt fails on the site the first one took. Both attempts use up an id, then and "
          "every tick after (next_id 6, 8, 10).")
def _():
    s = State(ticks=2, resource=200)
    a, c = s.add("BuilderUnit", 1, (4, 5)), s.add("BuilderUnit", 1, (8, 6))
    s.add("BuilderUnit", 2, (18, 18))
    s.order(a, build("House", (5, 5)))
    s.order(c, build("House", (5, 5)))
    return s, E2


@scenario("build-then-move", [1, 6], "A builder with a build and a move order does not move in the tick its build "
          "succeeds, and walks once the build fails (the site is taken); another one, without money, walks at "
          "once. Every attempt uses up an id, so the House is id 5 or 6.",
          random="which builder's attempt comes first, and so the House's id")
def _():
    s = State(ticks=2, resource=[100, 0])
    s.order(s.add("BuilderUnit", 1, (2, 2)), combine(build("House", (3, 2)), move((2, 12))))
    s.order(s.add("BuilderUnit", 2, (12, 2)), combine(build("House", (13, 2)), move((12, 12))))
    return s, E2


@scenario("build-no-attempt", [6], "Build orders that are not attempts: a melee unit (no build properties), a House, "
          "an inactive BuilderBase, a builder ordered to build a BuilderUnit (not among its options). Nothing is "
          "built and next_id stays: only an order for a type the entity may build uses up an id.")
def _():
    s = State(ticks=2, resource=100)
    s.order(s.add("MeleeUnit", 1, (2, 2)), build("House", (3, 2)))
    s.order(s.add("House", 1, (2, 8)), build("House", (5, 8)))
    s.order(s.add("BuilderBase", 1, (10, 2), health=100, active=False), build("BuilderUnit", (15, 2)))
    s.order(s.add("BuilderUnit", 1, (2, 14)), build("BuilderUnit", (3, 14)))
    s.add("House", 1, (10, 10))
    s.add("BuilderUnit", 2, (18, 18))
    return s, E2


@scenario("passive-income-after-builds", [1, 10], "passive_income 3 and 47 resource: a House (50) is not built in "
          "the first tick, as the income comes after the builds, but in the second (resource 47, 50, 3).")
def _():
    s = State(ticks=3, resource=47, passive_income=3)
    s.order(s.add("BuilderUnit", 1, (2, 2)), build("House", (3, 2)))
    s.add("BuilderUnit", 2, (18, 18))
    return s, E2


@scenario("move-dying-unit", [1, 5], "A unit killed in the tick still moves on its order, and the unit that wants "
          "its cell steps in behind it in the same tick.")
def _():
    s = State(ticks=2)
    d, f = s.add("MeleeUnit", 1, (5, 5), health=5), s.add("MeleeUnit", 1, (5, 4))
    e = s.add("MeleeUnit", 2, (4, 5))
    s.order(d, move((5, 10)))
    s.order(f, move((5, 5)))
    s.order(e, attack(d))
    return s, E2


@scenario("move-dead-blocks", [1, 5], "A unit killed in the tick that does not move keeps its cell until the end of "
          "the tick: the unit waiting for the cell gets in the next tick.")
def _():
    s = State(ticks=2)
    d, f = s.add("MeleeUnit", 1, (5, 5), health=5), s.add("MeleeUnit", 1, (5, 4))
    e = s.add("MeleeUnit", 2, (4, 5))
    s.order(f, move((5, 5)))
    s.order(e, attack(d))
    return s, E2


@scenario("inactive-does-nothing", [4, 6], "An inactive builder (5/10 hp) with a move order and an inactive Turret "
          "(50/100) with an attack order on an enemy in range: neither acts.")
def _():
    s = State(ticks=2)
    s.order(s.add("BuilderUnit", 1, (2, 2), health=5, active=False), move((2, 10)))
    t = s.add("Turret", 1, (10, 10), health=50, active=False)
    s.order(t, attack(s.add("MeleeUnit", 2, (12, 10))))
    return s, E2


@scenario("end-inactive-only", [12], "Player 2's only entity is an inactive House: the game goes on, an inactive "
          "entity keeps its player in the game.")
def _():
    s = State(ticks=3)
    s.add("BuilderUnit", 1, (2, 2))
    s.add("House", 2, (15, 15), health=5, active=False)
    return s, E2


def _breach(unit: str, hp: int, gap: int) -> tuple:
    """A unit next to a wall of resources (x = 10 on a 30x30 map), sent across it with break_through: the cell in
    front of it holds a resource of `hp`, the wall's gap is `gap` rows away (a detour of 2 x gap steps)."""
    s = State(map_size=30, ticks=1)
    u = s.add(unit, 1, (9, 10))
    for y in range(30):
        if y != 10 + gap:
            s.add("Resource", None, (10, y), health=hp if y == 10 else 30)
    s.add("BuilderUnit", 2, (28, 28))
    s.order(u, move((20, 10), break_through=True))
    return s, E2


@scenario("move-break-through-cost-tie", [5], "With break_through, entering a blocker's cell costs the hits it takes "
          "to destroy it, ceil(health / damage), in place of 1: a melee unit (5 damage) before a 12 hp resource "
          "(3 hits) with the wall's gap 1 row away (2 more steps) finds both ways equally short and takes either.",
          random="whether the unit attacks the resource or steps towards the gap")
def _():
    return _breach("MeleeUnit", 12, 1)


@scenario("move-break-through-cost-chop", [5], "A builder (1 damage) before a 10 hp resource (10 hits) with the "
          "wall's gap 6 rows away (12 more steps): breaking through is shorter, it mines the resource.")
def _():
    return _breach("BuilderUnit", 10, 6)


@scenario("move-break-through-cost-walk", [5], "A builder (1 damage) before a 10 hp resource (10 hits) with the "
          "wall's gap 4 rows away (8 more steps): the detour is shorter, it walks towards the gap.")
def _():
    return _breach("BuilderUnit", 10, 4)


# Pathfinding as the official engine runs it (read off its machine code in step 4) ------------------------------------
@scenario("move-fog-unseen-wall", [5, 8], "Fog of war on: a path search counts only the entities its player sees, "
          "and takes every other cell for free. A builder (sight 10) has a long resource divider ahead; the way round "
          "it to the north is closed by a wall 20 cells off. Seeing only the near end of the divider, it goes round "
          "either way (with the whole map in sight only the south is open).",
          random="whether the unit goes round the divider by the north or by the south")
def _():
    s = State(map_size=40, ticks=4, fog=True)
    s.resources([(x, 20) for x in range(6, 31)] + [(25, y) for y in range(20)])
    s.order(s.add("BuilderUnit", 1, (5, 20)), move((35, 20)))
    s.add("BuilderUnit", 2, (38, 38))
    return s, E2


@scenario("move-node-cap-frontier", [5], "max_pathfind_nodes = 1: the search expands only the start, yet the path "
          "goes to the best cell it put on its heap. A builder with break_through and a resource in front of it on "
          "the way to its target finds that cell the closest to the target and mines it, tick after tick.")
def _():
    s = State(ticks=3, max_pathfind_nodes=1)
    s.add("Resource", None, (6, 10))
    s.order(s.add("BuilderUnit", 1, (5, 10)), move((15, 10), break_through=True))
    s.add("BuilderUnit", 2, (18, 18))
    return s, E2


@scenario("attack-auto-fog-hunt", [4, 8], "Fog of war on: a hunt counts enemies its player does not see, and walks "
          "through what it does not see. A melee unit (sight 10, pathfind_range 14) hunts a House 14 cells away "
          "behind a resource wall across the map 11 cells away: unseen, the wall is no obstacle and the House is in "
          "reach, so the unit steps towards it (with the wall in sight it would stay).")
def _():
    s = State(ticks=1, fog=True)
    s.resources([(13, y) for y in range(20)])
    s.order(s.add("MeleeUnit", 1, (2, 10)), attack(auto_range=14))
    s.add("House", 2, (16, 9))
    return s, E2


# CLI ---------------------------------------------------------------------------------------------------------------
def _select(names: list[str]) -> list[Scenario]:
    if not names:
        return list(SCENARIOS.values())
    out = []
    for n in names:
        matched = [sc for key, sc in SCENARIOS.items() if key == n or (n.endswith("*") and key.startswith(n[:-1]))]
        if not matched:
            raise SystemExit(f"no scenario {n}")
        out += matched
    return out


def show(fx: dict) -> str:
    """A fixture as a human reads it: per tick, what changed."""
    lines = [f"{fx['name']}: {fx['about']}"]
    if fx.get("random"):
        lines.append(f"  random: {fx['random']}")
    exp = fx["expected"]
    lines.append(f"  returncode {exp['returncode']}, results {exp['results']}")
    for line in exp["log"]:
        lines.append(f"  log: {line}")
    for seat, p in (exp.get("puppets") or {}).items():
        lines.append(f"  puppet seat {seat}: views at ticks {p['view_ticks']}, finished={p['finished']}")
    prev = None
    for snap in exp["ticks"]:
        ents = {e["id"]: e for e in snap["entities"]}
        acts = {a["entity_id"]: a["action"] for a in snap["entity_actions"]}
        players = {p["id"]: p for p in snap["players"]}
        parts = []
        if prev is None:
            for e in ents.values():
                parts.append(_ent(e))
        else:
            pents, pacts, pplayers = prev
            for i, e in ents.items():
                if i not in pents:
                    parts.append("+" + _ent(e))
                elif e != pents[i]:
                    parts.append(_ent(e))
            parts += [f"-{i}" for i in pents if i not in ents]
            for i, a in acts.items():
                if pacts.get(i) != a:
                    parts.append(f"act {i}={_act(a)}")
            parts += [f"act -{i}" for i in pacts if i not in acts]
            for i, p in players.items():
                if p != pplayers.get(i):
                    parts.append(f"P{i} score {p['score']} res {p['resource']}")
        if snap["events"]:
            parts.append("ev " + " ".join(f"{a}>{b}" if t == 0 else f"[{t} {a} {b}]" for t, a, b in snap["events"]))
        lines.append(f"  t{snap['tick']:<3} " + "; ".join(parts))
        prev = (ents, acts, players)
    return "\n".join(lines)


def _ent(e: dict) -> str:
    owner = "" if e["player_id"] is None else f"P{e['player_id']} "
    flag = "" if e["active"] else " inactive"
    return f"{e['id']}:{owner}{e['entity_type']}@{e['position']['x']},{e['position']['y']} hp{e['health']}{flag}"


def _act(a: dict) -> str:
    out = []
    if a["attack_action"]:
        at = a["attack_action"]
        out.append(f"attack({at['target']}, auto={at['auto_attack']})")
    if a["build_action"]:
        b = a["build_action"]
        out.append(f"build({b['entity_type']}@{b['position']['x']},{b['position']['y']})")
    if a["repair_action"]:
        out.append(f"repair({a['repair_action']['target']})")
    if a["move_action"]:
        m = a["move_action"]
        out.append(f"move({m['target']['x']},{m['target']['y']}{'' if m['find_closest_position'] else ' exact'}"
                   f"{' break' if m['break_through'] else ''})")
    return "+".join(out) or "nothing"


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, names = argv[0], argv[1:]
    if cmd == "list":
        for sc in SCENARIOS.values():
            print(f"{sc.name:32} q{','.join(map(str, sc.questions)):8} {'random ' if sc.random else ''}{sc.about[:70]}")
        return 0
    if cmd == "show":
        for n in names:
            print(show(fixture_io.load(n)))
        return 0
    failed = 0
    if cmd == "alternatives":
        for sc in _select(names):
            fx = fixture_io.load(sc.name)
            alts = fixture_io.alternatives(fx)
            fx["expected"].pop("alternatives", None)
            if alts:
                fx["expected"]["alternatives"] = alts
            fixture_io.write(fx)
            print(f"{sc.name}: " + (", ".join(f"tick {t}: {len(v)}" for t, v in alts.items()) or "deterministic"),
                  flush=True)
        return 0
    for sc in _select(names):
        fresh = fixture_io.record(sc, with_alternatives=cmd == "record")
        if cmd == "record":
            path = fixture_io.write(fresh)
            print(f"recorded {path.name}: {len(fresh['expected']['ticks'])} states")
        elif cmd == "check":
            problems = fixture_io.compare(fixture_io.load(sc.name), fresh)
            print(f"{'ok  ' if not problems else 'DIFF'} {sc.name}")
            for p in problems:
                print(f"     {p}")
            failed += bool(problems)
        else:
            raise SystemExit(f"unknown command {cmd}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
