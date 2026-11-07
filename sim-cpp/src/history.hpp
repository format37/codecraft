// The game as the viewer sees it: every tick's changes (players, entities, stored orders, events) as compact JSON,
// and the history of the states, kept as keyframes and per-tick diffs, so that any past tick can be rebuilt (the view
// of the tick on screen for DebugUpdate, a state file of it for Ctrl-E).
#pragma once
#include <mutex>
#include <string>
#include <utility>
#include <vector>

#include "jsonout.hpp"
#include "model.hpp"

namespace cc {

struct TickDiff {
    int32_t next_id = 0, current_tick = 0;
    std::vector<Player> players;                              // every player
    std::vector<Entity> set;                                  // entities added or changed
    std::vector<int32_t> removed;
    std::vector<std::pair<int32_t, EntityAction>> orders;     // stored orders added or changed
    std::vector<int32_t> orders_removed;
};

TickDiff diff(const Model& before, const Model& after);
void apply(Model& m, const TickDiff& d);

// JSON for the viewer: the format of server/codecraft/replay.py's replay.json (entities [id, player, type, x, y,
// health, active], players [id, index, score, resource], orders as {move, build, attack, repair}).
void entity_json(Out& o, const Entity& e);
void order_json(Out& o, const EntityAction& a);
void properties_json(Out& o, const Properties& props);
std::string tick_json(const TickDiff& d, const std::vector<Event>& events);

class History {
public:
    void start(const Model& m);
    void push(const Model& m, const TickDiff& d);    // the state after a tick, and how it came from the one before
    bool has(int tick);
    Model at(int tick);                               // throws std::out_of_range for a tick not played

private:
    static constexpr int KEY_EVERY = 50;
    std::mutex mutex_;
    int first_ = 0;
    std::vector<Model> keys_;                         // the states at first_, first_ + KEY_EVERY, ...
    std::vector<TickDiff> diffs_;                     // diffs_[k] leads from tick first_ + k to first_ + k + 1
    int cached_tick_ = -1;
    Model cached_;
};

}  // namespace cc
