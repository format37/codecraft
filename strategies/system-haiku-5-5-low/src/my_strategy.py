# system-haiku-5-5-low: economy first (builders mine), houses before the population cap,
# a steady stream of ranged units from one base once the economy is up, attack-move at the enemy.
from model import *


BUILDER_COST = 10
HOUSE_COST = 50
RANGED_COST = 30
MAX_BUILDERS = 12
ARMY_START = 8         # builders before the bases start training an army
MAX_HOUSES_IN_PROGRESS = 1


class MyStrategy:
    def __init__(self):
        self.site_index = 0  # unused placeholder kept for future site rotation

    def get_action(self, player_view, debug_interface):
        result = Action({})
        my_id = player_view.my_id
        props = player_view.entity_properties
        mine = [e for e in player_view.entities if e.player_id == my_id]
        enemies = [e for e in player_view.entities
                   if e.player_id is not None and e.player_id != my_id]

        budget = self._resource(player_view, my_id)

        def count(t, only_active=True):
            return sum(1 for e in mine if e.entity_type == t and (e.active or not only_active))

        builders = count(EntityType.BUILDER_UNIT)
        rangers = count(EntityType.RANGED_UNIT)
        melee = count(EntityType.MELEE_UNIT)

        # population: capacity of active buildings vs. units alive plus queued units.
        cap = sum(props[e.entity_type].population_provide for e in mine if e.active)
        used = sum(props[e.entity_type].population_use for e in mine)
        free_pop = cap - used
        houses_building = count(EntityType.HOUSE, only_active=False) - count(EntityType.HOUSE)

        busy_builders = set()
        bases = [e for e in mine if e.entity_type in (EntityType.BUILDER_BASE,
                                                       EntityType.RANGED_BASE,
                                                       EntityType.MELEE_BASE)]
        builder_base = [e for e in bases if e.entity_type == EntityType.BUILDER_BASE]
        combat_base = [e for e in bases if e.entity_type == EntityType.RANGED_BASE]

        # 1. Pick one idle builder to raise a house when the population is about to run out.
        house_order = None
        if free_pop <= 2 and houses_building < MAX_HOUSES_IN_PROGRESS and budget >= HOUSE_COST:
            house_order = self._house_site(mine, props)
        if house_order is not None:
            builder = self._pick_builder(mine, busy_builders)
            if builder is not None:
                busy_builders.add(builder.id)
                budget -= HOUSE_COST
                result.entity_actions[builder.id] = self._builder_action(
                    builder, BuildAction(EntityType.HOUSE, house_order), props)

        # 2. Builder bases keep training builders until the mining crew is big enough.
        for base in builder_base:
            if not base.active:
                continue
            if builders < MAX_BUILDERS and budget >= BUILDER_COST and free_pop >= 1:
                result.entity_actions[base.id] = self._train(base, EntityType.BUILDER_UNIT, props)
                budget -= BUILDER_COST
                builders += 1
                free_pop -= 1

        # 3. Once the economy is running, a ranged base trains an army.
        for base in combat_base:
            if not base.active:
                continue
            if builders >= ARMY_START and budget >= RANGED_COST and free_pop >= 1:
                result.entity_actions[base.id] = self._train(base, EntityType.RANGED_UNIT, props)
                budget -= RANGED_COST
                free_pop -= 1

        # 4. Every builder mines; idle combat units march on the enemy.
        target = self._attack_target(mine, enemies, player_view)
        for e in mine:
            if e.id in result.entity_actions:
                continue
            if e.entity_type == EntityType.BUILDER_UNIT:
                result.entity_actions[e.id] = self._builder_action(e, None, props)
            elif e.entity_type in (EntityType.RANGED_UNIT, EntityType.MELEE_UNIT):
                sight = props[e.entity_type].sight_range
                move = MoveAction(target, True, True) if target is not None else None
                result.entity_actions[e.id] = EntityAction(
                    move, None,
                    AttackAction(None, AutoAttack(sight, [])), None)
        return result

    def debug_update(self, player_view, debug_interface):
        debug_interface.send(DebugCommand.Clear())

    def _resource(self, player_view, my_id):
        for p in player_view.players:
            if p.id == my_id:
                return p.resource
        return 0

    def _train(self, base, unit_type, props):
        return EntityAction(None, BuildAction(unit_type, Vec2Int(base.position.x, base.position.y)), None, None)

    def _builder_action(self, builder, build, props):
        # A builder mines while idle: auto-attack resources in its sight. A build order moves it
        # to the site first; the auto-attack only fires on resources in range, so it cannot
        # interrupt the construction.
        if build is not None:
            return EntityAction(None, build, None, None)
        sight = props[EntityType.BUILDER_UNIT].sight_range
        return EntityAction(
            None, None,
            AttackAction(None, AutoAttack(sight, [EntityType.RESOURCE])),
            None)

    def _pick_builder(self, mine, busy):
        for e in mine:
            if e.entity_type == EntityType.BUILDER_UNIT and e.id not in busy and e.active:
                return e
        return None

    def _house_site(self, mine, props):
        bases = [e for e in mine if e.entity_type in (EntityType.BUILDER_BASE, EntityType.RANGED_BASE,
                                                       EntityType.MELEE_BASE) and e.active]
        if not bases:
            return None
        base = bases[0]
        size = props[EntityType.BUILDER_BASE].size
        occupied = set()
        for e in mine:
            s = props[e.entity_type].size
            for dx in range(s):
                for dy in range(s):
                    occupied.add((e.position.x + dx, e.position.y + dy))
        ring = []
        for r in range(size + 1, size + 9):
            for dx in range(-r, r + 1, 3):
                ring.append((base.position.x + dx, base.position.y - r))
                ring.append((base.position.x + dx, base.position.y + size + r - 3))
            for dy in range(-r, r + 1, 3):
                ring.append((base.position.x - r, base.position.y + dy))
                ring.append((base.position.x + size + r - 3, base.position.y + dy))
        for (x, y) in ring:
            cells = [(x + i, y + j) for i in range(3) for j in range(3)]
            if all(0 <= cx < 80 and 0 <= cy < 80 and (cx, cy) not in occupied for cx, cy in cells):
                return Vec2Int(x, y)
        return None

    def _attack_target(self, mine, enemies, player_view):
        if not enemies:
            size = player_view.map_size
            bases = [e for e in mine if e.entity_type == EntityType.BUILDER_BASE]
            if bases:
                b = bases[0].position
                return Vec2Int(size - 1 - b.x, size - 1 - b.y)
            return Vec2Int(size - 1, size - 1)
        bases = [e for e in enemies if e.entity_type in (EntityType.BUILDER_BASE, EntityType.MELEE_BASE,
                                                          EntityType.RANGED_BASE)]
        pool = bases or enemies
        pivot = next((e for e in mine if e.entity_type == EntityType.BUILDER_BASE), None)
        if pivot is None:
            pivot = mine[0] if mine else None
        if pivot is None:
            return pool[0].position
        return min(pool, key=lambda e: abs(e.position.x - pivot.position.x) + abs(e.position.y - pivot.position.y)).position
