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

ReplayWriter::ReplayWriter(const std::string& path, bool keep) : keep_(keep) {
    if (path.empty()) return;
    f_ = fopen(path.c_str(), "wb");
    if (!f_) throw std::runtime_error("cannot write the replay " + path);
}

void ReplayWriter::put(const std::string& data) {
    if (f_) fwrite(data.data(), 1, data.size(), f_);
    if (keep_) kept_ += data;
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
    put(w.buf);
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
    put(w.buf);
    prev_.next_id = m.next_id;
    prev_.current_tick = m.current_tick;
    prev_.players = m.players;
    prev_.entities = m.entities;
    prev_.entity_actions = m.entity_actions;
}

ReplayReader::ReplayReader(std::string data) : data_(std::move(data)) { initial_ = model(); }

int32_t ReplayReader::i32() {
    if (pos_ + 4 > data_.size()) throw std::runtime_error("the replay ends in the middle of a value");
    uint32_t v = 0;
    for (int k = 3; k >= 0; k--) v = (v << 8) | static_cast<unsigned char>(data_[pos_ + static_cast<size_t>(k)]);
    pos_ += 4;
    return static_cast<int32_t>(v);
}

bool ReplayReader::boolean() {
    if (pos_ >= data_.size()) throw std::runtime_error("the replay ends in the middle of a value");
    unsigned char b = static_cast<unsigned char>(data_[pos_++]);
    if (b > 1) throw std::runtime_error("bad bool in the replay");
    return b == 1;
}

int32_t ReplayReader::count() {
    int32_t n = i32();
    if (n < 0 || static_cast<size_t>(n) > data_.size()) throw std::runtime_error("bad count in the replay");
    return n;
}

std::vector<EntityType> ReplayReader::types() {
    std::vector<EntityType> out;
    for (int32_t k = count(); k > 0; k--) {
        int32_t t = i32();
        if (t < 0 || t >= TYPE_COUNT) throw std::runtime_error("bad entity type in the replay");
        out.push_back(static_cast<EntityType>(t));
    }
    return out;
}

Properties ReplayReader::properties() {
    Properties props;
    for (int32_t k = count(); k > 0; k--) {
        int32_t t = i32();
        if (t < 0 || t >= TYPE_COUNT) throw std::runtime_error("bad entity type in the replay");
        EntityProperties p;
        p.size = i32();
        p.build_score = i32();
        p.destroy_score = i32();
        p.can_move = boolean();
        p.population_provide = i32();
        p.population_use = i32();
        p.max_health = i32();
        p.initial_cost = i32();
        p.sight_range = i32();
        p.resource_per_health = i32();
        if (boolean()) {
            BuildProperties b;
            b.options = types();
            if (boolean()) b.init_health = i32();
            p.build = b;
        }
        if (boolean()) {
            AttackProperties a;
            a.attack_range = i32();
            a.damage = i32();
            a.collect_resource = boolean();
            p.attack = a;
        }
        if (boolean()) {
            RepairProperties r;
            r.valid_targets = types();
            r.power = i32();
            p.repair = r;
        }
        props.set(static_cast<EntityType>(t), p);
    }
    return props;
}

Player ReplayReader::player() {
    Player p;
    p.id = i32();
    p.index = i32();
    p.score = i32();
    p.resource = i32();
    return p;
}

Entity ReplayReader::entity() {
    Entity e;
    e.id = i32();
    if (boolean()) e.player_id = i32();
    int32_t t = i32();
    if (t < 0 || t >= TYPE_COUNT) throw std::runtime_error("bad entity type in the replay");
    e.entity_type = static_cast<EntityType>(t);
    e.position.x = i32();
    e.position.y = i32();
    e.health = i32();
    e.active = boolean();
    return e;
}

EntityAction ReplayReader::action() {
    EntityAction a;
    if (boolean()) {
        MoveAction m;
        m.target.x = i32();
        m.target.y = i32();
        m.find_closest_position = boolean();
        m.break_through = boolean();
        a.move_action = m;
    }
    if (boolean()) {
        BuildAction b;
        int32_t t = i32();
        if (t < 0 || t >= TYPE_COUNT) throw std::runtime_error("bad entity type in the replay");
        b.entity_type = static_cast<EntityType>(t);
        b.position.x = i32();
        b.position.y = i32();
        a.build_action = b;
    }
    if (boolean()) {
        AttackAction t;
        if (boolean()) t.target = i32();
        if (boolean()) {
            AutoAttack aa;
            aa.pathfind_range = i32();
            aa.valid_targets = types();
            t.auto_attack = aa;
        }
        a.attack_action = t;
    }
    if (boolean()) a.repair_action = RepairAction{i32()};
    return a;
}

Model ReplayReader::model() {
    Model m;
    m.next_id = i32();
    m.map_size = i32();
    m.fog_of_war = boolean();
    m.props = properties();
    m.max_tick_count = i32();
    m.max_pathfind_nodes = i32();
    m.current_tick = i32();
    m.passive_income = i32();
    for (int32_t k = count(); k > 0; k--) {
        i32();
        Player p = player();
        m.players[p.id] = p;
    }
    for (int32_t k = count(); k > 0; k--) {
        i32();
        Entity e = entity();
        m.entities[e.id] = e;
    }
    for (int32_t k = count(); k > 0; k--) {
        i32();
        int32_t id = i32();
        m.entity_actions[id] = action();
    }
    return m;
}

bool ReplayReader::next(Model& m, std::vector<Event>& events) {
    if (pos_ >= data_.size()) return false;
    events.clear();
    for (int32_t k = count(); k > 0; k--) {
        Event e;
        e.tag = i32();
        e.a = i32();
        e.b = i32();
        events.push_back(e);
    }
    m.next_id = i32();
    m.map_size = i32();
    m.fog_of_war = boolean();
    m.max_tick_count = i32();
    m.max_pathfind_nodes = i32();
    m.current_tick = i32();
    m.passive_income = i32();
    for (int32_t k = count(); k > 0; k--) {             // players: added, removed, updated
        Player p = player();
        m.players[p.id] = p;
    }
    for (int32_t k = count(); k > 0; k--) m.players.erase(i32());
    for (int32_t k = count(); k > 0; k--) {
        int32_t id = i32();
        m.players[id] = player();
    }
    for (int32_t k = count(); k > 0; k--) {             // entities
        Entity e = entity();
        m.entities[e.id] = e;
    }
    for (int32_t k = count(); k > 0; k--) m.entities.erase(i32());
    for (int32_t k = count(); k > 0; k--) {
        int32_t id = i32();
        m.entities[id] = entity();
    }
    for (int32_t k = count(); k > 0; k--) {             // stored orders: (entity id, order)
        int32_t id = i32();
        m.entity_actions[id] = action();
    }
    for (int32_t k = count(); k > 0; k--) m.entity_actions.erase(i32());
    for (int32_t k = count(); k > 0; k--) {
        i32();
        int32_t id = i32();
        m.entity_actions[id] = action();
    }
    return true;
}

}  // namespace cc

