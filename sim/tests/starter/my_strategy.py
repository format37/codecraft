# python/my_strategy.py of github.com/format37/codecraft-starter at commit cd13533, as it is (these two lines aside):
# the strategy that the built-in QuickStart ports. test_quickstart.py plays it with the official Python client.
from model import *


class MyStrategy:
    # One MyStrategy plays the whole game: anything you keep on self survives from tick to tick.

    def log(self, text):
        if self.debug:
            self.debug.send(DebugCommand.Add(DebugData.Log(text)))

    def draw_rectangle(self, x0, y0, x1, y1, color=Color(1, 0, 0, 1)):
        if not self.debug:
            return
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        vertices = []
        for i in range(4):  # LINES takes vertices in pairs: one pair per side
            for x, y in (corners[i], corners[(i + 1) % 4]):
                vertices.append(ColoredVertex(Vec2Float(x, y), Vec2Float(0, 0), color))
        self.debug.send(DebugCommand.Add(DebugData.Primitives(vertices, PrimitiveType.LINES)))

    def get_action(self, player_view, debug_interface):
        self.debug = debug_interface  # None on the server and in batch mode: there is no viewer to draw in
        me = player_view.my_id
        house_props = player_view.entity_properties[EntityType.HOUSE]
        resources = next(p.resource for p in player_view.players if p.id == me)
        mine = [e for e in player_view.entities if e.player_id == me]
        builders = [e for e in mine if e.entity_type == EntityType.BUILDER_UNIT]
        base = next((e for e in mine if e.entity_type == EntityType.BUILDER_BASE), None)
        house = next((e for e in mine if e.entity_type == EntityType.HOUSE), None)

        count = {}
        for e in mine:
            count[e.entity_type] = count.get(e.entity_type, 0) + 1
        for entity_type, n in count.items():
            self.log(f'{entity_type.name}: {n}')
        if house is not None:
            self.log(f'house health: {house.health}/{house_props.max_health}')

        # One house, right of the builder base (which is 5x5)
        house_spot = None
        if house is None and base is not None and resources >= house_props.initial_cost:
            house_spot = Vec2Int(base.position.x + 5, base.position.y + 4)
            self.draw_rectangle(house_spot.x, house_spot.y,
                                house_spot.x + house_props.size, house_spot.y + house_props.size)

        # Of the actions given to an entity, the first one possible this tick is done, in the order
        # attack, build, repair, move. So "build (or repair), else move" walks a builder to the house
        # and starts the job as soon as it is next to it.
        result = Action({})
        for builder in builders:
            if house_spot is not None:
                build = BuildAction(EntityType.HOUSE, house_spot)
                result.entity_actions[builder.id] = EntityAction(
                    MoveAction(house_spot, True, False), build, None, None)
            elif house is not None and house.health < house_props.max_health:
                # A new house starts with little health and works only once repaired to full
                result.entity_actions[builder.id] = EntityAction(
                    MoveAction(house.position, True, False), None, None, RepairAction(house.id))
            else:
                # Thoughtless mining: step right. A move into an adjacent cell with a resource mines it.
                step = Vec2Int(builder.position.x + 1, builder.position.y)
                result.entity_actions[builder.id] = EntityAction(MoveAction(step, True, False), None, None, None)
        return result

    def debug_update(self, player_view, debug_interface):
        # main.py calls this while the viewer waits between ticks; drawings sent here show on every tick
        pass
