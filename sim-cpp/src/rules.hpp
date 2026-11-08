// The rules of a tick, as sim/codecraft_sim/rules.py (its docstring is the spec): pathfinding, attacks, builds,
// repairs, movement, the end of the tick; every draw from the game's PyRandom, in the Python engine's order.
#pragma once
#include <map>
#include <optional>
#include <set>
#include <vector>

#include "model.hpp"
#include "pathfind.hpp"
#include "rng.hpp"

namespace cc {

using SightMaps = std::map<int32_t, std::vector<uint8_t>>;     // player id -> 1 per seen cell (y * n + x)

// The arena's change to the 2020 rules (rules.py, FogMemory, is the spec): under fog of war the path searches of each
// player also know the resources and buildings of others it has been shown, as last shown; a cell it does not see
// is what is remembered there, or free. One per game, updated at the start of every turn; null: the 2020 rules.
class FogMemory {
public:
    void update(const Model& m, const SightMaps& sight);
    // the remembered entity on each cell (y * n + x) for the player, FREE (-1) where none
    const std::vector<int32_t>& cells(int32_t player_id, int n);
    const std::map<int32_t, Remembered>& known(int32_t player_id) { return known_[player_id]; }

private:
    std::map<int32_t, std::map<int32_t, Remembered>> known_;    // player id -> entity id -> as last shown
    std::map<int32_t, std::vector<int32_t>> cells_;             // made again when what is remembered where changes
};

int distance(Vec2 a, int a_size, Vec2 b, int b_size);
std::set<int32_t> players_with_entities(const Model& m);
bool game_over(const Model& m);
int unit_cost(const Model& m, int32_t player_id, EntityType t);
// Plays one turn on `m` and returns its events in the order they happened. `seen`: the players' sight maps at the
// start of the turn under fog of war, when already made for the views.
// `memory`: the game's FogMemory under the arena's rules, null for the 2020 rules.
std::vector<Event> process_turn(Model& m, PyRandom& rng, const SightMaps* seen = nullptr, FogMemory* memory = nullptr);

}  // namespace cc
