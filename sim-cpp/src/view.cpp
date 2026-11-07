#include "view.hpp"

#include <algorithm>

#include "pathfind.hpp"

namespace cc {

Vec2 to_view(int seat, Vec2 pos, int size, int n) {
    int x = pos.x, y = pos.y;
    switch (seat) {
        case 1: return Vec2{n - x - size, n - y - size};
        case 2: return Vec2{y, n - x - size};
        case 3: return Vec2{n - y - size, x};
        default: return Vec2{x, y};
    }
}

Vec2 from_view(int seat, Vec2 pos, int size, int n) {
    int a = pos.x, b = pos.y;
    switch (seat) {
        case 1: return Vec2{n - a - size, n - b - size};
        case 2: return Vec2{n - b - size, a};
        case 3: return Vec2{b, n - a - size};
        default: return Vec2{a, b};
    }
}

std::vector<uint8_t> sight_map(const Model& m, std::optional<int32_t> player_id) {
    Grid grid(m.map_size);
    std::vector<uint8_t> seen(static_cast<size_t>(m.map_size) * m.map_size, 0);
    for (const auto& kv : m.entities) {
        const Entity& e = kv.second;
        if (e.player_id == player_id) {
            const EntityProperties& p = m.props[e.entity_type];
            grid.around(e.position, p.size, p.sight_range, [&](int i) { seen[static_cast<size_t>(i)] = 1; });
        }
    }
    return seen;
}

PlayerView player_view(const Model& m, int32_t player_id, const std::vector<uint8_t>* seen) {
    PlayerView v;
    int seat = m.players.at(player_id).index;
    int n = m.map_size;
    v.my_id = player_id;
    v.map_size = n;
    v.fog_of_war = m.fog_of_war;
    v.props = &m.props;
    v.max_tick_count = m.max_tick_count;
    v.max_pathfind_nodes = m.max_pathfind_nodes;
    v.current_tick = m.current_tick;
    for (const auto& kv : m.players) v.players.push_back(ViewPlayer{kv.second.id, kv.second.score, kv.second.resource});
    std::vector<uint8_t> own_sight;
    if (m.fog_of_war && !seen) {
        own_sight = sight_map(m, player_id);
        seen = &own_sight;
    }
    for (const auto& kv : m.entities) {
        const Entity& e = kv.second;
        int size = m.props[e.entity_type].size;
        bool shown = !m.fog_of_war || e.player_id == player_id;
        if (!shown) {
            for (int y = std::max(e.position.y, 0); y < std::min(e.position.y + size, n) && !shown; y++)
                for (int x = std::max(e.position.x, 0); x < std::min(e.position.x + size, n); x++)
                    if ((*seen)[static_cast<size_t>(y * n + x)]) {
                        shown = true;
                        break;
                    }
        }
        if (shown) {
            Entity c = e;
            c.position = to_view(seat, e.position, size, n);
            v.entities.push_back(c);
        }
    }
    return v;
}

EntityAction action_from_view(int seat, const EntityAction& a, const Model& m) {
    EntityAction out = a;
    int n = m.map_size;
    if (out.move_action) out.move_action->target = from_view(seat, out.move_action->target, 1, n);
    if (out.build_action) {
        int size = m.props[out.build_action->entity_type].size;
        out.build_action->position = from_view(seat, out.build_action->position, size, n);
    }
    return out;
}

}  // namespace cc
