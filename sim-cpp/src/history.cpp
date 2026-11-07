#include "history.hpp"

#include <stdexcept>

namespace cc {

TickDiff diff(const Model& before, const Model& after) {
    TickDiff d;
    d.next_id = after.next_id;
    d.current_tick = after.current_tick;
    for (const auto& kv : after.players) d.players.push_back(kv.second);
    auto a = before.entities.begin(), b = after.entities.begin();
    while (a != before.entities.end() || b != after.entities.end()) {
        if (b == after.entities.end() || (a != before.entities.end() && a->first < b->first)) {
            d.removed.push_back(a->first);
            ++a;
        } else if (a == before.entities.end() || b->first < a->first) {
            d.set.push_back(b->second);
            ++b;
        } else {
            if (a->second != b->second) d.set.push_back(b->second);
            ++a;
            ++b;
        }
    }
    auto p = before.entity_actions.begin(), q = after.entity_actions.begin();
    while (p != before.entity_actions.end() || q != after.entity_actions.end()) {
        if (q == after.entity_actions.end() || (p != before.entity_actions.end() && p->first < q->first)) {
            d.orders_removed.push_back(p->first);
            ++p;
        } else if (p == before.entity_actions.end() || q->first < p->first) {
            d.orders.emplace_back(q->first, q->second);
            ++q;
        } else {
            if (p->second != q->second) d.orders.emplace_back(q->first, q->second);
            ++p;
            ++q;
        }
    }
    return d;
}

void apply(Model& m, const TickDiff& d) {
    m.next_id = d.next_id;
    m.current_tick = d.current_tick;
    for (const Player& p : d.players) m.players[p.id] = p;
    for (int32_t id : d.removed) m.entities.erase(id);
    for (const Entity& e : d.set) m.entities[e.id] = e;
    for (int32_t id : d.orders_removed) m.entity_actions.erase(id);
    for (const auto& kv : d.orders) m.entity_actions[kv.first] = kv.second;
}

void entity_json(Out& o, const Entity& e) {
    o.raw('[').num(e.id).raw(',');
    if (e.player_id) o.num(*e.player_id);
    else o.raw("null");
    o.raw(',').num(static_cast<int>(e.entity_type)).raw(',').num(e.position.x).raw(',').num(e.position.y);
    o.raw(',').num(e.health).raw(',').num(e.active ? 1 : 0).raw(']');
}

static void types_json(Out& o, const std::vector<EntityType>& types) {
    o.raw('[');
    for (EntityType t : types) o.sep().num(static_cast<int>(t));
    o.raw(']');
}

void order_json(Out& o, const EntityAction& a) {
    o.raw('{');
    if (a.move_action) {
        const MoveAction& m = *a.move_action;
        o.key("move").raw("{\"target\":[").num(m.target.x).raw(',').num(m.target.y).raw("],");
        o.raw("\"find_closest_position\":").boolean(m.find_closest_position);
        o.raw(",\"break_through\":").boolean(m.break_through).raw('}');
    }
    if (a.build_action) {
        const BuildAction& b = *a.build_action;
        o.key("build").raw("{\"entity_type\":").num(static_cast<int>(b.entity_type));
        o.raw(",\"position\":[").num(b.position.x).raw(',').num(b.position.y).raw("]}");
    }
    if (a.attack_action) {
        const AttackAction& t = *a.attack_action;
        o.key("attack").raw("{\"target\":");
        if (t.target) o.num(*t.target);
        else o.raw("null");
        o.raw(",\"auto_attack\":");
        if (t.auto_attack) {
            o.raw("{\"pathfind_range\":").num(t.auto_attack->pathfind_range).raw(",\"valid_targets\":");
            types_json(o, t.auto_attack->valid_targets);
            o.raw('}');
        } else {
            o.raw("null");
        }
        o.raw('}');
    }
    if (a.repair_action) o.key("repair").raw("{\"target\":").num(a.repair_action->target).raw('}');
    o.raw('}');
}

static const char* const UPPER_NAMES[TYPE_COUNT] = {"WALL", "HOUSE", "BUILDER_BASE", "BUILDER_UNIT", "MELEE_BASE",
                                                     "MELEE_UNIT", "RANGED_BASE", "RANGED_UNIT", "RESOURCE",
                                                     "TURRET"};

void properties_json(Out& o, const Properties& props) {
    o.raw('{');
    for (const auto& kv : props.list) {
        const EntityProperties& p = kv.second;
        o.key(UPPER_NAMES[static_cast<int>(kv.first)]).raw('{');
        o.key("size").num(p.size).key("build_score").num(p.build_score).key("destroy_score").num(p.destroy_score);
        o.key("can_move").boolean(p.can_move).key("population_provide").num(p.population_provide);
        o.key("population_use").num(p.population_use).key("max_health").num(p.max_health);
        o.key("initial_cost").num(p.initial_cost).key("sight_range").num(p.sight_range);
        o.key("resource_per_health").num(p.resource_per_health);
        o.key("build");
        if (p.build) {
            o.raw("{\"options\":");
            types_json(o, p.build->options);
            o.raw(",\"init_health\":");
            if (p.build->init_health) o.num(*p.build->init_health);
            else o.raw("null");
            o.raw('}');
        } else {
            o.raw("null");
        }
        o.key("attack");
        if (p.attack) {
            o.raw("{\"attack_range\":").num(p.attack->attack_range).raw(",\"damage\":").num(p.attack->damage);
            o.raw(",\"collect_resource\":").boolean(p.attack->collect_resource).raw('}');
        } else {
            o.raw("null");
        }
        o.key("repair");
        if (p.repair) {
            o.raw("{\"valid_targets\":");
            types_json(o, p.repair->valid_targets);
            o.raw(",\"power\":").num(p.repair->power).raw('}');
        } else {
            o.raw("null");
        }
        o.raw('}');
    }
    o.raw('}');
}

std::string tick_json(const TickDiff& d, const std::vector<Event>& events) {
    Out o;
    o.raw('{').key("t").num(d.current_tick);
    o.key("p").raw('[');
    for (const Player& p : d.players)
        o.sep().raw('[').num(p.id).raw(',').num(p.index).raw(',').num(p.score).raw(',').num(p.resource).raw(']');
    o.raw(']');
    if (!d.removed.empty()) {
        o.key("r").raw('[');
        for (int32_t id : d.removed) o.sep().num(id);
        o.raw(']');
    }
    if (!d.set.empty()) {
        o.key("u").raw('[');
        for (const Entity& e : d.set) {
            o.sep();
            entity_json(o, e);
        }
        o.raw(']');
    }
    if (!d.orders.empty()) {
        o.key("act").raw('{');
        for (const auto& kv : d.orders) {
            o.key(std::to_string(kv.first).c_str());
            order_json(o, kv.second);
        }
        o.raw('}');
    }
    if (!d.orders_removed.empty()) {
        o.key("actr").raw('[');
        for (int32_t id : d.orders_removed) o.sep().num(id);
        o.raw(']');
    }
    if (!events.empty()) {
        o.key("ev").raw('[');
        for (const Event& e : events) o.sep().raw('[').num(e.tag).raw(',').num(e.a).raw(',').num(e.b).raw(']');
        o.raw(']');
    }
    o.raw('}');
    return o.s;
}

void History::start(const Model& m) {
    std::lock_guard<std::mutex> lock(mutex_);
    first_ = m.current_tick;
    keys_.assign(1, m);
    diffs_.clear();
    cached_tick_ = -1;
}

void History::push(const Model& m, const TickDiff& d) {
    std::lock_guard<std::mutex> lock(mutex_);
    diffs_.push_back(d);
    if (diffs_.size() % KEY_EVERY == 0) keys_.push_back(m);
}

bool History::has(int tick) {
    std::lock_guard<std::mutex> lock(mutex_);
    return !keys_.empty() && tick >= first_ && tick <= first_ + static_cast<int>(diffs_.size());
}

Model History::at(int tick) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (keys_.empty() || tick < first_ || tick > first_ + static_cast<int>(diffs_.size()))
        throw std::out_of_range("tick " + std::to_string(tick) + " was not played");
    if (tick == cached_tick_) return cached_;
    int k = (tick - first_) / KEY_EVERY;
    Model m = keys_[static_cast<size_t>(k)];
    for (int i = k * KEY_EVERY; i < tick - first_; i++) apply(m, diffs_[static_cast<size_t>(i)]);
    cached_tick_ = tick;
    cached_ = m;
    return m;
}

}  // namespace cc
