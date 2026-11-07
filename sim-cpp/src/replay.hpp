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
    // `keep`: the bytes also stay in memory, for bytes(); an empty path writes no file (the viewer's Ctrl-S)
    explicit ReplayWriter(const std::string& path, bool keep = false);
    ~ReplayWriter();
    void start(const Model& m);
    void tick(const std::vector<Event>& events, const Model& m);
    void close();
    const std::string& bytes() const { return kept_; }

private:
    FILE* f_ = nullptr;
    bool keep_;
    std::string kept_;
    Model prev_;
    void put(const std::string& data);
};

// Reads a replay file back, tick by tick (`--replay`): the first state, then each tick's events and state.
class ReplayReader {
public:
    explicit ReplayReader(std::string data);    // throws std::runtime_error on a malformed file
    const Model& initial() const { return initial_; }
    bool next(Model& m, std::vector<Event>& events);    // m: the state before; false at the end of the file

private:
    std::string data_;
    size_t pos_ = 0;
    Model initial_;
    int32_t i32();
    bool boolean();
    int32_t count();
    Model model();
    Player player();
    Entity entity();
    EntityAction action();
    Properties properties();
    std::vector<EntityType> types();
};

}  // namespace cc

