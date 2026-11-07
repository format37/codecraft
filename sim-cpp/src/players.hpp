// The seats, as sim/codecraft_sim/players.py: a TCP client (the official protocol: listen, accept within
// accept_timeout, the token, each read within `timeout` as the official SO_RCVTIMEO, debug messages read and
// ignored or refused, the official error texts), an Empty seat, the built-in QuickStart; and the log.
#pragma once
#include <memory>
#include <mutex>
#include <optional>
#include <string>

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
    TcpPlayer(std::string host, int port, std::optional<double> accept_timeout, std::optional<double> timeout,
              std::string token, Log& log);
    ~TcpPlayer() override;
    void listen();                       // throws std::runtime_error when the address cannot be bound
    void start() override;               // accepts and checks the token; a failure goes to `failure`
    Action get_action(const PlayerView& view) override;
    void finish() override;
    void crash(const std::string& error, Log& log) override;
    std::optional<std::string> failure;

private:
    std::string host_, token_;
    int port_;
    std::optional<double> accept_timeout_, timeout_;
    Log& log_;
    int listener_ = -1, sock_ = -1;
    std::unique_ptr<SocketReader> reader_;
    void send_all(const std::string& data);
    void drop();
};

}  // namespace cc
