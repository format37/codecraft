#include "rules.hpp"

#include <algorithm>
#include <unordered_map>
#include <unordered_set>

#include "pathfind.hpp"
#include "view.hpp"

namespace cc {

int distance(Vec2 a, int a_size, Vec2 b, int b_size) {
    int dx = std::max({0, b.x - (a.x + a_size - 1), a.x - (b.x + b_size - 1)});
    int dy = std::max({0, b.y - (a.y + a_size - 1), a.y - (b.y + b_size - 1)});
    return dx + dy;
}

std::set<int32_t> players_with_entities(const Model& m) {
    std::set<int32_t> out;
    for (const auto& kv : m.entities)
        if (kv.second.player_id) out.insert(*kv.second.player_id);
    return out;
}

bool game_over(const Model& m) {
    return m.current_tick >= m.max_tick_count || players_with_entities(m).size() <= 1;
}

int unit_cost(const Model& m, int32_t player_id, EntityType t) {
    const EntityProperties& p = m.props[t];
    if (!p.can_move) return p.initial_cost;
    int n = 0;
    for (const auto& kv : m.entities)
        if (kv.second.player_id == player_id && kv.second.entity_type == t) n++;
    return p.initial_cost + n;
}

static std::pair<int, int> population(const Model& m, int32_t player_id) {
    int provided = 0, used = 0;
    for (const auto& kv : m.entities) {
        const Entity& e = kv.second;
        if (e.player_id == player_id) {
            const EntityProperties& p = m.props[e.entity_type];
            used += p.population_use;
            if (e.active) provided += p.population_provide;
        }
    }
    return {provided, used};
}

static bool any_seen(const std::vector<uint8_t>& seen, int n, Vec2 pos, int size) {
    if (size == 1) return seen[static_cast<size_t>(pos.y * n + pos.x)] != 0;
    for (int y = pos.y; y < std::min(pos.y + size, n); y++)
        for (int x = pos.x; x < std::min(pos.x + size, n); x++)
            if (seen[static_cast<size_t>(y * n + x)]) return true;
    return false;
}

void FogMemory::update(const Model& m, const SightMaps& sight) {
    const int n = m.map_size;
    for (const auto& pk : m.players) {
        const int32_t pid = pk.first;
        const std::vector<uint8_t>& seen = sight.at(pid);
        std::map<int32_t, Remembered>& known = known_[pid];
        bool changed = false;                                   // what is remembered where (not just a health)
        for (auto it = known.begin(); it != known.end();) {
            if (!m.entities.count(it->first) && any_seen(seen, n, it->second.position, it->second.size)) {
                it = known.erase(it);
                changed = true;
            } else {
                ++it;
            }
        }
        for (const auto& kv : m.entities) {
            const Entity& e = kv.second;
            const EntityProperties& p = m.props[e.entity_type];
            if (e.player_id == pid || p.can_move || !any_seen(seen, n, e.position, p.size)) continue;
            auto [it, added] = known.insert_or_assign(e.id, Remembered{e.player_id, e.position, p.size, e.health});
            changed = changed || added;
        }
        if (changed) cells_.erase(pid);
    }
}

const std::vector<int32_t>& FogMemory::cells(int32_t player_id, int n) {
    auto it = cells_.find(player_id);
    if (it != cells_.end()) return it->second;
    std::vector<int32_t> cells(static_cast<size_t>(n) * n, FREE);
    for (const auto& kv : known_[player_id]) {
        const Remembered& r = kv.second;
        for (int y = r.position.y; y < std::min(r.position.y + r.size, n); y++)
            for (int x = r.position.x; x < std::min(r.position.x + r.size, n); x++)
                cells[static_cast<size_t>(y * n + x)] = kv.first;
    }
    return cells_.emplace(player_id, std::move(cells)).first->second;
}

namespace {

bool contains(const std::vector<EntityType>& v, EntityType t) { return std::find(v.begin(), v.end(), t) != v.end(); }

class Turn {
public:
    Turn(Model& m, PyRandom& rng, const SightMaps* seen, FogMemory* memory)
        : m(m), rng(rng), props(m.props), grid(Grid::of(m)), memory(m.fog_of_war ? memory : nullptr) {
        for (const auto& kv : m.entities) ids.push_back(kv.first);
        if (seen) seen_ = *seen;
    }

    std::vector<Event> play() {
        if (memory) {
            for (const auto& kv : m.players) sight(kv.first);
            memory->update(m, seen_);
        }
        std::set<int32_t> playing = players_with_entities(m);
        plan();
        attacks();
        builds();
        repairs();
        movement();
        end_of_tick(playing);
        return events;
    }

private:
    Model& m;
    PyRandom& rng;
    const Properties& props;
    Grid grid;
    std::vector<int32_t> ids;                          // the entities at the start of the tick, by id
    std::vector<Event> events;
    std::unordered_set<int32_t> busy;                 // attacked, built or repaired this tick
    std::vector<std::pair<int32_t, Vec2>> steps;      // the cell each unit would step into, by id
    std::unordered_map<int32_t, Vec2> step_of;
    std::map<std::pair<int32_t, std::vector<EntityType>>, std::vector<char>> goals_;
    SightMaps seen_;
    FogMemory* memory;

    const EntityAction* order_of(const Entity& e) const {
        if (!e.active) return nullptr;
        auto it = m.entity_actions.find(e.id);
        return it == m.entity_actions.end() ? nullptr : &it->second;
    }

    // 1. pathfinding --------------------------------------------------------------------------------------
    void plan() {
        for (int32_t eid : ids) {
            std::optional<Vec2> s = plan_unit(m.entities.at(eid));
            if (s) {
                steps.emplace_back(eid, *s);
                step_of[eid] = *s;
            }
        }
    }

    std::optional<Vec2> plan_unit(const Entity& e) {
        const EntityAction* a = order_of(e);
        const EntityProperties& p = props[e.entity_type];
        if (!a || !p.can_move) return std::nullopt;
        std::optional<Vec2> step;
        if (a->attack_action && p.attack) {
            const AttackAction& at = *a->attack_action;
            if (named_target(e, at.target)) return std::nullopt;
            if (at.auto_attack) {
                if (!in_range(e, *at.auto_attack).empty()) return std::nullopt;
                step = hunt(e, *at.auto_attack);
            }
        }
        if (!step && a->move_action) step = move_step(e, *a->move_action);
        return step;
    }

    std::optional<Vec2> move_step(const Entity& e, const MoveAction& mv) {
        Vec2 t = mv.target, pos = e.position;
        int d = std::abs(t.x - pos.x) + std::abs(t.y - pos.y);
        if (d == 0) return std::nullopt;
        if (d == 1) return t;
        return first_step(grid, pos, t, m.max_pathfind_nodes, entry_cost(e, mv.break_through), rng);
    }

    Cost entry_cost(const Entity& e, bool break_through) {
        Cost c;
        c.cells = &grid.cells;
        c.seen = sight(e.player_id);
        if (c.seen && memory) {
            c.memory = &memory->cells(*e.player_id, m.map_size);
            c.known = &memory->known(*e.player_id);
        }
        c.break_through = break_through;
        if (break_through) {
            const auto& attack = props[e.entity_type].attack;
            c.damage = attack ? attack->damage : 0;
            c.own = e.player_id;
            c.entities = &m.entities;
        }
        return c;
    }

    const std::vector<uint8_t>* sight(std::optional<int32_t> player_id) {
        if (!m.fog_of_war) return nullptr;
        int32_t key = player_id ? *player_id : INT32_MIN;
        auto it = seen_.find(key);
        if (it == seen_.end()) it = seen_.emplace(key, sight_map(m, player_id)).first;
        return &it->second;
    }

    std::optional<Vec2> hunt(const Entity& e, const AutoAttack& aa) {
        const std::vector<char>& goals = goal_cells(*e.player_id, aa.valid_targets);
        return hunt_step(grid, e.position, goals, aa.pathfind_range, m.max_pathfind_nodes, entry_cost(e, false), rng);
    }

    const std::vector<char>& goal_cells(int32_t player_id, const std::vector<EntityType>& valid) {
        auto key = std::make_pair(player_id, valid);
        auto it = goals_.find(key);
        if (it != goals_.end()) return it->second;
        std::vector<char> goals(grid.cells.size(), 0);
        for (const auto& kv : m.entities)
            if (is_enemy(player_id, kv.second, valid))
                grid.around(kv.second.position, props[kv.second.entity_type].size, 1,
                            [&](int i) { goals[static_cast<size_t>(i)] = 1; });
        return goals_.emplace(key, std::move(goals)).first->second;
    }

    static bool is_enemy(std::optional<int32_t> player_id, const Entity& t, const std::vector<EntityType>& valid) {
        if (t.player_id == player_id) return false;
        return valid.empty() ? t.player_id.has_value() : contains(valid, t.entity_type);
    }

    std::vector<int32_t> in_range(const Entity& e, const AutoAttack& aa) const {
        const EntityProperties& p = props[e.entity_type];
        std::vector<int32_t> found;
        grid.around(e.position, p.size, p.attack->attack_range, [&](int i) {
            int32_t o = grid.cells[static_cast<size_t>(i)];
            if (o != FREE) found.push_back(o);
        });
        std::sort(found.begin(), found.end());
        found.erase(std::unique(found.begin(), found.end()), found.end());
        std::vector<int32_t> out;
        for (int32_t id : found)
            if (is_enemy(e.player_id, m.entities.at(id), aa.valid_targets)) out.push_back(id);
        return out;
    }

    // 2. attacks -------------------------------------------------------------------------------------------
    void attacks() {
        std::vector<int32_t> attackers;
        for (int32_t eid : ids) {
            const Entity& e = m.entities.at(eid);
            const EntityAction* a = order_of(e);
            if (a && props[e.entity_type].attack && (a->attack_action || step_of.count(eid))) attackers.push_back(eid);
        }
        rng.shuffle(attackers);
        for (int32_t eid : attackers) {
            Entity& e = m.entities.at(eid);
            auto st = step_of.find(eid);
            std::optional<Vec2> step;
            if (st != step_of.end()) step = st->second;
            Entity* t = attack_target(e, m.entity_actions.at(eid), step);
            if (t) {
                hit(e, *t);
                busy.insert(eid);
            }
        }
    }

    Entity* named_target(const Entity& e, std::optional<int32_t> target_id) {
        if (!target_id) return nullptr;
        auto it = m.entities.find(*target_id);
        if (it == m.entities.end()) return nullptr;
        const EntityProperties& p = props[e.entity_type];
        int t_size = props[it->second.entity_type].size;
        if (distance(e.position, p.size, it->second.position, t_size) <= p.attack->attack_range) return &it->second;
        return nullptr;
    }

    Entity* attack_target(const Entity& e, const EntityAction& a, std::optional<Vec2> step) {
        if (a.attack_action) {
            const AttackAction& at = *a.attack_action;
            Entity* t = named_target(e, at.target);
            if (t) return t;
            if (at.auto_attack) {
                std::vector<int32_t> enemies = in_range(e, *at.auto_attack);
                if (!enemies.empty()) return &m.entities.at(rng.choice(enemies));
            }
        }
        if (step) {
            int32_t o = grid.occupant(*step);
            if (o != FREE && m.entities.at(o).player_id != e.player_id) return &m.entities.at(o);
        }
        return nullptr;
    }

    void hit(const Entity& e, Entity& t) {
        const AttackProperties& attack = *props[e.entity_type].attack;
        const EntityProperties& tp = props[t.entity_type];
        events.push_back(Event{0, e.id, t.id});
        int before = t.health;
        t.health -= attack.damage;
        if (!e.player_id) return;
        auto pit = m.players.find(*e.player_id);
        if (pit == m.players.end()) return;
        Player& player = pit->second;
        if (attack.collect_resource && tp.resource_per_health && before > 0) {
            int gained = std::min(attack.damage, before) * tp.resource_per_health;
            player.resource += gained;
            player.score += gained;
        }
        if (before > 0 && t.health <= 0 && t.player_id != e.player_id) player.score += tp.destroy_score;
    }

    // 3. builds --------------------------------------------------------------------------------------------
    void builds() {
        std::vector<int32_t> builders;
        for (int32_t eid : ids) {
            const Entity& e = m.entities.at(eid);
            const EntityAction* a = order_of(e);
            if (a && a->build_action && !busy.count(eid) && props[e.entity_type].build) builders.push_back(eid);
        }
        rng.shuffle(builders);
        for (int32_t eid : builders)
            if (build(m.entities.at(eid), m.entity_actions.at(eid))) busy.insert(eid);
    }

    bool build(const Entity& e, const EntityAction& a) {
        const BuildAction& b = *a.build_action;
        const BuildProperties& bp = *props[e.entity_type].build;
        if (!contains(bp.options, b.entity_type)) return false;
        int32_t new_id = m.next_id;               // from here on an attempt uses up an id, a failed one too
        m.next_id++;
        if (!can_build(e, b)) return false;
        const EntityProperties& tp = props[b.entity_type];
        int32_t owner = *e.player_id;
        m.players.at(owner).resource -= unit_cost(m, owner, b.entity_type);
        int health = bp.init_health ? *bp.init_health : tp.max_health;
        Entity ne{new_id, owner, b.entity_type, b.position, health, false};
        m.entities[new_id] = ne;
        grid.place(new_id, b.position, tp.size);
        return true;
    }

    bool can_build(const Entity& e, const BuildAction& b) const {
        const EntityProperties& tp = props[b.entity_type];
        if (distance(e.position, props[e.entity_type].size, b.position, tp.size) != 1) return false;
        if (!grid.free_square(b.position, tp.size)) return false;
        if (!e.player_id) return false;
        auto pit = m.players.find(*e.player_id);
        if (pit == m.players.end() || pit->second.resource < unit_cost(m, *e.player_id, b.entity_type)) return false;
        if (tp.population_use) {
            auto pu = population(m, *e.player_id);
            if (pu.second + tp.population_use > pu.first) return false;
        }
        return true;
    }

    // 4. repairs -------------------------------------------------------------------------------------------
    void repairs() {
        for (int32_t eid : ids) {
            const Entity& e = m.entities.at(eid);
            const EntityAction* a = order_of(e);
            const auto& rp = props[e.entity_type].repair;
            if (!a || !a->repair_action || !rp || busy.count(eid)) continue;
            auto it = m.entities.find(a->repair_action->target);
            if (it == m.entities.end()) continue;
            Entity& t = it->second;
            if (t.health <= 0 || !contains(rp->valid_targets, t.entity_type)) continue;
            const EntityProperties& tp = props[t.entity_type];
            if (distance(e.position, props[e.entity_type].size, t.position, tp.size) != 1) continue;
            t.health = std::min(tp.max_health, t.health + rp->power);
            busy.insert(eid);
        }
    }

    // 5. movement ------------------------------------------------------------------------------------------
    void movement() {
        std::vector<std::pair<int32_t, Vec2>> pending;
        for (const auto& s : steps)
            if (!busy.count(s.first)) pending.push_back(s);
        while (!pending.empty()) {
            // the free cells wanted, in the order of their first claimant, each with its claimants in order
            std::vector<std::pair<Vec2, std::vector<int32_t>>> wanted;
            for (const auto& pc : pending) {
                if (!grid.is_free(pc.second)) continue;
                auto w = std::find_if(wanted.begin(), wanted.end(),
                                      [&](const auto& x) { return x.first == pc.second; });
                if (w == wanted.end()) wanted.push_back({pc.second, {pc.first}});
                else w->second.push_back(pc.first);
            }
            if (wanted.empty()) break;
            std::unordered_set<int32_t> done;
            for (const auto& w : wanted) {
                const std::vector<int32_t>& eids = w.second;
                int32_t eid = eids.size() == 1 ? eids[0] : rng.choice(eids);
                Entity& e = m.entities.at(eid);
                grid.clear(e.position, 1);
                e.position = w.first;
                grid.place(eid, w.first, 1);
                for (int32_t loser : eids) done.insert(loser);
            }
            pending.erase(std::remove_if(pending.begin(), pending.end(),
                                         [&](const auto& pc) { return done.count(pc.first) > 0; }),
                          pending.end());
        }
    }

    // 6. the end of the tick -------------------------------------------------------------------------------
    void end_of_tick(const std::set<int32_t>& playing) {
        for (auto it = m.entities.begin(); it != m.entities.end();) {
            if (it->second.health <= 0) it = m.entities.erase(it);
            else ++it;
        }
        for (auto& kv : m.entities) {
            Entity& e = kv.second;
            const EntityProperties& p = props[e.entity_type];
            if (!e.active && e.health >= p.max_health) {
                e.active = true;
                if (e.player_id) {
                    auto pit = m.players.find(*e.player_id);
                    if (pit != m.players.end()) pit->second.score += p.build_score;
                }
            }
        }
        for (auto& kv : m.players) kv.second.resource += m.passive_income;
        m.current_tick++;
        std::set<int32_t> left = players_with_entities(m);
        if (m.players.size() == 2 && playing.size() == 2 && left.size() == 1) {
            int32_t winner = *left.begin();
            for (auto& kv : m.players)
                if (kv.first != winner) m.players.at(winner).score += kv.second.score;
        }
    }
};

}  // namespace

std::vector<Event> process_turn(Model& m, PyRandom& rng, const SightMaps* seen, FogMemory* memory) {
    return Turn(m, rng, seen, memory).play();
}

}  // namespace cc
