// The game state, as sim/codecraft_sim/model.py: every type mirrors one of the official engine's (the field orders of
// the TCP protocol and of the replay), and the engine's serde JSON (LoadFrom state files) reads into it.
#pragma once
#include <array>
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "json.hpp"

namespace cc {

enum class EntityType : int32_t {
    Wall = 0, House, BuilderBase, BuilderUnit, MeleeBase, MeleeUnit, RangedBase, RangedUnit, Resource, Turret
};
constexpr int TYPE_COUNT = 10;
extern const char* const TYPE_NAMES[TYPE_COUNT];         // the JSON names: "Wall", "House", ...
std::optional<EntityType> parse_type(const std::string& s);   // a JSON name or the Python one ("BUILDER_UNIT")
EntityType type_from_json(const Json& j);

struct Vec2 {
    int32_t x = 0, y = 0;
    bool operator==(const Vec2& o) const { return x == o.x && y == o.y; }
    bool operator!=(const Vec2& o) const { return !(*this == o); }
    bool operator<(const Vec2& o) const { return x != o.x ? x < o.x : y < o.y; }
};

struct AttackProperties {
    int32_t attack_range, damage;
    bool collect_resource;
    bool operator==(const AttackProperties& o) const {
        return attack_range == o.attack_range && damage == o.damage && collect_resource == o.collect_resource;
    }
};
struct BuildProperties {
    std::vector<EntityType> options;
    std::optional<int32_t> init_health;
    bool operator==(const BuildProperties& o) const { return options == o.options && init_health == o.init_health; }
};
struct RepairProperties {
    std::vector<EntityType> valid_targets;
    int32_t power;
    bool operator==(const RepairProperties& o) const { return valid_targets == o.valid_targets && power == o.power; }
};
struct EntityProperties {
    int32_t size, build_score, destroy_score;
    bool can_move;
    int32_t population_provide, population_use, max_health, initial_cost, sight_range, resource_per_health;
    std::optional<BuildProperties> build;
    std::optional<AttackProperties> attack;
    std::optional<RepairProperties> repair;
};

// The property table, in its own order (the order it is written in, on the wire and in the replay).
struct Properties {
    std::vector<std::pair<EntityType, EntityProperties>> list;
    std::array<int, TYPE_COUNT> index;
    Properties() { index.fill(-1); }
    const EntityProperties& operator[](EntityType t) const {
        return list[static_cast<size_t>(index[static_cast<int>(t)])].second;
    }
    bool has(EntityType t) const { return index[static_cast<int>(t)] >= 0; }
    void set(EntityType t, EntityProperties p);
};
Properties default_properties();        // docs/doc-en.md, in the order of model.DEFAULT_PROPERTIES

struct MoveAction {
    Vec2 target;
    bool find_closest_position = true, break_through = false;
    bool operator==(const MoveAction& o) const {
        return target == o.target && find_closest_position == o.find_closest_position &&
               break_through == o.break_through;
    }
};
struct BuildAction {
    EntityType entity_type;
    Vec2 position;
    bool operator==(const BuildAction& o) const { return entity_type == o.entity_type && position == o.position; }
};
struct AutoAttack {
    int32_t pathfind_range;
    std::vector<EntityType> valid_targets;
    bool operator==(const AutoAttack& o) const {
        return pathfind_range == o.pathfind_range && valid_targets == o.valid_targets;
    }
};
struct AttackAction {
    std::optional<int32_t> target;
    std::optional<AutoAttack> auto_attack;
    bool operator==(const AttackAction& o) const { return target == o.target && auto_attack == o.auto_attack; }
};
struct RepairAction {
    int32_t target;
    bool operator==(const RepairAction& o) const { return target == o.target; }
};
struct EntityAction {
    std::optional<MoveAction> move_action;
    std::optional<BuildAction> build_action;
    std::optional<AttackAction> attack_action;
    std::optional<RepairAction> repair_action;
    bool operator==(const EntityAction& o) const {
        return move_action == o.move_action && build_action == o.build_action && attack_action == o.attack_action &&
               repair_action == o.repair_action;
    }
    bool operator!=(const EntityAction& o) const { return !(*this == o); }
};

struct Player {
    int32_t id, index, score = 0, resource = 0;
    bool operator==(const Player& o) const {
        return id == o.id && index == o.index && score == o.score && resource == o.resource;
    }
    bool operator!=(const Player& o) const { return !(*this == o); }
};

struct Entity {
    int32_t id;
    std::optional<int32_t> player_id;
    EntityType entity_type;
    Vec2 position;
    int32_t health;
    bool active = true;
    bool operator==(const Entity& o) const {
        return id == o.id && player_id == o.player_id && entity_type == o.entity_type && position == o.position &&
               health == o.health && active == o.active;
    }
    bool operator!=(const Entity& o) const { return !(*this == o); }
};

// Players, entities and stored orders by id: the order the Python engine keeps them in.
struct Model {
    int32_t next_id = 1, map_size = 80;
    bool fog_of_war = false;
    Properties props;
    int32_t max_tick_count = 1000, max_pathfind_nodes = 1000, current_tick = 0, passive_income = 0;
    std::map<int32_t, Player> players;
    std::map<int32_t, Entity> entities;
    std::map<int32_t, EntityAction> entity_actions;
};

struct Event {
    int32_t tag, a, b;
};

struct ViewPlayer {
    int32_t id, score, resource;
};

struct PlayerView {
    int32_t my_id, map_size;
    bool fog_of_war;
    const Properties* props;
    int32_t max_tick_count, max_pathfind_nodes, current_tick;
    std::vector<ViewPlayer> players;
    std::vector<Entity> entities;
};

// JSON (the engine's serde form)
EntityProperties properties_from_json(const Json& j);
EntityAction action_from_json(const Json& j);
Vec2 vec_from_json(const Json& j);
Model model_from_json(const Json& j);

}  // namespace cc
