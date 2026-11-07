// The viewer of the GUI mode (the official runner without --batch-mode opens a window; this engine serves a page):
// a small HTTP server on 127.0.0.1 that serves the viewer (viewer/viewer.html, built in), streams the game to it as
// server-sent events, and takes back what the viewer sends: its controls (pause, speed, the tick it shows, steps),
// its debug state (window, mouse, keys, camera), a config to start a game with, Ctrl-S and Ctrl-E.
#pragma once
#include <atomic>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

#include "history.hpp"
#include "json.hpp"
#include "players.hpp"
#include "replay.hpp"

namespace cc {

struct Control {
    bool playing = true;
    double speed = 10;          // ticks per second; 0 = as fast as the engine goes
    bool follow = true;         // the viewer shows the newest tick
    int display = 0;            // the tick it shows otherwise
};

// What the viewer may save of the game being played: its states (Ctrl-E) and its replay so far (Ctrl-S).
struct GameRecord {
    History history;
    std::mutex replay_mutex;
    std::unique_ptr<ReplayWriter> replay;     // keeps its bytes; none when watching a replay file
    std::string source;                       // the replay file watched
    uint64_t seed = 0;
};

class Live : public DebugHost {
public:
    Live(Log& log, double speed);
    ~Live() override;
    int listen(const std::string& host, int port);      // throws std::runtime_error; returns the port bound
    void start();                                        // serves from its own threads

    // The engine's side. A game's events start with "init" (or "config" for the config screen).
    void new_game(const std::string& first_event, const std::string& json,
                  std::shared_ptr<GameRecord> record = nullptr);
    void event(const char* name, const std::string& json);
    Control control();
    bool take_step();                // the viewer asked for one more tick (paused at the newest)
    void wait(double seconds);       // until then, or something comes from the viewer
    Json wait_config();              // the config the viewer starts a game with (after new_game("config", ...))
    void wait_new_game();            // until the viewer asks for another game

    void debug_ops(int seat, int layer, std::vector<DebugOp> ops) override;
    DebugInput debug_input() override;

private:
    Log& log_;
    int listener_ = -1, port_ = 0;
    std::thread acceptor_;
    std::atomic<bool> stop_{false};
    std::atomic<int> active_{0};               // connections being served

    std::mutex mutex_;
    std::condition_variable events_cv_, input_cv_;
    int generation_ = 0;
    std::vector<std::string> events_;          // the game's events so far, as SSE frames
    std::shared_ptr<GameRecord> record_;
    Control control_;
    int steps_ = 0;
    DebugInput input_;
    std::optional<Json> config_;
    bool new_game_ = false;
    long inputs_ = 0;                          // counts the viewer's controls, for wait()

    void accept_loop();
    void serve(int fd);
    void stream(int fd);
    std::string post(const std::string& path, const Json& body);
    std::string save_state(int tick);
    std::string save_replay();
};

}  // namespace cc
