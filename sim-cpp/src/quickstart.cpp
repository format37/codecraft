#include "quickstart.hpp"

namespace cc {

Action quickstart_action(const PlayerView& view) {
    using T = EntityType;
    int32_t me = view.my_id;
    const EntityProperties& house_props = (*view.props)[T::House];
    int resources = 0;
    for (const ViewPlayer& p : view.players)
        if (p.id == me) {
            resources = p.resource;
            break;
        }
    std::vector<const Entity*> builders;
    const Entity* base = nullptr;
    const Entity* house = nullptr;
    for (const Entity& e : view.entities) {
        if (e.player_id != me) continue;
        if (e.entity_type == T::BuilderUnit) builders.push_back(&e);
        if (e.entity_type == T::BuilderBase && !base) base = &e;
        if (e.entity_type == T::House && !house) house = &e;
    }

    // one house, right of the builder base (which is 5x5)
    std::optional<Vec2> house_spot;
    if (!house && base && resources >= house_props.initial_cost)
        house_spot = Vec2{base->position.x + 5, base->position.y + 4};

    Action result;
    for (const Entity* b : builders) {
        EntityAction a;
        if (house_spot) {
            a.move_action = MoveAction{*house_spot, true, false};
            a.build_action = BuildAction{T::House, *house_spot};
        } else if (house && house->health < house_props.max_health) {
            a.move_action = MoveAction{house->position, true, false};
            a.repair_action = RepairAction{house->id};
        } else {  // a step right: a move into a cell with a resource mines it
            a.move_action = MoveAction{Vec2{b->position.x + 1, b->position.y}, true, false};
        }
        result.emplace_back(b->id, a);
    }
    return result;
}

}  // namespace cc
