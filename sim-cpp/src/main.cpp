// codecraft-sim-cpp: our CodeCraft 2020 engine in C++, with the official runner's command line:
//   codecraft-sim-cpp --config /game/config.json --batch-mode --save-results /game/results.json
//       --save-replay /game/replay.dat --log-level info --player-names a b c d
// It plays exactly the games of the Python engine (sim/codecraft_sim), seed for seed. Without --batch-mode it is the
// local runner: the game is shown live in the browser (http://127.0.0.1:31000/) with the official GUI's debug
// interface for the TCP clients; without --config the viewer asks for one.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <set>
#include <sstream>
#include <string>
#include <vector>

#include "engine.hpp"

// build/version.cpp, made by the Makefile: the version of the engine's source, as the server tags its image with it
extern const char ENGINE_VERSION[];

int main(int argc, char** argv) {
    std::string config_path, save_results, save_replay, replay_path, log_level = "info";
    bool replay_mode = false, client_gen = false, batch = false;
    cc::LiveOptions live;
    // The image ghcr.io/format37/codecraft-runner sets CODECRAFT_RUNNER_CONTAINER=1 (sim-cpp/Dockerfile): the viewer
    // and the seats listen on every address, to be reached through `docker -p`, and no browser is opened. Flags on
    // the command line still win; batch mode (the server) uses neither.
    if (const char* c = getenv("CODECRAFT_RUNNER_CONTAINER"); c && *c && strcmp(c, "0") != 0) {
        live.bind = "0.0.0.0";
        live.open_browser = false;
    }
    const std::set<std::string> ignored_with_value = {"--repeat", "--lang", "--client-gen-options",
                                                     "--client-gen-host-from-docker", "--client-gen-test-mode"};
    for (int k = 1; k < argc; k++) {
        std::string a = argv[k];
        auto value = [&]() -> std::string {
            if (k + 1 >= argc) {
                fprintf(stderr, "error: %s needs a value\n", a.c_str());
                exit(2);
            }
            return argv[++k];
        };
        if (a == "-V" || a == "--version") {
            printf("codecraft-sim-cpp %s\n", ENGINE_VERSION);
            return 0;
        } else if (a == "-h" || a == "--help") {
            printf("codecraft-sim-cpp %s: the CodeCraft 2020 game engine (our own)\n"
                   "  --config FILE           the game: seed, preset or state, seats (the official runner's config)\n"
                   "  --batch-mode            no viewer: play and exit (the server's way)\n"
                   "  --save-results FILE --save-replay FILE --log-level LEVEL --player-names NAME...\n"
                   "Without --batch-mode the game is shown live in the browser, with the debug interface of the\n"
                   "official GUI for TCP clients; without --config the viewer asks for one:\n"
                   "  --viewer-port PORT      the viewer's port (default 31000; 0 = any free one)\n"
                   "  --bind ADDR             the address the viewer and the TCP seats without a host listen on\n"
                   "                          (default 127.0.0.1; the container image, which sets\n"
                   "                          CODECRAFT_RUNNER_CONTAINER=1, listens on 0.0.0.0 and opens no browser)\n"
                   "  --viewer-speed TPS      ticks per second at the start (default 10; 0 = as fast as it goes)\n"
                   "  --no-browser            do not open the browser\n"
                   "  --viewer-exit           exit when the game is over (default: wait for the next game)\n"
                   "  --replay FILE           watch a replay file (--save-replay, Ctrl-S) in the viewer\n"
                   "  --limits server|warn    the server's limits for TCP seats: cut a strategy off past them, as\n"
                   "                          the server does (default), or only warn (the viewer switches too)\n"
                   "  --tick-limit SEC        the server's time to answer a tick (default 1)\n"
                   "  --game-limit SEC        the server's time to answer in all, a game (default 40; the server\n"
                   "                          counts CPU time, the runner the time to answer)\n",
                   ENGINE_VERSION);
            return 0;
        } else if (a == "--config") {
            config_path = value();
        } else if (a == "--batch-mode") {
            batch = true;
        } else if (a == "--viewer-port") {
            live.port = atoi(value().c_str());
        } else if (a == "--bind") {
            live.bind = value();
        } else if (a == "--viewer-speed") {
            live.speed = atof(value().c_str());
        } else if (a == "--no-browser") {
            live.open_browser = false;
        } else if (a == "--viewer-exit") {
            live.exit_when_over = true;
        } else if (a == "--limits") {
            std::string v = value();
            if (v != "server" && v != "warn") {
                fprintf(stderr, "error: --limits is server or warn, not %s\n", v.c_str());
                return 2;
            }
            live.server_limits = v == "server";
        } else if (a == "--tick-limit" || a == "--game-limit") {
            double v = atof(value().c_str());
            if (!(v > 0)) {
                fprintf(stderr, "error: %s takes seconds above 0\n", a.c_str());
                return 2;
            }
            (a == "--tick-limit" ? live.tick_limit : live.game_limit) = v;
        } else if (a == "--client-gen-preserve-files") {
        } else if (a == "--save-results") {
            save_results = value();
        } else if (a == "--save-replay") {
            save_replay = value();
        } else if (a == "--log-level") {
            log_level = value();
        } else if (a == "--player-names") {
            while (k + 1 < argc && strncmp(argv[k + 1], "--", 2) != 0) live.names.push_back(argv[++k]);
        } else if (a == "--client-gen-language") {
            while (k + 1 < argc && strncmp(argv[k + 1], "--", 2) != 0) k++;
        } else if (a == "--replay") {
            replay_path = value();
            replay_mode = true;
        } else if (a == "--client-gen") {
            value();
            client_gen = true;
        } else if (ignored_with_value.count(a)) {
            value();
        } else {
            fprintf(stderr, "error: unknown argument %s\n", a.c_str());
            return 2;
        }
    }
    cc::Log log(log_level);
    if (client_gen || (replay_mode && batch)) {
        log.error("panicked at 'this engine has no client generator, and plays replays only with the viewer'");
        return cc::PANIC;
    }
    if (config_path.empty() && batch) {
        log.error("panicked at 'Batch mode can only be used with --config option'");
        return cc::PANIC;
    }
    live.save_results = save_results;
    live.save_replay = save_replay;
    if (replay_mode) return cc::run_replay(replay_path, live, log);
    if (config_path.empty()) return cc::run_live(nullptr, live, log);
    cc::Json config;
    try {
        std::ifstream f(config_path);
        if (!f) throw cc::JsonError("cannot read " + config_path);
        std::stringstream ss;
        ss << f.rdbuf();
        config = cc::parse_json(ss.str());
        if (config.kind != cc::Json::Object) throw cc::JsonError("not a JSON object");
    } catch (const std::exception& e) {
        log.error(std::string("panicked at 'Could not load config: ") + e.what() + "'");
        return cc::PANIC;
    }
    if (!batch) return cc::run_live(&config, live, log);
    return cc::run(config, save_results, save_replay, log);
}
