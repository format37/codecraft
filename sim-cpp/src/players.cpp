#include "players.hpp"

#include <arpa/inet.h>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <stdexcept>
#include <sys/socket.h>
#include <unistd.h>

#include "quickstart.hpp"

namespace cc {

std::string os_error(int err);      // trans.cpp

static const char* const NO_CONNECTION = "IO error: IO error: connection reset";
static const char* const TOKEN_MISMATCH = "IO error: IO error: Token mismatch";

Log::Log(const std::string& level) {
    if (level == "off") level_ = 0;
    else if (level == "error") level_ = 1;
    else if (level == "warn") level_ = 2;
    else if (level == "debug") level_ = 4;
    else if (level == "trace") level_ = 5;
    else level_ = 3;
}

void Log::write(int level, const char* name, const std::string& msg) {
    if (level > level_) return;
    std::lock_guard<std::mutex> lock(mutex_);
    fprintf(stderr, "%s - %s\n", name, msg.c_str());
    fflush(stderr);
}

void Seat::crash(const std::string& error, Log& log) {
    crashed = true;
    comment = "Player crashed: " + error;
    log.warn("Player error: " + error);
}

Action QuickStartPlayer::get_action(const PlayerView& view) { return quickstart_action(view); }

TcpPlayer::TcpPlayer(std::string host, int port, std::optional<double> accept_timeout, std::optional<double> timeout,
                     std::string token, Log& log)
    : host_(std::move(host)), token_(std::move(token)), port_(port), accept_timeout_(accept_timeout),
      timeout_(timeout), log_(log) {}

TcpPlayer::~TcpPlayer() {
    if (listener_ >= 0) ::close(listener_);
    if (sock_ >= 0) ::close(sock_);
}

static void set_timeout(int fd, std::optional<double> seconds) {
    timeval tv{0, 0};
    if (seconds) {
        double s = *seconds;
        tv.tv_sec = static_cast<time_t>(s);
        tv.tv_usec = static_cast<suseconds_t>((s - static_cast<double>(tv.tv_sec)) * 1e6);
        if (tv.tv_sec == 0 && tv.tv_usec == 0) tv.tv_usec = 1;
    }
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
}

void TcpPlayer::listen() {
    addrinfo hints{}, *res = nullptr;
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_flags = AI_PASSIVE;
    std::string port = std::to_string(port_);
    if (getaddrinfo(host_.c_str(), port.c_str(), &hints, &res) != 0 || !res)
        throw std::runtime_error("cannot resolve " + host_);
    int fd = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    if (fd < 0 || bind(fd, res->ai_addr, res->ai_addrlen) != 0 || ::listen(fd, 1) != 0) {
        int err = errno;
        freeaddrinfo(res);
        if (fd >= 0) ::close(fd);
        throw std::runtime_error(host_ + ":" + port + ": " + strerror(err));
    }
    freeaddrinfo(res);
    listener_ = fd;
    log_.info("Waiting for connection on port " + std::to_string(port_));
}

void TcpPlayer::start() {
    pollfd p{listener_, POLLIN, 0};
    int wait_ms = accept_timeout_ ? static_cast<int>(*accept_timeout_ * 1000) : -1;
    int ready;
    do {
        ready = poll(&p, 1, wait_ms);
    } while (ready < 0 && errno == EINTR);
    int conn = ready > 0 ? accept(listener_, nullptr, nullptr) : -1;
    if (conn < 0) {
        log_.info("Timeout accepting player on port " + std::to_string(port_));
        ::close(listener_);
        listener_ = -1;
        log_.info("Stop listening port " + std::to_string(port_));
        failure = NO_CONNECTION;
        return;
    }
    ::close(listener_);
    listener_ = -1;
    log_.info("Got connection on port " + std::to_string(port_));
    int one = 1;
    setsockopt(conn, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
    set_timeout(conn, accept_timeout_);
    std::string token;
    try {
        SocketReader r(conn);
        token = r.string();
    } catch (const IoError&) {
        ::close(conn);
        failure = NO_CONNECTION;
        return;
    }
    if (token != token_) {
        ::close(conn);
        failure = TOKEN_MISMATCH;
        return;
    }
    set_timeout(conn, timeout_);
    sock_ = conn;
    reader_ = std::make_unique<SocketReader>(conn);
}

void TcpPlayer::send_all(const std::string& data) {
    size_t sent = 0;
    while (sent < data.size()) {
        ssize_t k = send(sock_, data.data() + sent, data.size() - sent, MSG_NOSIGNAL);
        if (k < 0) {
            if (errno == EINTR) continue;
            throw IoError(os_error(errno));
        }
        sent += static_cast<size_t>(k);
    }
}

// A DebugMessage carries a DebugCommand, read and thrown away (clients/Python/model/debug_*.py)
static void skip_vertex(SocketReader& r) {
    if (r.boolean()) {
        r.f32();
        r.f32();
    }
    for (int k = 0; k < 6; k++) r.f32();
}

static void skip_debug_command(SocketReader& r) {
    int32_t tag = r.i32();
    if (tag == 0) {                         // Add(DebugData)
        int32_t data = r.i32();
        if (data == 0) {                    // Log(text)
            r.string();
        } else if (data == 1) {             // Primitives(vertices, primitive_type)
            int32_t n = r.count();
            for (int32_t k = 0; k < n; k++) skip_vertex(r);
            r.i32();
        } else if (data == 2) {             // PlacedText(vertex, text, alignment, size)
            skip_vertex(r);
            r.string();
            r.f32();
            r.f32();
        } else {
            throw IoError("IO error: Unexpected tag " + std::to_string(data));
        }
    } else if (tag == 2) {                  // SetAutoFlush(enable)
        r.boolean();
    } else if (tag != 1 && tag != 3) {      // Clear, Flush
        throw IoError("IO error: Unexpected tag " + std::to_string(tag));
    }
}

Action TcpPlayer::get_action(const PlayerView& view) {
    Writer w;
    write_get_action(w, view, false);
    send_all(w.buf);
    SocketReader& r = *reader_;
    while (true) {
        int32_t tag = r.i32();
        if (tag == ACTION_MESSAGE) return read_action(r);
        if (tag == DEBUG_MESSAGE) skip_debug_command(r);
        else if (tag == DEBUG_UPDATE_DONE)
            throw IoError("IO error: Unexpected debug update done message in get_action");
        else if (tag == REQUEST_DEBUG_STATE)
            throw IoError("IO error: Requested debug state with no debug interface available");
        else throw IoError("IO error: Unexpected tag " + std::to_string(tag));
    }
}

void TcpPlayer::crash(const std::string& error, Log& log) {
    Seat::crash(error, log);
    drop();
}

void TcpPlayer::finish() {
    if (sock_ < 0) return;
    try {
        Writer w;
        write_finish(w);
        send_all(w.buf);
    } catch (const IoError&) {
    }
    drop();
}

void TcpPlayer::drop() {
    if (sock_ < 0) return;
    reader_.reset();
    ::close(sock_);
    sock_ = -1;
    log_.info("Dropping tcp player on port " + std::to_string(port_));
}

}  // namespace cc
