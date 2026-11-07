// The starting state of a `Create` game, as sim/codecraft_sim/mapgen.py: the presets, `Custom`, the placement of the
// seats by quarter turns, and our map generator (gradient noise in fixed point, the same under the eight symmetries
// of the square, corners kept free and joined), drawing exactly what the Python one draws.
#pragma once
#include <string>
#include <utility>
#include <vector>

#include "json.hpp"
#include "model.hpp"
#include "rng.hpp"

namespace cc {

struct Options {
    int32_t map_size = 80, initial_resource = 0, passive_income = 0, max_pathfind_nodes = 1000;
    bool fog_of_war = false;
    int32_t max_tick_count = 1000;
    std::vector<std::pair<EntityType, Vec2>> starting_entities;
    Properties props = default_properties();
};

Options preset(const std::string& name);          // throws std::invalid_argument for an unknown name
Options options_from_json(const Json& custom);    // throws JsonError
Vec2 place(Vec2 pos, int size, int n, int seat);
Model create(const Options& o, int seats, PyRandom& rng);
uint32_t below(PyRandom& rng, uint32_t n);

}  // namespace cc
