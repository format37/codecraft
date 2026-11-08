#include "engine.hpp"

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <memory>
#include <random>
#include <sstream>
#include <stdexcept>
#include <thread>

#include "history.hpp"
#include "jsonout.hpp"
#include "live.hpp"
#include "mapgen.hpp"
#include "net.hpp"
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

std::unique_ptr<Seat> make_seat(const Json& spec, const std::string& default_host, Log& log) {
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
            // null host: the runner's own address (127.0.0.1, or --bind); null token: any token is taken (as the
            // official runner)
            const Json* host = value->get("host");
            const Json* token = value->get("token");
            std::optional<std::string> tok;
            if (token && !token->is_null()) tok = token->as_string();
            return std::make_unique<TcpPlayer>(host && !host->is_null() ? host->as_string() : default_host,
                                               static_cast<int>(value->at("port").as_int()),
                                               opt_seconds(value->get("accept_timeout")),
                                               opt_seconds(value->get("timeout")), tok, log);
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

// A game set up from its config: the seed, the generator, the map or state, the seats (not connected yet).
struct Game {
    uint64_t seed = 0;
    std::unique_ptr<PyRandom> rng;
    Model model;
    std::vector<std::unique_ptr<Seat>> seats;
};

Game setup(const Json& config, Log& log, const std::string& default_host = "127.0.0.1") {
    Game g;
    const Json* s = config.get("seed");
    if (!s || s->is_null()) {
        std::random_device rd;
        g.seed = (static_cast<uint64_t>(rd()) << 32) | rd();
    } else if ((s->kind == Json::Int && s->i >= 0) || s->kind == Json::UInt) {
        g.seed = s->as_u64();
    } else {
        throw ConfigError("the seed must be an unsigned 64-bit number");
    }
    const Json* specs = config.get("players");
    if (!specs || specs->kind != Json::Array || specs->arr.empty() || specs->arr.size() > 4)
        throw ConfigError("`players` must list 1 to 4 seats");
    g.rng = std::make_unique<PyRandom>(g.seed);
    g.model = load_game(config, static_cast<int>(specs->arr.size()), *g.rng);
    for (const Json& spec : specs->arr) g.seats.push_back(make_seat(spec, default_host, log));
    return g;
}

}  // namespace

int run(const Json& config, const std::string& save_results, const std::string& save_replay, Log& log) {
    Game g;
    try {
        g = setup(config, log);
    } catch (const std::exception& e) {
        log.error(std::string("panicked at '") + e.what() + "'");
        return PANIC;
    }
    uint64_t seed = g.seed;
    Model& model = g.model;
    std::vector<std::unique_ptr<Seat>>& seats = g.seats;
    PyRandom* rng = g.rng.get();
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

// GUI mode ------------------------------------------------------------------------------------------------------

namespace {

std::string seat_label(const LiveOptions& o, size_t k) {
    return k < o.names.size() ? o.names[k] : "Player " + std::to_string(k + 1);
}

std::string seat_json(size_t k, const Seat& s, const char* status) {
    Out o;
    o.raw('{').key("s").num(static_cast<int64_t>(k)).key("status").str(s.crashed ? "crashed" : status);
    o.key("comment");
    if (s.comment) o.str(*s.comment);
    else o.raw("null");
    o.raw('}');
    return o.s;
}

struct SeatInfo {
    std::string label, kind, status;
    int port = 0;
};

// The first event of a game: the map, the seats, the first state (the replay.json format, see history.hpp), and
// the server's limits for the TCP seats ({server: cut off past them, tick, game: seconds}; null for a replay).
std::string init_json(const Model& m, const std::vector<SeatInfo>& seats, const std::string& seed, bool replay,
                      const std::string& limits = "null") {
    Out o;
    o.raw('{').key("map_size").num(m.map_size).key("fog_of_war").boolean(m.fog_of_war);
    o.key("max_tick_count").num(m.max_tick_count).key("tick").num(m.current_tick);
    o.key("seed").str(seed).key("replay").boolean(replay).key("limits").raw(limits.c_str());
    o.key("properties");
    properties_json(o, m.props);
    std::vector<Player> players;
    for (const auto& kv : m.players) players.push_back(kv.second);
    std::sort(players.begin(), players.end(), [](const Player& a, const Player& b) { return a.index < b.index; });
    o.key("players").raw('[');
    for (const Player& p : players)
        o.sep().raw('[').num(p.id).raw(',').num(p.index).raw(',').num(p.score).raw(',').num(p.resource).raw(']');
    o.raw(']').key("seats").raw('[');
    for (const SeatInfo& s : seats) {
        o.sep().raw('{').key("label").str(s.label).key("kind").str(s.kind).key("status").str(s.status);
        if (s.port) o.key("port").num(s.port);
        o.raw('}');
    }
    o.raw(']').key("entities").raw('[');
    for (const auto& kv : m.entities) {
        o.sep();
        entity_json(o, kv.second);
    }
    o.raw(']').key("actions").raw('{');
    for (const auto& kv : m.entity_actions) {
        o.key(std::to_string(kv.first).c_str());
        order_json(o, kv.second);
    }
    o.raw("}}");
    return o.s;
}

std::vector<SeatInfo> seat_infos(const Game& g, const LiveOptions& opts) {
    std::vector<SeatInfo> out;
    for (size_t k = 0; k < g.seats.size(); k++) {
        SeatInfo i;
        i.label = seat_label(opts, k);
        if (auto* t = dynamic_cast<const TcpPlayer*>(g.seats[k].get())) {
            i.kind = "tcp";
            i.port = t->port();
            i.status = "waiting";
        } else {
            i.kind = g.seats[k]->is_empty() ? "empty" : "quickstart";
            i.status = "ready";
        }
        out.push_back(i);
    }
    return out;
}

std::string secs(double v) {
    char b[32];
    snprintf(b, sizeof b, "%g", v);
    return b;
}

// What the TCP seats asked for a tick took to answer: {t, ms: [per seat, null if not asked], used: [seconds in all]}.
std::string answers_json(int tick, const std::vector<std::optional<double>>& took, const std::vector<double>& used) {
    Out o;
    o.raw('{').key("t").num(tick).key("ms").raw('[');
    for (const auto& v : took) {
        o.sep();
        if (v) o.flt(*v * 1000);
        else o.raw("null");
    }
    o.raw(']').key("used").raw('[');
    for (size_t k = 0; k < took.size(); k++) {
        o.sep();
        if (took[k]) o.flt(used[k]);
        else o.raw("null");
    }
    o.raw("]}");
    return o.s;
}

// As the official GUI: the orders for a tick are asked for as soon as it is reached, then the tick stays on screen
// while the clients get their debug updates (the view of the tick on screen; at least one between ticks, about 14 a
// second while the game waits); the next tick comes when the viewer plays at the newest tick and its speed allows,
// or when it asks for a step. With the server's limits on, a TCP seat that takes longer than the server allows (to
// answer a tick, or in all) is cut off as the server would; either way the viewer gets each answer's time.
void live_loop(Game& g, std::vector<TcpPlayer*>& tcp, Live& live, GameRecord& rec, const LiveOptions& opts,
               Log& log) {
    Model& m = g.model;
    std::map<int, int32_t> by_seat;
    for (const auto& kv : m.players) by_seat[kv.second.index] = kv.first;
    using clock = std::chrono::steady_clock;
    auto last = clock::now();
    Model before = m;
    const int first_tick = m.current_tick;
    std::vector<double> used(g.seats.size(), 0.0);         // what each TCP seat has spent answering
    const std::string tick_cut = "no answer in " + secs(opts.tick_limit) + " s, the server's limit for a tick";
    const std::string game_cut = secs(opts.game_limit) + " s of answers in all, the server's limit for a game " +
                                 "(the server counts CPU time)";
    auto crash = [&](size_t k, const std::string& error) {
        g.seats[k]->crash(error, log);
        live.event("seat", seat_json(k, *g.seats[k], "crashed"));
    };
    auto debugging = [&] {
        for (size_t k = 0; k < tcp.size(); k++)
            if (tcp[k] && !g.seats[k]->crashed && tcp[k]->connected()) return true;
        return false;
    };
    auto debug_round = [&](int shown) {
        Model past;
        const Model* at = &m;
        if (shown != m.current_tick) {
            past = rec.history.at(shown);
            at = &past;
        }
        for (size_t k = 0; k < tcp.size(); k++) {
            if (!tcp[k] || g.seats[k]->crashed || !tcp[k]->connected()) continue;
            try {
                tcp[k]->debug_update(player_view(*at, by_seat.at(static_cast<int>(k))));
            } catch (const IoError& e) {
                crash(k, e.what());
            }
        }
    };
    while (!game_over(m) && !live.reset_requested()) {
        // the orders for the tick reached, at once: what a strategy draws answering it is shown with it
        SightMaps seen;
        if (m.fog_of_war)
            for (const auto& kv : m.players) seen[kv.first] = sight_map(m, kv.first);
        const bool limits = live.control().server_limits;
        std::vector<std::optional<double>> took(g.seats.size());
        for (size_t seat = 0; seat < g.seats.size(); seat++) {
            Seat& s = *g.seats[seat];
            if (s.crashed || s.is_empty()) continue;
            int32_t pid = by_seat.at(static_cast<int>(seat));
            auto it = seen.find(pid);
            TcpPlayer* t = tcp[seat];
            const double left = opts.game_limit - used[seat];
            if (t) t->set_limit(limits ? std::optional<double>(std::clamp(left, 0.0, opts.tick_limit)) : std::nullopt);
            Action actions;
            try {
                actions = s.get_action(player_view(m, pid, it == seen.end() ? nullptr : &it->second));
            } catch (const IoError& e) {
                std::string why = e.what();
                if (t) {
                    took[seat] = t->answer_seconds();
                    used[seat] += t->answer_seconds();
                    if (t->limit_hit()) why = left < opts.tick_limit ? game_cut : tick_cut;
                }
                crash(seat, why);
                continue;
            }
            if (t) {
                took[seat] = t->answer_seconds();
                used[seat] += t->answer_seconds();
            }
            take_orders(m, pid, static_cast<int>(seat), actions, log);
        }
        if (std::any_of(took.begin(), took.end(), [](const auto& v) { return v.has_value(); }))
            live.event("answers", answers_json(m.current_tick, took, used));
        // then the tick stays on screen as long as the viewer wants it
        bool updated = false;
        while (true) {
            if (live.reset_requested()) return;
            Control c = live.control();
            int newest = m.current_tick;
            bool debug = debugging();
            if (debug) {
                debug_round(c.follow ? newest : std::clamp(c.display, first_tick, newest));
                updated = true;
            }
            if (live.take_step()) break;
            double interval = c.speed > 0 ? 1.0 / c.speed : 0.0;
            double elapsed = std::chrono::duration<double>(clock::now() - last).count();
            bool at_newest = c.follow || c.display >= newest;
            if (c.playing && at_newest && elapsed >= interval && (updated || !debug)) break;
            double pause = debug ? 0.07 : 0.25;
            if (c.playing && at_newest) pause = std::min(pause, std::max(0.0, interval - elapsed));
            live.wait(pause);
        }
        last = clock::now();
        std::vector<Event> events = process_turn(m, *g.rng, m.fog_of_war ? &seen : nullptr);
        {
            std::lock_guard<std::mutex> lock(rec.replay_mutex);
            rec.replay->tick(events, m);
        }
        TickDiff d = diff(before, m);
        rec.history.push(m, d);
        before = m;
        live.event("tick", tick_json(d, events));
    }
}

int play_live(Game& g, Live& live, const LiveOptions& opts, Log& log) {
    Model& m = g.model;
    auto rec = std::make_shared<GameRecord>();
    rec->seed = g.seed;
    rec->history.start(m);
    rec->replay = std::make_unique<ReplayWriter>(opts.save_replay, true);
    rec->replay->start(m);
    Out limits;
    limits.raw('{').key("server").boolean(live.control().server_limits).key("tick").flt(opts.tick_limit);
    limits.key("game").flt(opts.game_limit).raw('}');
    live.new_game("init", init_json(m, seat_infos(g, opts), std::to_string(g.seed), false, limits.s), rec);
    std::vector<TcpPlayer*> tcp(g.seats.size(), nullptr);
    for (size_t k = 0; k < g.seats.size(); k++) {
        tcp[k] = dynamic_cast<TcpPlayer*>(g.seats[k].get());
        if (tcp[k]) tcp[k]->set_debug(&live, static_cast<int>(k), m.map_size);
    }
    // the viewer's Restart stops the waits on the seats; cleared before they go
    struct Interruptible {
        Live& live;
        ~Interruptible() { live.set_interrupt(nullptr); }
    } interruptible{live};
    live.set_interrupt([&tcp](bool force) {
        for (TcpPlayer* t : tcp)
            if (t) t->interrupt(force);
    });
    connect(g.seats, log);                         // throws when a port cannot be bound
    bool restart = live.reset_requested();
    if (!restart)
        for (size_t k = 0; k < g.seats.size(); k++)
            if (tcp[k]) live.event("seat", seat_json(k, *g.seats[k], "connected"));
    bool over_at_start = game_over(m) && m.current_tick < m.max_tick_count;
    int code = 0;
    std::string error;
    try {
        if (!restart && !game_over(m)) live_loop(g, tcp, live, *rec, opts, log);
    } catch (const std::exception& e) {
        error = e.what();
        log.error("panicked at '" + error + "'");
        code = PANIC;
    }
    for (auto& s : g.seats)
        if (!s->crashed) s->finish();              // a restart too: the clients end as after a game
    live.set_interrupt(nullptr);
    {
        std::lock_guard<std::mutex> lock(rec->replay_mutex);
        rec->replay->close();
    }
    if (live.reset_requested()) {
        log.info("Restart at tick " + std::to_string(m.current_tick));
        return code;
    }
    Json res = results(m, g.seats, g.seed);
    if (!code && !opts.save_results.empty() && !over_at_start) {
        std::ofstream f(opts.save_results, std::ios::binary);
        f << dump_json(res, 2);
    }
    Out o;
    o.raw('{').key("tick").num(m.current_tick).key("seed").str(std::to_string(g.seed));
    o.key("results").raw(dump_json(*res.get("results"), -1).c_str());
    o.key("players").raw(dump_json(*res.get("players"), -1).c_str());
    o.key("over_at_start").boolean(over_at_start).key("error");
    if (code) o.str(error);
    else o.raw("null");
    o.raw('}');
    live.event("over", o.s);
    std::string scores;
    for (const Json& v : res.get("results")->arr) scores += (scores.empty() ? "" : ", ") + std::to_string(v.i);
    log.info("Game over at tick " + std::to_string(m.current_tick) + ": " + scores);
    return code;
}

Json with_seed(Json config, uint64_t seed) {
    for (auto& kv : config.obj)
        if (kv.first == "seed") {
            kv.second = Json::unsigned_integer(seed);
            return config;
        }
    config.add("seed", Json::unsigned_integer(seed));
    return config;
}

std::string default_config() {
    return "{\"seed\":null,\"game\":{\"Create\":\"Round1\"},\"players\":[{\"Tcp\":{\"host\":null,\"port\":31001,"
           "\"accept_timeout\":null,\"timeout\":null,\"token\":null}},\"QuickStart\",\"QuickStart\",\"QuickStart\"]}";
}

}  // namespace

int run_live(const Json* config, const LiveOptions& opts, Log& log) {
    Live live(log, opts.speed, opts.server_limits);
    int port;
    try {
        port = live.listen(opts.bind, opts.port);
    } catch (const std::exception& e) {
        log.error(std::string("panicked at 'Could not start the viewer: ") + e.what() + "'");
        return PANIC;
    }
    live.start();
    // always 127.0.0.1, whatever --bind: in a container, `docker -p` brings the host's 127.0.0.1 here
    std::string url = "http://127.0.0.1:" + std::to_string(port) + "/";
    log.info("Viewer: " + url);
    if (opts.open_browser) net::open_browser(url);
    std::string offer = config ? dump_json(*config, -1) : default_config(), error;
    std::optional<Json> next;              // a config to play without the config screen
    if (config) next = *config;
    bool from_file = config != nullptr;
    Json base;                             // the config chosen (a restart plays it again)
    while (true) {
        Json cfg;
        if (next) {
            cfg = *next;
            next.reset();
        } else {
            Out o;
            o.raw('{').key("config").raw(offer.c_str()).key("error");
            if (error.empty()) o.raw("null");
            else o.str(error);
            o.raw('}');
            live.new_game("config", o.s);
            cfg = base = live.wait_config();
            offer = dump_json(cfg, -1);
        }
        if (from_file) base = cfg;
        Game g;
        try {
            g = setup(cfg, log, opts.bind);
        } catch (const std::exception& e) {
            log.error(std::string("panicked at '") + e.what() + "'");
            if (from_file) return PANIC;        // a bad --config file ends the runner, as in batch mode
            error = e.what();
            continue;
        }
        from_file = false;
        error.clear();
        int code;
        try {
            code = play_live(g, live, opts, log);
        } catch (const std::exception& e) {      // a port in use
            log.error(std::string("panicked at 'Could not listen: ") + e.what() + "'");
            if (opts.exit_when_over) return PANIC;
            error = std::string("Could not listen: ") + e.what();
            continue;
        }
        bool same_seed = false;
        bool restart = live.take_reset(same_seed);
        if (!restart) {
            if (opts.exit_when_over) return code;
            restart = live.wait_next() == Live::Next::Restart && live.take_reset(same_seed);
        }
        if (restart) next = same_seed ? with_seed(base, g.seed) : base;
    }
}

int run_replay(const std::string& path, const LiveOptions& opts, Log& log) {
    std::unique_ptr<ReplayReader> reader;
    try {
        std::ifstream f(path, std::ios::binary);
        if (!f) throw std::runtime_error("cannot read it");
        std::stringstream ss;
        ss << f.rdbuf();
        reader = std::make_unique<ReplayReader>(ss.str());
    } catch (const std::exception& e) {
        log.error("panicked at 'Could not load the replay " + path + ": " + e.what() + "'");
        return PANIC;
    }
    Live live(log, opts.speed);
    int port;
    try {
        port = live.listen(opts.bind, opts.port);
    } catch (const std::exception& e) {
        log.error(std::string("panicked at 'Could not start the viewer: ") + e.what() + "'");
        return PANIC;
    }
    live.start();
    // always 127.0.0.1, whatever --bind: in a container, `docker -p` brings the host's 127.0.0.1 here
    std::string url = "http://127.0.0.1:" + std::to_string(port) + "/";
    log.info("Viewer: " + url);
    if (opts.open_browser) net::open_browser(url);
    Model m = reader->initial();
    std::vector<SeatInfo> seats;
    for (size_t k = 0; k < m.players.size(); k++) seats.push_back(SeatInfo{seat_label(opts, k), "replay", "replay", 0});
    auto rec = std::make_shared<GameRecord>();
    rec->source = path;
    rec->history.start(m);
    live.new_game("init", init_json(m, seats, "", true), rec);
    Model before = m;
    std::vector<Event> events;
    try {
        while (reader->next(m, events)) {
            TickDiff d = diff(before, m);
            rec->history.push(m, d);
            before = m;
            live.event("tick", tick_json(d, events));
        }
    } catch (const std::exception& e) {
        log.warn(std::string("The replay ends early: ") + e.what());
    }
    std::vector<int32_t> scores(m.players.size(), 0);
    for (const auto& kv : m.players)
        if (kv.second.index >= 0 && static_cast<size_t>(kv.second.index) < scores.size())
            scores[static_cast<size_t>(kv.second.index)] = kv.second.score;
    Out o;
    o.raw('{').key("tick").num(m.current_tick).key("seed").str("").key("replay").boolean(true);
    o.key("results").raw('[');
    for (int32_t v : scores) o.sep().num(v);
    o.raw(']').key("players").raw('[');
    for (size_t k = 0; k < scores.size(); k++) o.sep().raw("{\"crashed\":false,\"comment\":null}");
    o.raw(']').key("over_at_start").boolean(false).key("error").raw("null").raw('}');
    live.event("over", o.s);
    log.info("Replay " + path + ": ticks " + std::to_string(reader->initial().current_tick) + " to " +
             std::to_string(m.current_tick));
    if (opts.exit_when_over) return 0;
    while (true) live.wait_new_game();
}

}  // namespace cc

