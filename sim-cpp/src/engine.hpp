// The game loop of `aicup2020 --batch-mode`, as sim/codecraft_sim/engine.py.
#pragma once
#include <string>

#include "json.hpp"
#include "players.hpp"

namespace cc {

constexpr int PANIC = 101;
int run(const Json& config, const std::string& save_results, const std::string& save_replay, Log& log);

}  // namespace cc
