"""The rules of CodeCraft 2020: one tick of the game, as the official engine (aicup2020 1.3.0) plays it.

    events = process_turn(model, rng)   # plays the turn from model.current_tick to the next tick, in place
    game_over(model)                    # at most one player owns entities, or the ticks are spent

The orders are the actions stored in `model.entity_actions`. The engine loop stores each player's new orders there
before the turn; an order stays until the player replaces it (the orders of dead entities stay too, unused). An
entity does at most one thing per tick: the first of attack, build, repair, move that it can do. Inactive
entities do nothing. Everything random (the order of the attacks and of the builds, the target of an auto-attack,
the winner of a movement conflict, the choice among equally short paths) is drawn from `rng`, so a seed and the
same orders replay a game exactly; the official engine draws differently, the fixtures list what it can draw.

The phases (docs/doc-en.md "Control interface", settled against the official engine by sim/fixtures):

1. Pathfinding, on the positions at the start of the tick (every entity is an obstacle, own units included;
   under fog of war only those the unit's player sees): each active unit gets the cell it would step into. A move
   order whose target is next to the unit names that cell directly, any other is planned with A* (pathfind.py).
   A unit with an auto-attack and nothing to hit (neither the named target nor a valid enemy within range) looks
   for a cell next to a valid enemy at most `pathfind_range` steps away (next to it whatever the attack range),
   and heads there instead of following its move order.
2. Attacks, each attacker once, in a random order: the named target if it exists and is within attack range;
   else, with an auto-attack, a random valid enemy within range (one already at 0 health included); else the
   entity in the cell the unit would step into, if it is not its player's (that is how a move order onto a
   resource mines it). Every hit deals its damage and leaves an event, overkill included; the hit that takes a
   target from above 0 to 0 or below gives its destroy score, except to the target's own player; an attacker
   that collects gets resource and score for the health a resource still had. An entity hit to 0 earlier in the
   order still attacks.
3. Builds, in a random order, by entities that have not attacked. An order for a type the builder may build
   takes the next id, whether it succeeds or not; it needs a site on the map, free, touching the builder by a
   side, the money (paid at once: a unit costs its initial cost plus the player's units of its type, inactive ones
   included; a building its initial cost) and, for an entity that uses population, room for it (provided by the
   player's active entities, used by all its entities). The new entity is inactive, with the builder's
   `init_health` or full health.
4. Repairs, by entities that did neither: an adjacent target of a valid type (any player's) with health above
   0 gets `power`, up to its maximum; a target already at full health still takes the repairer's turn.
5. Movement, by the units that did nothing else, in steps: each step moves every unit whose next cell is free
   (one at random when several want the same cell), until no unit can move. Swaps fail. An entity at 0 health
   still stands on its cells (it is removed at the end of the tick) and a unit at 0 health still moves.
6. The end of the tick: entities at 0 health or below are removed; inactive ones at full health become active
   and their player scores their build score; every player gets the passive income; the tick counter grows; when
   exactly two players play and one has just lost its last entity, the other gets its score.
"""
from __future__ import annotations

import random
from typing import Iterable

from . import pathfind
from .model import (ATTACK_EVENT, AutoAttack, BuildAction, Entity, EntityAction, EntityType, Event, Model, MoveAction,
                    Vec2)
from .pathfind import FREE, Grid


def distance(a: Vec2, a_size: int, b: Vec2, b_size: int) -> int:
    """The Manhattan distance between the nearest cells of two squares (0 when they overlap): attack range, the
    reach of builders and repairers, sight."""
    dx = max(0, b.x - (a.x + a_size - 1), a.x - (b.x + b_size - 1))
    dy = max(0, b.y - (a.y + a_size - 1), a.y - (b.y + b_size - 1))
    return dx + dy


def players_with_entities(model: Model) -> set[int]:
    return {e.player_id for e in model.entities.values() if e.player_id is not None}


def game_over(model: Model) -> bool:
    return model.current_tick >= model.max_tick_count or len(players_with_entities(model)) <= 1


def unit_cost(model: Model, player_id: int, entity_type: EntityType) -> int:
    """What building an entity of this type costs the player now."""
    p = model.entity_properties[entity_type]
    if not p.can_move:
        return p.initial_cost
    return p.initial_cost + sum(1 for e in model.entities.values()
                                if e.player_id == player_id and e.entity_type == entity_type)


def population(model: Model, player_id: int) -> tuple[int, int]:
    """(provided, used): provided by the player's active entities, used by all of them."""
    provided = used = 0
    for e in model.entities.values():
        if e.player_id == player_id:
            p = model.entity_properties[e.entity_type]
            used += p.population_use
            if e.active:
                provided += p.population_provide
    return provided, used


def process_turn(model: Model, rng: random.Random) -> list[Event]:
    """Play one turn on `model` (changed in place) and return its events, in the order they happened."""
    return _Turn(model, rng).play()


class _Turn:
    def __init__(self, model: Model, rng: random.Random) -> None:
        self.m = model
        self.rng = rng
        self.props = model.entity_properties
        self.grid = Grid.of(model)
        self.ids = sorted(model.entities)       # the entities at the start of the tick, in a fixed order
        self.events: list[Event] = []
        self.busy: set[int] = set()             # attacked, built or repaired this tick
        self._goals: dict[tuple, set[int]] = {}
        self._seen: dict[int | None, bytearray] = {}

    def play(self) -> list[Event]:
        playing = players_with_entities(self.m)
        steps = self.plan()
        self.attacks(steps)
        self.builds()
        self.repairs()
        self.movement(steps)
        self.end_of_tick(playing)
        return self.events

    def order_of(self, e: Entity) -> EntityAction | None:
        return self.m.entity_actions.get(e.id) if e.active else None

    # 1. pathfinding ------------------------------------------------------------------------------------------
    def plan(self) -> dict[int, Vec2]:
        """The cell each unit would step into this tick."""
        steps = {}
        for eid in self.ids:
            step = self.plan_unit(self.m.entities[eid])
            if step is not None:
                steps[eid] = step
        return steps

    def plan_unit(self, e: Entity) -> Vec2 | None:
        """The cell `e` would step into, or None: no order, it cannot move, or it has something to hit."""
        a = self.order_of(e)
        p = self.props[e.entity_type]
        if a is None or not p.can_move:
            return None
        step = None
        at = a.attack_action
        if at is not None and p.attack is not None:
            if self.named_target(e, at.target) is not None:
                return None                     # it will attack and not move
            if at.auto_attack is not None:
                if self.in_range(e, at.auto_attack):
                    return None
                step = self.hunt(e, at.auto_attack)
        if step is None and a.move_action is not None:
            step = self.move_step(e, a.move_action)
        return step

    def move_step(self, e: Entity, mv: MoveAction, prefer: Vec2 | None = None) -> Vec2 | None:
        """The step of a move order. `prefer` is for the corpus check, which follows a record: among equally good
        paths, those through that cell win the draw."""
        t, pos = mv.target, e.position
        d = abs(t.x - pos.x) + abs(t.y - pos.y)
        if d == 0:
            return None
        if d == 1:
            return t
        return pathfind.first_step(self.grid, pos, t, self.m.max_pathfind_nodes,
                                   self.entry_cost(e, mv.break_through), self.rng, prefer)

    def entry_cost(self, e: Entity, break_through: bool):
        """The price of entering each cell for a path of `e` (None: it cannot), see pathfind.py."""
        cells, seen = self.grid.cells, self.seen(e.player_id)
        if not break_through:
            if seen is None:
                return lambda i: 1 if cells[i] == FREE else None
            return lambda i: 1 if cells[i] == FREE or not seen[i] else None
        entities, own = self.m.entities, e.player_id
        damage = self.props[e.entity_type].attack.damage if self.props[e.entity_type].attack else 0

        def cost(i: int) -> int | None:
            o = cells[i]
            if o == FREE or (seen is not None and not seen[i]):
                return 1
            b = entities[o]
            if b.player_id == own or damage <= 0:
                return None
            return -(-max(b.health, 1) // damage)        # the hits it takes, in place of the step's 1
        return cost

    def seen(self, player_id: int | None) -> bytearray | None:
        """Under fog of war, the cells the player sees (within the sight range of one of its entities, inactive
        ones included), 1 for each; a path search of the player's units takes every other cell for free, whatever
        stands there (the official engine plans with what the player sees). None without fog."""
        if not self.m.fog_of_war:
            return None
        if player_id not in self._seen:
            seen = bytearray(self.m.map_size * self.m.map_size)
            for t in self.m.entities.values():
                if t.player_id == player_id:
                    p = self.props[t.entity_type]
                    for i in self.grid.around(t.position, p.size, p.sight_range):
                        seen[i] = 1
            self._seen[player_id] = seen
        return self._seen[player_id]

    def hunt(self, e: Entity, auto: AutoAttack, prefer: Vec2 | None = None) -> Vec2 | None:
        """A step towards the nearest cell next to a valid enemy, if one is at most `pathfind_range` steps away:
        the reach is counted to a cell next to the enemy whatever the attack range (`attack-auto-ranged-reach`);
        a ranged unit stops to shoot as soon as an enemy is in range. The enemies count whether the player sees them
        or not. `prefer`: see `move_step`."""
        goals = self.goal_cells(e.player_id, auto.valid_targets, 1)
        return pathfind.hunt_step(self.grid, e.position, goals, auto.pathfind_range, self.m.max_pathfind_nodes,
                                  self.entry_cost(e, False), self.rng, prefer)

    def goal_cells(self, player_id: int, valid: tuple[EntityType, ...], r: int) -> set[int]:
        key = (player_id, valid, r)
        if key not in self._goals:
            goals: set[int] = set()
            for t in self.m.entities.values():
                if self.is_enemy(player_id, t, valid):
                    goals.update(self.grid.around(t.position, self.props[t.entity_type].size, r))
            self._goals[key] = goals
        return self._goals[key]

    @staticmethod
    def is_enemy(player_id: int, t: Entity, valid: tuple[EntityType, ...]) -> bool:
        """A target for an auto-attack: of a valid type and not the player's; with no types given, any other
        player's entity (never a resource)."""
        if t.player_id == player_id:
            return False
        return t.entity_type in valid if valid else t.player_id is not None

    def in_range(self, e: Entity, auto: AutoAttack) -> list[Entity]:
        """The valid enemies within attack range of `e`, by id."""
        p = self.props[e.entity_type]
        cells, entities = self.grid.cells, self.m.entities
        found = {cells[i] for i in self.grid.around(e.position, p.size, p.attack.attack_range)} - {FREE}
        return [entities[i] for i in sorted(found) if self.is_enemy(e.player_id, entities[i], auto.valid_targets)]

    # 2. attacks ----------------------------------------------------------------------------------------------
    def attacks(self, steps: dict[int, Vec2]) -> None:
        ids = []
        for eid in self.ids:
            e = self.m.entities[eid]
            a = self.order_of(e)
            if (a is not None and self.props[e.entity_type].attack is not None
                    and (a.attack_action is not None or eid in steps)):
                ids.append(eid)
        for eid in self.attack_order(ids):
            e = self.m.entities[eid]
            target = self.attack_target(e, self.m.entity_actions[eid], steps.get(eid))
            if target is not None:
                self.hit(e, target)
                self.busy.add(eid)

    def named_target(self, e: Entity, target_id: int | None) -> Entity | None:
        """The entity an attack order names, if it exists and is within attack range."""
        t = self.m.entities.get(target_id) if target_id is not None else None
        p = self.props[e.entity_type]
        if t is not None and distance(e.position, p.size, t.position,
                                      self.props[t.entity_type].size) <= p.attack.attack_range:
            return t
        return None

    def attack_target(self, e: Entity, a: EntityAction, step: Vec2 | None) -> Entity | None:
        at = a.attack_action
        if at is not None:
            t = self.named_target(e, at.target)
            if t is not None:
                return t
            if at.auto_attack is not None:
                enemies = self.in_range(e, at.auto_attack)
                if enemies:
                    return self.auto_target(e, enemies)
        if step is not None:
            o = self.grid.occupant(step)
            if o != FREE and self.m.entities[o].player_id != e.player_id:
                return self.m.entities[o]
        return None

    def hit(self, e: Entity, t: Entity) -> None:
        attack = self.props[e.entity_type].attack
        tp = self.props[t.entity_type]
        self.events.append(Event(ATTACK_EVENT, e.id, t.id))
        before = t.health
        t.health -= attack.damage
        player = self.m.players.get(e.player_id)
        if player is None:
            return
        if attack.collect_resource and tp.resource_per_health and before > 0:
            gained = min(attack.damage, before) * tp.resource_per_health
            player.resource += gained
            player.score += gained
        if before > 0 >= t.health and t.player_id != e.player_id:
            player.score += tp.destroy_score

    # 3. builds -----------------------------------------------------------------------------------------------
    def builds(self) -> None:
        ids = []
        for eid in self.ids:
            e = self.m.entities[eid]
            a = self.order_of(e)
            if (a is not None and a.build_action is not None and eid not in self.busy
                    and self.props[e.entity_type].build is not None):
                ids.append(eid)
        for eid in self.build_order(ids):
            if self.build(self.m.entities[eid], self.m.entity_actions[eid]):
                self.busy.add(eid)

    def build(self, e: Entity, a: EntityAction) -> bool:
        b, m = a.build_action, self.m
        bp = self.props[e.entity_type].build
        if b.entity_type not in bp.options:
            return False
        new_id = m.next_id                      # from here on an attempt uses up an id, a failed one too
        m.next_id += 1
        if not self.can_build(e, b):
            return False
        tp = self.props[b.entity_type]
        m.players[e.player_id].resource -= unit_cost(m, e.player_id, b.entity_type)
        health = tp.max_health if bp.init_health is None else bp.init_health
        new = Entity(new_id, e.player_id, b.entity_type, b.position, health, active=False)
        m.entities[new.id] = new
        self.grid.place(new.id, new.position, tp.size)
        return True

    def can_build(self, e: Entity, b: BuildAction) -> bool:
        """Whether `e` can build now what `b` orders: the site (on the map, free, touching `e` by a side), the
        money, and room for the population the new entity uses."""
        m, tp = self.m, self.props[b.entity_type]
        if distance(e.position, self.props[e.entity_type].size, b.position, tp.size) != 1:
            return False
        if not self.grid.free_square(b.position, tp.size):
            return False
        player = m.players.get(e.player_id)
        if player is None or player.resource < unit_cost(m, e.player_id, b.entity_type):
            return False
        if tp.population_use:
            provided, used = population(m, e.player_id)
            if used + tp.population_use > provided:
                return False
        return True

    # 4. repairs ----------------------------------------------------------------------------------------------
    def repairs(self) -> None:
        for eid in self.ids:
            e = self.m.entities[eid]
            a = self.order_of(e)
            rp = self.props[e.entity_type].repair
            if a is None or a.repair_action is None or rp is None or eid in self.busy:
                continue
            t = self.m.entities.get(a.repair_action.target)
            if t is None or t.health <= 0 or t.entity_type not in rp.valid_targets:
                continue
            tp = self.props[t.entity_type]
            if distance(e.position, self.props[e.entity_type].size, t.position, tp.size) != 1:
                continue
            t.health = min(tp.max_health, t.health + rp.power)
            self.busy.add(eid)

    # 5. movement ---------------------------------------------------------------------------------------------
    def movement(self, steps: dict[int, Vec2]) -> None:
        pending = {eid: c for eid, c in steps.items() if eid not in self.busy}
        grid, entities = self.grid, self.m.entities
        while pending:
            wanted: dict[Vec2, list[int]] = {}
            for eid, c in pending.items():
                if grid.is_free(c):
                    wanted.setdefault(c, []).append(eid)
            if not wanted:
                break
            for c, eids in wanted.items():
                eid = self.conflict_winner(c, eids)
                e = entities[eid]
                grid.clear(e.position, 1)
                e.position = c
                grid.place(eid, c, 1)
                for loser in eids:
                    del pending[loser]          # the cell is taken: the others will not get in this tick

    # the draws -----------------------------------------------------------------------------------------------
    # Every random choice of a turn is made here, or in the ties of the path searches (pathfind.py, from the same
    # generator). The corpus check (oracle/corpus.py) overrides these to make the choices a record shows.
    def attack_order(self, ids: list[int]) -> Iterable[int]:
        self.rng.shuffle(ids)
        return ids

    def auto_target(self, e: Entity, enemies: list[Entity]) -> Entity:
        return self.rng.choice(enemies)

    def build_order(self, ids: list[int]) -> Iterable[int]:
        """The builders in the order they try (each `build` is done before the next builder is taken)."""
        self.rng.shuffle(ids)
        return ids

    def conflict_winner(self, cell: Vec2, eids: list[int]) -> int:
        return eids[0] if len(eids) == 1 else self.rng.choice(eids)

    # 6. the end of the tick ----------------------------------------------------------------------------------
    def end_of_tick(self, playing: set[int]) -> None:
        m = self.m
        for eid in [eid for eid, e in m.entities.items() if e.health <= 0]:
            del m.entities[eid]
        for e in m.entities.values():
            p = self.props[e.entity_type]
            if not e.active and e.health >= p.max_health:
                e.active = True
                if e.player_id in m.players:
                    m.players[e.player_id].score += p.build_score
        for player in m.players.values():
            player.resource += m.passive_income
        m.current_tick += 1
        left = players_with_entities(m)
        if len(m.players) == 2 and len(playing) == 2 and len(left) == 1:
            (winner,) = left
            (loser,) = set(m.players) - left
            m.players[winner].score += m.players[loser].score
