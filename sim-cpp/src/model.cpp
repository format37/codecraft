#include "model.hpp"

namespace cc {

const char* const TYPE_NAMES[TYPE_COUNT] = {"Wall",       "House",     "BuilderBase", "BuilderUnit", "MeleeBase",
                                            "MeleeUnit",  "RangedBase", "RangedUnit", "Resource",    "Turret"};
static const char* const PY_NAMES[TYPE_COUNT] = {"WALL",       "HOUSE",       "BUILDER_BASE", "BUILDER_UNIT",
                                                 "MELEE_BASE", "MELEE_UNIT",  "RANGED_BASE",  "RANGED_UNIT",
                                                 "RESOURCE",   "TURRET"};

std::optional<EntityType> parse_type(const std::string& s) {
    for (int k = 0; k < TYPE_COUNT; k++)
        if (s == TYPE_NAMES[k] || s == PY_NAMES[k]) return static_cast<EntityType>(k);
    return std::nullopt;
}

EntityType type_from_json(const Json& j) {
    if (j.kind == Json::String) {
        auto t = parse_type(j.s);
        if (!t) throw JsonError("unknown variant `" + j.s + "`");
        return *t;
    }
    int64_t v = j.as_int();
    if (v < 0 || v >= TYPE_COUNT) throw JsonError("bad entity type " + std::to_string(v));
    return static_cast<EntityType>(v);
}

void Properties::set(EntityType t, EntityProperties p) {
    int k = static_cast<int>(t);
    if (index[k] >= 0) {
        list[static_cast<size_t>(index[k])].second = std::move(p);
    } else {
        index[k] = static_cast<int>(list.size());
        list.emplace_back(t, std::move(p));
    }
}

namespace {
using T = EntityType;

EntityProperties props(int32_t size, int32_t build_score, int32_t destroy_score, bool can_move, int32_t provide,
                       int32_t use, int32_t max_health, int32_t cost, int32_t sight, int32_t rph) {
    EntityProperties p;
    p.size = size;
    p.build_score = build_score;
    p.destroy_score = destroy_score;
    p.can_move = can_move;
    p.population_provide = provide;
    p.population_use = use;
    p.max_health = max_health;
    p.initial_cost = cost;
    p.sight_range = sight;
    p.resource_per_health = rph;
    return p;
}

EntityProperties base(EntityType unit) {
    EntityProperties p = props(5, 500, 5000, false, 5, 0, 300, 500, 5, 0);
    p.build = BuildProperties{{unit}, std::nullopt};
    return p;
}
}  // namespace

Properties default_properties() {
    Properties out;
    out.set(T::House, props(3, 50, 500, false, 5, 0, 50, 50, 5, 0));
    EntityProperties ranged = props(1, 30, 300, true, 0, 1, 10, 30, 10, 0);
    ranged.attack = AttackProperties{5, 5, false};
    out.set(T::RangedUnit, ranged);
    EntityProperties builder = props(1, 10, 100, true, 0, 1, 10, 10, 10, 0);
    builder.build = BuildProperties{{T::House, T::Wall, T::BuilderBase, T::MeleeBase, T::RangedBase, T::Turret}, 5};
    builder.attack = AttackProperties{1, 1, true};
    builder.repair = RepairProperties{{T::House, T::Wall, T::BuilderUnit, T::MeleeUnit, T::RangedUnit, T::BuilderBase,
                                       T::MeleeBase, T::RangedBase, T::Turret}, 1};
    out.set(T::BuilderUnit, builder);
    EntityProperties melee = props(1, 20, 200, true, 0, 1, 50, 20, 10, 0);
    melee.attack = AttackProperties{1, 5, false};
    out.set(T::MeleeUnit, melee);
    out.set(T::Wall, props(1, 10, 10, false, 0, 0, 50, 10, 2, 0));
    out.set(T::Resource, props(1, 0, 0, false, 0, 0, 30, 0, 0, 1));
    EntityProperties turret = props(2, 50, 500, false, 0, 0, 100, 50, 10, 0);
    turret.attack = AttackProperties{5, 5, false};
    out.set(T::Turret, turret);
    out.set(T::BuilderBase, base(T::BuilderUnit));
    out.set(T::RangedBase, base(T::RangedUnit));
    out.set(T::MeleeBase, base(T::MeleeUnit));
    return out;
}

static std::vector<EntityType> types_from_json(const Json& j) {
    std::vector<EntityType> out;
    for (const Json& v : j.arr) out.push_back(type_from_json(v));
    return out;
}

static int32_t i32(const Json& j) { return static_cast<int32_t>(j.as_int()); }

static bool present(const Json& j, const char* key) {
    const Json* v = j.get(key);
    return v && !v->is_null();
}

EntityProperties properties_from_json(const Json& d) {
    EntityProperties p = props(i32(d.at("size")), i32(d.at("build_score")), i32(d.at("destroy_score")),
                               d.at("can_move").as_bool(), i32(d.at("population_provide")),
                               i32(d.at("population_use")), i32(d.at("max_health")), i32(d.at("initial_cost")),
                               i32(d.at("sight_range")), i32(d.at("resource_per_health")));
    if (present(d, "build")) {
        const Json& b = d.at("build");
        BuildProperties bp{types_from_json(b.at("options")), std::nullopt};
        if (present(b, "init_health")) bp.init_health = i32(b.at("init_health"));
        p.build = bp;
    }
    if (present(d, "attack")) {
        const Json& a = d.at("attack");
        p.attack = AttackProperties{i32(a.at("range")), i32(a.at("damage")), a.at("collect_resource").as_bool()};
    }
    if (present(d, "repair")) {
        const Json& r = d.at("repair");
        p.repair = RepairProperties{types_from_json(r.at("valid_targets")), i32(r.at("power"))};
    }
    return p;
}

Vec2 vec_from_json(const Json& j) { return Vec2{i32(j.at("x")), i32(j.at("y"))}; }

EntityAction action_from_json(const Json& d) {
    EntityAction a;
    if (present(d, "move_action")) {
        const Json& m = d.at("move_action");
        a.move_action = MoveAction{vec_from_json(m.at("target")), m.at("find_closest_position").as_bool(),
                                   m.at("break_through").as_bool()};
    }
    if (present(d, "build_action")) {
        const Json& b = d.at("build_action");
        a.build_action = BuildAction{type_from_json(b.at("entity_type")), vec_from_json(b.at("position"))};
    }
    if (present(d, "attack_action")) {
        const Json& at = d.at("attack_action");
        AttackAction aa;
        if (present(at, "target")) aa.target = i32(at.at("target"));
        if (present(at, "auto_attack")) {
            const Json& au = at.at("auto_attack");
            aa.auto_attack = AutoAttack{i32(au.at("pathfind_range")), types_from_json(au.at("valid_targets"))};
        }
        a.attack_action = aa;
    }
    if (present(d, "repair_action")) a.repair_action = RepairAction{i32(d.at("repair_action").at("target"))};
    return a;
}

static int32_t key_of(const std::string& k) {
    try {
        size_t used = 0;
        long v = std::stol(k, &used);
        if (used != k.size()) throw JsonError("bad id " + k);
        return static_cast<int32_t>(v);
    } catch (const std::logic_error&) {
        throw JsonError("bad id " + k);
    }
}

Model model_from_json(const Json& d) {
    Model m;
    m.next_id = i32(d.at("next_id"));
    m.map_size = i32(d.at("map_size"));
    m.fog_of_war = d.at("fog_of_war").as_bool();
    for (const auto& kv : d.at("entity_properties").obj) {
        auto t = parse_type(kv.first);
        if (!t) throw JsonError("unknown variant `" + kv.first + "`");
        m.props.set(*t, properties_from_json(kv.second));
    }
    m.max_tick_count = i32(d.at("max_tick_count"));
    m.max_pathfind_nodes = i32(d.at("max_pathfind_nodes"));
    m.current_tick = i32(d.at("current_tick"));
    m.passive_income = i32(d.at("passive_income"));
    for (const auto& kv : d.at("players").at("by_id").obj) {
        const Json& p = kv.second;
        m.players[key_of(kv.first)] = Player{i32(p.at("id")), i32(p.at("index")), i32(p.at("score")),
                                             i32(p.at("resource"))};
    }
    for (const auto& kv : d.at("entities").at("by_id").obj) {
        const Json& e = kv.second;
        Entity ent;
        ent.id = i32(e.at("id"));
        if (present(e, "player_id")) ent.player_id = i32(e.at("player_id"));
        ent.entity_type = type_from_json(e.at("entity_type"));
        ent.position = vec_from_json(e.at("position"));
        ent.health = i32(e.at("health"));
        ent.active = e.at("active").as_bool();
        m.entities[key_of(kv.first)] = ent;
    }
    for (const auto& kv : d.at("entity_actions").at("by_id").obj)
        m.entity_actions[key_of(kv.first)] = action_from_json(kv.second.at("action"));
    return m;
}

}  // namespace cc
