#include "quickstart.hpp"

#include <algorithm>
#include <cstdlib>
#include <map>

namespace cc {

namespace {

using T = EntityType;
const int FIRST_BUILDERS = 5, MAX_BUILDERS = 12, ARMY_SIZE = 8, REPAIR_RANGE = 6;

int dist(Vec2 a, Vec2 b) { return std::abs(a.x - b.x) + std::abs(a.y - b.y); }

// Python's min(entities, key=distance): the first of the nearest
const Entity* nearest(const std::vector<const Entity*>& entities, Vec2 pos) {
    const Entity* best = nullptr;
    int bd = 0;
    for (const Entity* e : entities) {
        int d = dist(e->position, pos);
        if (!best || d < bd) {
            best = e;
            bd = d;
        }
    }
    return best;
}

}  // namespace

Action quickstart_action(const PlayerView& view) {
    int32_t me = view.my_id;
    int size = view.map_size;
    const Properties& props = *view.props;
    std::vector<const Entity*> mine, enemies;
    for (const Entity& e : view.entities) {
        if (e.player_id == me) mine.push_back(&e);
        if (e.player_id && *e.player_id != me) enemies.push_back(&e);
    }
    int resources = 0;
    for (const ViewPlayer& p : view.players)
        if (p.id == me) {
            resources = p.resource;
            break;
        }
    std::map<EntityType, int> count;
    for (const Entity* e : mine) count[e->entity_type]++;
    auto counted = [&](EntityType t) {
        auto it = count.find(t);
        return it == count.end() ? 0 : it->second;
    };
    int provided = 0, used = 0;
    for (const Entity* e : mine) {
        if (e->active) provided += props[e->entity_type].population_provide;
        used += props[e->entity_type].population_use;
    }
    std::vector<char> occupied(static_cast<size_t>(size) * size, 0);
    for (const Entity& e : view.entities) {
        int s = props[e.entity_type].size;
        for (int dx = 0; dx < s; dx++)
            for (int dy = 0; dy < s; dy++) {
                int cx = e.position.x + dx, cy = e.position.y + dy;
                if (0 <= cx && cx < size && 0 <= cy && cy < size) occupied[static_cast<size_t>(cy) * size + cx] = 1;
            }
    }
    auto free = [&](int x, int y, int s) {
        for (int dx = 0; dx < s; dx++)
            for (int dy = 0; dy < s; dy++) {
                int cx = x + dx, cy = y + dy;
                if (!(0 <= cx && cx < size && 0 <= cy && cy < size) || occupied[static_cast<size_t>(cy) * size + cx])
                    return false;
            }
        return true;
    };
    auto spot_next_to = [&](const Entity* entity, int s) -> std::optional<Vec2> {
        int x = entity->position.x, y = entity->position.y, es = props[entity->entity_type].size;
        for (int k = 0; k < es; k++) {
            const int cand[4][2] = {{x + es, y + k}, {x + k, y + es}, {x - s, y + k}, {x + k, y - s}};
            for (const auto& c : cand)
                if (free(c[0], c[1], s)) return Vec2{c[0], c[1]};
        }
        return std::nullopt;
    };

    std::vector<EntityType> wanted;
    for (EntityType t : {T::BuilderBase, T::RangedBase})
        if (!counted(t)) wanted.push_back(t);
    bool house_pending = false;
    for (const Entity* e : mine)
        if (e->entity_type == T::House && !e->active) house_pending = true;
    if (used + 3 > provided && !house_pending) wanted.push_back(T::House);
    std::vector<const Entity*> damaged;
    int fighters = 0;
    for (const Entity* e : mine) {
        const EntityProperties& p = props[e->entity_type];
        if (!p.can_move && e->health < p.max_health) damaged.push_back(e);
        if (p.can_move && e->entity_type != T::BuilderUnit) fighters++;
    }
    Vec2 home{size / 2, size / 2};
    for (const Entity* e : mine)
        if (!props[e->entity_type].can_move) {
            home = e->position;
            break;
        }
    std::vector<const Entity*> builder_bases, others, order;
    for (const Entity* e : mine) (e->entity_type == T::BuilderBase ? builder_bases : others).push_back(e);
    if (counted(T::BuilderUnit) < FIRST_BUILDERS) {
        order = builder_bases;
        order.insert(order.end(), others.begin(), others.end());
    } else {
        order = others;
        order.insert(order.end(), builder_bases.begin(), builder_bases.end());
    }

    Action result;
    for (const Entity* e : order) {
        const EntityProperties& p = props[e->entity_type];
        EntityAction a;
        if (e->entity_type == T::BuilderUnit) {
            const Entity* target = nearest(damaged, e->position);
            if (target && dist(target->position, e->position) <= REPAIR_RANGE) {
                a.move_action = MoveAction{target->position, true, false};
                a.repair_action = RepairAction{target->id};
            } else {
                for (size_t k = 0; k < wanted.size(); k++) {
                    EntityType t = wanted[k];
                    auto spot = spot_next_to(e, props[t].size);
                    if (spot && resources >= props[t].initial_cost) {
                        a.build_action = BuildAction{t, *spot};
                        resources -= props[t].initial_cost;
                        wanted.erase(wanted.begin() + static_cast<long>(k));
                        break;
                    }
                }
                if (!a.build_action) a.attack_action = AttackAction{std::nullopt, AutoAttack{size, {T::Resource}}};
            }
        } else if (p.can_move) {
            const Entity* enemy = nearest(enemies, e->position);
            if (fighters >= ARMY_SIZE && enemy) a.move_action = MoveAction{enemy->position, true, true};
            else a.move_action = MoveAction{home, true, false};
            a.attack_action = AttackAction{std::nullopt, AutoAttack{p.sight_range, {}}};
        } else if (p.build && e->active) {
            EntityType unit = p.build->options[0];
            int cost = props[unit].initial_cost + counted(unit);
            auto spot = spot_next_to(e, 1);
            bool enough = unit == T::BuilderUnit && counted(unit) >= MAX_BUILDERS;
            if (spot && !enough && resources >= cost && used + props[unit].population_use <= provided) {
                a.build_action = BuildAction{unit, *spot};
                resources -= cost;
                used += props[unit].population_use;
                count[unit] = counted(unit) + 1;
            }
        } else {
            a.attack_action = AttackAction{std::nullopt, AutoAttack{p.sight_range, {}}};
        }
        result.emplace_back(e->id, a);
    }
    return result;
}

}  // namespace cc
