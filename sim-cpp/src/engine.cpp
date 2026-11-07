#include "engine.hpp"

#include <algorithm>
#include <fstream>
#include <memory>
#include <random>
#include <sstream>
#include <stdexcept>
#include <thread>

#include "mapgen.hpp"
#include "replay.hpp"
#include "rules.hpp"
#include "view.hpp"

namespace cc {

namespace {

struct ConfigError : std::runtime_error {
    using std::runtime_error::runtime_error;
};

Model load_game(const Json& config, int seats, PyRandom& rng) {
    const Json* game = config.get("game");
    if (!game || game->kind != Json::Object || game->obj.size() != 1)
        throw ConfigError("`game` must be {\"Create\": ...} or {\"LoadFrom\": \"/path/state.json\"}");
    const std::string& kind = game->obj[0].first;
    const Json& value = game->obj[0].second;
    if (kind == "LoadFrom") {
        Model m;
        std::string path;
        try {
            path = value.as_string();
            std::ifstream f(path);
            if (!f) throw JsonError("cannot read it");
            std::stringstream ss;
            ss << f.rdbuf();
            m = model_from_json(parse_json(ss.str()));
        } catch (const std::exception& e) {
            throw ConfigError("Could not load the state " + path + ": " + e.what());
        }
        std::vector<int> indexes;
        for (const auto& kv : m.players) indexes.push_back(kv.second.index);
        std::sort(indexes.begin(), indexes.end());
        std::vector<int> wanted;
        for (int k = 0; k < seats; k++) wanted.push_back(k);
        if (indexes != wanted)
            throw ConfigError("the state's players do not fit the config's " + std::to_string(seats) + " seats");
        return m;
    }
    if (kind == "Create") {
        try {
            Options o;
            if (value.kind == Json::String) o = preset(value.s);
            else if (value.kind == Json::Object && value.obj.size() == 1 && value.obj[0].first == "Custom")
                o = options_from_json(value.obj[0].second);
            else throw JsonError("unknown game");
            return create(o, seats, rng);
        } catch (const std::exception& e) {
            throw ConfigError(std::string("Could not load config: ") + e.what());
        }
    }
    throw ConfigError("unknown variant `" + kind + "`, expected `Create` or `LoadFrom`");
}

std::optional<double> opt_seconds(const Json* v) {
    if (!v || v->is_null()) return std::nullopt;
    return v->as_double();
}

std::unique_ptr<Seat> make_seat(const Json& spec, Log& log) {
    std::string kind;
    const Json* value = nullptr;
    if (spec.kind == Json::String) {
        kind = spec.s;
    } else if (spec.kind == Json::Object && spec.obj.size() == 1) {
        kind = spec.obj[0].first;
        value = &spec.obj[0].second;
    } else {
        throw ConfigError("unknown player");
    }
    if (kind == "Empty") return std::make_unique<EmptyPlayer>();
    if (kind == "QuickStart") return std::make_unique<QuickStartPlayer>();
    if (kind == "Tcp" && value) {
        try {
            return std::make_unique<TcpPlayer>(value->at("host").as_string(),
                                               static_cast<int>(value->at("port").as_int()),
                                               opt_seconds(value->get("accept_timeout")),
                                               opt_seconds(value->get("timeout")), value->at("token").as_string(), log);
        } catch (const JsonError& e) {
            throw ConfigError(std::string("bad Tcp player: ") + e.what());
        }
    }
    throw ConfigError("unknown player `" + kind + "`, expected `Tcp`, `QuickStart` or `Empty`");
}

void connect(std::vector<std::unique_ptr<Seat>>& seats, Log& log) {
    std::vector<TcpPlayer*> tcp;
    for (auto& s : seats)
        if (auto* t = dynamic_cast<TcpPlayer*>(s.get())) tcp.push_back(t);
    for (TcpPlayer* t : tcp) t->listen();
    std::vector<std::thread> threads;
    for (TcpPlayer* t : tcp) threads.emplace_back([t] { t->start(); });
    for (auto& th : threads) th.join();
    for (TcpPlayer* t : tcp)
        if (t->failure) t->crash(*t->failure, log);
}

void take_orders(Model& m, int32_t player_id, int seat, const Action& actions, Log& log) {
    for (const auto& kv : actions) {
        auto it = m.entities.find(kv.first);
        if (it == m.entities.end()) log.error("Got action for non-existent entity");
        else if (it->second.player_id != player_id) log.error("Got action for entity not belonging to the player");
        else m.entity_actions[kv.first] = action_from_view(seat, kv.second, m);
    }
}

void play(Model& m, std::vector<std::unique_ptr<Seat>>& seats, PyRandom& rng, ReplayWriter* replay, Log& log) {
    std::map<int, int32_t> by_seat;
    for (const auto& kv : m.players) by_seat[kv.second.index] = kv.first;
    while (!game_over(m)) {
        SightMaps seen;
        if (m.fog_of_war)
            for (const auto& kv : m.players) seen[kv.first] = sight_map(m, kv.first);
        for (size_t seat = 0; seat < seats.size(); seat++) {
            Seat& s = *seats[seat];
            if (s.crashed || s.is_empty()) continue;
            int32_t pid = by_seat.at(static_cast<int>(seat));
            auto it = seen.find(pid);
            Action actions;
            try {
                actions = s.get_action(player_view(m, pid, it == seen.end() ? nullptr : &it->second));
            } catch (const IoError& e) {
                s.crash(e.what(), log);
                continue;
            }
            take_orders(m, pid, static_cast<int>(seat), actions, log);
        }
        std::vector<Event> events = process_turn(m, rng, m.fog_of_war ? &seen : nullptr);
        if (replay) replay->tick(events, m);
    }
}

Json results(const Model& m, const std::vector<std::unique_ptr<Seat>>& seats, uint64_t seed) {
    std::map<int, int32_t> score;
    for (const auto& kv : m.players) score[kv.second.index] = kv.second.score;
    Json players = Json::array(), res = Json::array();
    for (size_t k = 0; k < seats.size(); k++) {
        Json p = Json::object();
        p.add("crashed", Json::boolean(seats[k]->crashed));
        p.add("comment", seats[k]->comment ? Json::string(*seats[k]->comment) : Json::null());
        players.push(p);
        res.push(Json::integer(score.at(static_cast<int>(k))));
    }
    Json out = Json::object();
    out.add("players", players).add("results", res).add("seed", Json::unsigned_integer(seed));
    return out;
}

}  // namespace

int run(const Json& config, const std::string& save_results, const std::string& save_replay, Log& log) {
    uint64_t seed;
    Model model;
    std::vector<std::unique_ptr<Seat>> seats;
    std::unique_ptr<PyRandom> rng;
    try {
        const Json* s = config.get("seed");
        if (!s || s->is_null()) {
            std::random_device rd;
            seed = (static_cast<uint64_t>(rd()) << 32) | rd();
        } else if ((s->kind == Json::Int && s->i >= 0) || s->kind == Json::UInt) {
            seed = s->as_u64();
        } else {
            throw ConfigError("the seed must be an unsigned 64-bit number");
        }
        const Json* specs = config.get("players");
        if (!specs || specs->kind != Json::Array || specs->arr.empty() || specs->arr.size() > 4)
            throw ConfigError("`players` must list 1 to 4 seats");
        rng = std::make_unique<PyRandom>(seed);
        model = load_game(config, static_cast<int>(specs->arr.size()), *rng);
        for (const Json& spec : specs->arr) seats.push_back(make_seat(spec, log));
    } catch (const std::exception& e) {
        log.error(std::string("panicked at '") + e.what() + "'");
        return PANIC;
    }
    try {
        connect(seats, log);
    } catch (const std::exception& e) {
        log.error(std::string("panicked at 'Could not listen: ") + e.what() + "'");
        return PANIC;
    }
    bool over_at_start = game_over(model) && model.current_tick < model.max_tick_count;
    std::unique_ptr<ReplayWriter> replay;
    int code = 0;
    try {
        if (!save_replay.empty()) {
            replay = std::make_unique<ReplayWriter>(save_replay);
            replay->start(model);
        }
        if (!game_over(model)) play(model, seats, *rng, replay.get(), log);
    } catch (const std::exception& e) {
        log.error(std::string("panicked at '") + e.what() + "'");
        code = PANIC;
    }
    for (auto& s : seats)
        if (!s->crashed) s->finish();
    if (replay) replay->close();
    if (code) return code;
    if (!save_results.empty() && !over_at_start) {
        std::ofstream f(save_results, std::ios::binary);
        f << dump_json(results(model, seats, seed), 2);
    }
    return 0;
}

}  // namespace cc
