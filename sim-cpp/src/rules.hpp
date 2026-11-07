// The rules of a tick, as sim/codecraft_sim/rules.py (its docstring is the spec): pathfinding, attacks, builds,
// repairs, movement, the end of the tick; every draw from the game's PyRandom, in the Python engine's order.
#pragma once
#include <map>
#include <set>
#include <vector>

#include "model.hpp"
#include "rng.hpp"

namespace cc {

using SightMaps = std::map<int32_t, std::vector<uint8_t>>;     // player id -> 1 per seen cell (y * n + x)

int distance(Vec2 a, int a_size, Vec2 b, int b_size);
std::set<int32_t> players_with_entities(const Model& m);
bool game_over(const Model& m);
int unit_cost(const Model& m, int32_t player_id, EntityType t);
// Plays one turn on `m` and returns its events in the order they happened. `seen`: the players' sight maps at the
// start of the turn under fog of war, when already made for the views.
std::vector<Event> process_turn(Model& m, PyRandom& rng, const SightMaps* seen = nullptr);

}  // namespace cc
