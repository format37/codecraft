// The game loop of `aicup2020 --batch-mode`, as sim/codecraft_sim/engine.py; and the GUI mode (no --batch-mode),
// where a viewer in the browser (live.hpp) stands in for the official runner's window.
#pragma once
#include <string>
#include <vector>

#include "json.hpp"
#include "players.hpp"

namespace cc {

constexpr int PANIC = 101;
int run(const Json& config, const std::string& save_results, const std::string& save_replay, Log& log);

struct LiveOptions {
    int port = 31000;                  // the viewer's (127.0.0.1); 0 = any free port
    bool open_browser = true;
    double speed = 10;                 // ticks per second at the start; 0 = as fast as it goes
    bool exit_when_over = false;       // end after the game instead of waiting for the next one (tests)
    std::string save_results, save_replay;
    std::vector<std::string> names;    // --player-names
    // The server's limits for a strategy (its config.yaml: tick_timeout_sec, cpu_time_sec), for the TCP seats:
    // as on the server, a seat is cut off past them; or only warned about (the viewer switches between the two).
    bool server_limits = true;
    double tick_limit = 1.0;           // seconds to answer a tick
    double game_limit = 40.0;          // seconds of answers in all (the server counts CPU time)
};

// The GUI mode; without a config (null) the viewer's config screen comes first.
int run_live(const Json* config, const LiveOptions& options, Log& log);
// `--replay FILE`: a replay file in the viewer.
int run_replay(const std::string& path, const LiveOptions& options, Log& log);

}  // namespace cc
