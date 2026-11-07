// codecraft-sim-cpp: our CodeCraft 2020 engine in C++, with the official runner's command line (batch mode only):
//   codecraft-sim-cpp --config /game/config.json --batch-mode --save-results /game/results.json
//       --save-replay /game/replay.dat --log-level info --player-names a b c d
// It plays exactly the games of the Python engine (sim/codecraft_sim), seed for seed.
#include <cstdio>
#include <cstring>
#include <fstream>
#include <set>
#include <sstream>
#include <string>
#include <vector>

#include "engine.hpp"

#ifndef SIM_VERSION
#define SIM_VERSION "0.1.0"
#endif

int main(int argc, char** argv) {
    std::string config_path, save_results, save_replay, log_level = "info";
    bool replay_mode = false, client_gen = false;
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
            printf("codecraft-sim-cpp %s\n", SIM_VERSION);
            return 0;
        } else if (a == "-h" || a == "--help") {
            printf("codecraft-sim-cpp %s: the CodeCraft 2020 game engine (our own), batch mode\n"
                   "  --config FILE --batch-mode --save-results FILE --save-replay FILE --log-level LEVEL\n"
                   "  --player-names NAME...\n", SIM_VERSION);
            return 0;
        } else if (a == "--config") {
            config_path = value();
        } else if (a == "--batch-mode" || a == "--client-gen-preserve-files") {
        } else if (a == "--save-results") {
            save_results = value();
        } else if (a == "--save-replay") {
            save_replay = value();
        } else if (a == "--log-level") {
            log_level = value();
        } else if (a == "--player-names" || a == "--client-gen-language") {
            while (k + 1 < argc && strncmp(argv[k + 1], "--", 2) != 0) k++;
        } else if (a == "--replay") {
            value();
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
    if (replay_mode || client_gen) {
        log.error("panicked at 'this engine has no GUI and no client generator'");
        return cc::PANIC;
    }
    if (config_path.empty()) {
        log.error("panicked at 'Batch mode can only be used with --config option'");
        return cc::PANIC;
    }
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
    return cc::run(config, save_results, save_replay, log);
}
