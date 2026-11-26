// The game loop of `aicup2020 --batch-mode`, as sim/codecraft_sim/engine.py; and the GUI mode (no --batch-mode),
// where a viewer in the browser (live.hpp) stands in for the official runner's window.
#pragma once
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include "json.hpp"
#include "players.hpp"
#include "rng.hpp"

namespace cc {

class ReplayWriter;

// Embedding: a program that plays games in its own process (a training arena, say) uses the batch mode's own
// setup, loop and results below. Two additions change nothing when unused: seat kinds of the host's own (a
// SeatMaker, asked for any kind the engine does not know) and a TickObserver, told about every tick and able to
// end the game after it.
class TickObserver {
public:
    virtual ~TickObserver() = default;
    virtual void start(const Model&) {}              // the state before the first tick is played
    // after each tick, with its events; false ends the game in this state (the results are this state's)
    virtual bool after_tick(const Model& m, const std::vector<Event>& events) = 0;
};

// The seat for a kind the engine does not know ({"Kind": value}, or "Kind" with a null value); null: unknown too.
using SeatMaker = std::function<std::unique_ptr<Seat>(const std::string& kind, const Json* value)>;

// A game set up from its config: the seed, the generator, the map or state, the seats (not connected yet).
struct Game {
    uint64_t seed = 0;
    std::unique_ptr<PyRandom> rng;
    Model model;
    std::vector<std::unique_ptr<Seat>> seats;
};

// What the TCP seats' answers took, tick by tick (--save-timing; the server keeps it for the game's page): from the
// view sent to the orders back, debug messages included, as the runner's viewer shows it live.
struct AnswerTimes {
    int first_tick = 0;
    std::vector<std::vector<std::optional<double>>> seconds;    // [seat][tick - first_tick]; none: not asked
    void record(size_t seat, int tick, double s);
    // {"first_tick": T, "limit_ms": [the config's timeout per seat, or null], "ms": [per seat: [per tick from T,
    // null when not asked], or null for a seat that is not TCP]}
    std::string json(const std::vector<std::unique_ptr<Seat>>& seats) const;
};

// throws on a bad config (std::exception); TCP seats still have to be connected before play()
Game setup(const Json& config, Log& log, const std::string& default_host = "127.0.0.1",
           const SeatMaker* extra = nullptr);
void play(Model& m, std::vector<std::unique_ptr<Seat>>& seats, PyRandom& rng, ReplayWriter* replay, Log& log,
          bool rules_2020, TickObserver* observer = nullptr, AnswerTimes* answers = nullptr);
Json results(const Model& m, const std::vector<std::unique_ptr<Seat>>& seats, uint64_t seed);

constexpr int PANIC = 101;
// `rules_2020`: the 2020 rules exactly; by default the arena's (rules.hpp, FogMemory).
int run(const Json& config, const std::string& save_results, const std::string& save_replay, Log& log,
        bool rules_2020 = false, const std::string& save_timing = "");

struct LiveOptions {
    std::string bind = "127.0.0.1";    // --bind: where the viewer and the TCP seats without a host listen
    int port = 31000;                  // the viewer's port; 0 = any free port
    bool open_browser = true;
    double speed = 10;                 // ticks per second at the start; 0 = as fast as it goes
    bool exit_when_over = false;       // end after the game instead of waiting for the next one (tests)
    std::string save_results, save_replay;
    std::vector<std::string> names;    // --player-names
    // The server's limits for a strategy (its config.yaml: tick_timeout_sec, cpu_time_sec), for the TCP seats:
    // a seat past them is only warned about (the default: a breakpoint may take its time), or cut off as on the
    // server (the viewer switches between the two).
    bool server_limits = false;
    // A TCP seat's strategy went away (its connection lost in a game), or connects again after a game: the game
    // starts again by itself, as with the viewer's Restart (the viewer switches it; off with --viewer-exit).
    bool auto_restart = true;
    double tick_limit = 1.0;           // seconds to answer a tick
    double game_limit = 40.0;          // seconds of answers in all (the server counts CPU time)
    bool rules_2020 = false;           // --rules 2020: the 2020 rules exactly (default: the arena's, FogMemory)
    // --seed: the seed the config screen offers (main puts it into a --config file's config itself)
    std::optional<uint64_t> seed;
};

// The config with its seed set to `seed` (added when it has none).
Json with_seed(Json config, uint64_t seed);

// The GUI mode; without a config (null) the viewer's config screen comes first.
int run_live(const Json* config, const LiveOptions& options, Log& log);
// `--replay FILE`: a replay file in the viewer.
int run_replay(const std::string& path, const LiveOptions& options, Log& log);

}  // namespace cc
