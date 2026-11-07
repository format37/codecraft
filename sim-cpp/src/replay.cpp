#include "replay.hpp"

#include <stdexcept>

#include "trans.hpp"

namespace cc {

static void write_player(Writer& w, const Player& p) {
    w.i32(p.id);
    w.i32(p.index);
    w.i32(p.score);
    w.i32(p.resource);
}

static void write_model(Writer& w, const Model& m) {
    w.i32(m.next_id);
    w.i32(m.map_size);
    w.boolean(m.fog_of_war);
    write_properties_map(w, m.props);
    w.i32(m.max_tick_count);
    w.i32(m.max_pathfind_nodes);
    w.i32(m.current_tick);
    w.i32(m.passive_income);
    w.i32(static_cast<int32_t>(m.players.size()));
    for (const auto& kv : m.players) {
        w.i32(kv.first);
        write_player(w, kv.second);
    }
    w.i32(static_cast<int32_t>(m.entities.size()));
    for (const auto& kv : m.entities) {
        w.i32(kv.first);
        write_entity(w, kv.second);
    }
    w.i32(static_cast<int32_t>(m.entity_actions.size()));
    for (const auto& kv : m.entity_actions) {
        w.i32(kv.first);
        w.i32(kv.first);
        write_entity_action(w, kv.second);
    }
}

// added (in the new order), removed (in the old order), updated (in the new order), as replay.diff
template <class T, class F>
static void write_delta(Writer& w, const std::map<int32_t, T>& prev, const std::map<int32_t, T>& cur, F write_item) {
    std::vector<const std::pair<const int32_t, T>*> added, updated;
    for (const auto& kv : cur) {
        auto it = prev.find(kv.first);
        if (it == prev.end()) added.push_back(&kv);
        else if (!(it->second == kv.second)) updated.push_back(&kv);
    }
    w.i32(static_cast<int32_t>(added.size()));
    for (auto* kv : added) write_item(w, kv->first, kv->second);
    std::vector<int32_t> removed;
    for (const auto& kv : prev)
        if (!cur.count(kv.first)) removed.push_back(kv.first);
    w.i32(static_cast<int32_t>(removed.size()));
    for (int32_t k : removed) w.i32(k);
    w.i32(static_cast<int32_t>(updated.size()));
    for (auto* kv : updated) {
        w.i32(kv->first);
        write_item(w, kv->first, kv->second);
    }
}

ReplayWriter::ReplayWriter(const std::string& path) : f_(fopen(path.c_str(), "wb")) {
    if (!f_) throw std::runtime_error("cannot write the replay " + path);
}

ReplayWriter::~ReplayWriter() { close(); }

void ReplayWriter::close() {
    if (f_) {
        fclose(f_);
        f_ = nullptr;
    }
}

void ReplayWriter::start(const Model& m) {
    Writer w;
    write_model(w, m);
    fwrite(w.buf.data(), 1, w.buf.size(), f_);
    prev_ = m;
}

void ReplayWriter::tick(const std::vector<Event>& events, const Model& m) {
    Writer w;
    w.i32(static_cast<int32_t>(events.size()));
    for (const Event& e : events) {
        w.i32(e.tag);
        w.i32(e.a);
        w.i32(e.b);
    }
    w.i32(m.next_id);
    w.i32(m.map_size);
    w.boolean(m.fog_of_war);
    w.i32(m.max_tick_count);
    w.i32(m.max_pathfind_nodes);
    w.i32(m.current_tick);
    w.i32(m.passive_income);
    write_delta(w, prev_.players, m.players, [](Writer& wr, int32_t, const Player& p) { write_player(wr, p); });
    write_delta(w, prev_.entities, m.entities, [](Writer& wr, int32_t, const Entity& e) { write_entity(wr, e); });
    write_delta(w, prev_.entity_actions, m.entity_actions, [](Writer& wr, int32_t id, const EntityAction& a) {
        wr.i32(id);
        write_entity_action(wr, a);
    });
    fwrite(w.buf.data(), 1, w.buf.size(), f_);
    prev_.next_id = m.next_id;
    prev_.current_tick = m.current_tick;
    prev_.players = m.players;
    prev_.entities = m.entities;
    prev_.entity_actions = m.entity_actions;
}

}  // namespace cc
