// The replay file of `--save-replay` (docs/replay-format.md), written as the game runs: the Model at the first tick,
// then per tick the events and the delta of the collections (sim/codecraft_sim/replay.py, ReplayWriter).
#pragma once
#include <cstdio>
#include <string>
#include <vector>

#include "model.hpp"

namespace cc {

class ReplayWriter {
public:
    explicit ReplayWriter(const std::string& path);
    ~ReplayWriter();
    void start(const Model& m);
    void tick(const std::vector<Event>& events, const Model& m);
    void close();

private:
    FILE* f_;
    Model prev_;
};

}  // namespace cc
