// The seats, as sim/codecraft_sim/players.py: a TCP client (the official protocol: listen, accept within
// accept_timeout, the token, each read within `timeout` as the official SO_RCVTIMEO, the official error texts), an
// Empty seat, the built-in QuickStart; and the log. In batch mode a client's debug messages are read and ignored or
// refused; with the viewer (live.hpp) a TCP client has the official GUI's debug interface: debug_available, its
// drawings, DebugUpdate between ticks and the DebugState it asks for.
#pragma once
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "debug.hpp"
#include "trans.hpp"

namespace cc {

class Log {
public:
    explicit Log(const std::string& level = "info");
    void error(const std::string& msg) { write(1, "ERROR", msg); }
    void warn(const std::string& msg) { write(2, "WARN", msg); }
    void info(const std::string& msg) { write(3, "INFO", msg); }

private:
    int level_;
    std::mutex mutex_;
    void write(int level, const char* name, const std::string& msg);
};

// A change of a seat's drawings: Add (its data, in the global frame) or Clear.
struct DebugOp {
    bool clear = false;
    DebugData data;
};

// The viewer's side of the debug interface: where a seat's drawings go, where its DebugState comes from.
class DebugHost {
public:
    virtual ~DebugHost() = default;
    // `layer` >= 0: the drawings of that tick (sent while answering its view); -1: the seat's persistent layer
    // (sent in debug_update), shown with every tick
    virtual void debug_ops(int seat, int layer, std::vector<DebugOp> ops) = 0;
    virtual DebugInput debug_input() = 0;
};

class Seat {
public:
    virtual ~Seat() = default;
    virtual bool is_empty() const { return false; }
    virtual void start() {}
    virtual Action get_action(const PlayerView& view) = 0;     // throws IoError when the seat fails
    virtual void finish() {}
    virtual void crash(const std::string& error, Log& log);
    bool crashed = false;
    std::optional<std::string> comment;
};

class EmptyPlayer : public Seat {
public:
    bool is_empty() const override { return true; }
    Action get_action(const PlayerView&) override { return {}; }
};

class QuickStartPlayer : public Seat {
public:
    Action get_action(const PlayerView& view) override;
};

class TcpPlayer : public Seat {
public:
    // a null token accepts any (as the official runner)
    TcpPlayer(std::string host, int port, std::optional<double> accept_timeout, std::optional<double> timeout,
              std::optional<std::string> token, Log& log);
    ~TcpPlayer() override;
    void listen();                       // throws std::runtime_error when the address cannot be bound
    void start() override;               // accepts and checks the token; a failure goes to `failure`
    Action get_action(const PlayerView& view) override;
    void finish() override;
    void crash(const std::string& error, Log& log) override;
    std::optional<std::string> failure;
    int port() const { return port_; }
    bool connected() const { return sock_ >= 0; }

    // The viewer's debug interface (GUI mode): `seat` turns the client's points into the global frame.
    void set_debug(DebugHost* host, int seat, int map_size);
    bool debug() const { return debug_ != nullptr; }
    void debug_update(const PlayerView& view);     // DebugUpdate, until DebugUpdateDone; throws IoError

private:
    std::string host_;
    std::optional<std::string> token_;
    int port_;
    std::optional<double> accept_timeout_, timeout_;
    Log& log_;
    int listener_ = -1, sock_ = -1;
    std::unique_ptr<SocketReader> reader_;
    void send_all(const std::string& data);
    void drop();

    DebugHost* debug_ = nullptr;
    int seat_ = 0, map_size_ = 80;
    bool autoflush_ = true;
    std::vector<std::pair<int, DebugOp>> pending_;     // autoflush off: commands waiting for Flush, with their layer
    std::vector<DebugOp> unsent_;                       // done, not yet handed to the viewer
    int unsent_layer_ = 0;
    void debug_command(const DebugCommand& c, int layer);
    void apply(int layer, DebugOp op);
    void hand_over();
    void send_debug_state();
    int32_t next_tag();                                 // the next message; drawings go out while the client is quiet
};

}  // namespace cc
