// What a seat is sent, as sim/codecraft_sim/view.py: fog of war (the entities with a cell in the sight of one of the
// player's entities), each seat in its own frame (seat 1 turned by 180 degrees, seat 2 by 90, seat 3 by 270), the
// entities by id; and the way back for the positions of the seat's orders.
#pragma once
#include <optional>
#include <vector>

#include "model.hpp"

namespace cc {

Vec2 to_view(int seat, Vec2 pos, int size, int n);
Vec2 from_view(int seat, Vec2 pos, int size, int n);
std::vector<uint8_t> sight_map(const Model& m, std::optional<int32_t> player_id);
PlayerView player_view(const Model& m, int32_t player_id, const std::vector<uint8_t>* seen = nullptr);
EntityAction action_from_view(int seat, const EntityAction& a, const Model& m);

}  // namespace cc
