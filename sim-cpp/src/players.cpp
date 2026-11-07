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
                     std::optional<std::string> token, Log& log)
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
    if (token_ && token != *token_) {
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

void TcpPlayer::set_debug(DebugHost* host, int seat, int map_size) {
    debug_ = host;
    seat_ = seat;
    map_size_ = map_size;
}

// The next message's tag. With the viewer, the drawings done so far are handed over when the client goes quiet
// (it may stop at a breakpoint in the middle of its turn: what it drew until then is shown, as in the official GUI).
int32_t TcpPlayer::next_tag() {
    if (debug_ && !unsent_.empty() && !reader_->buffered()) {
        pollfd p{sock_, POLLIN, 0};
        if (poll(&p, 1, 30) == 0) hand_over();
    }
    return reader_->i32();
}

void TcpPlayer::hand_over() {
    if (!debug_ || unsent_.empty()) return;
    debug_->debug_ops(seat_, unsent_layer_, std::move(unsent_));
    unsent_.clear();
}

void TcpPlayer::apply(int layer, DebugOp op) {
    if (!unsent_.empty() && unsent_layer_ != layer) hand_over();
    unsent_layer_ = layer;
    unsent_.push_back(std::move(op));
}

// Add and Clear go to `layer` at once, or with autoflush off wait for Flush (and then still go to the layer of the
// tick they were sent in: the official GUI shows a late-flushed drawing on that tick).
void TcpPlayer::debug_command(const DebugCommand& c, int layer) {
    if (c.tag == DebugCommand::Add || c.tag == DebugCommand::Clear) {
        DebugOp op;
        op.clear = c.tag == DebugCommand::Clear;
        if (!op.clear) {
            op.data = c.data;
            for (DebugVertex& v : op.data.vertices)
                if (v.world) point_from_view(seat_, v.x, v.y, map_size_);
        }
        if (autoflush_) apply(layer, std::move(op));
        else pending_.emplace_back(layer, std::move(op));
        return;
    }
    if (c.tag == DebugCommand::SetAutoFlush) autoflush_ = c.enable;
    if (c.tag == DebugCommand::Flush || (c.tag == DebugCommand::SetAutoFlush && c.enable)) {
        for (auto& kv : pending_) apply(kv.first, std::move(kv.second));
        pending_.clear();
    }
}

void TcpPlayer::send_debug_state() {
    hand_over();
    Writer w;
    write_debug_state(w, debug_->debug_input(), seat_, map_size_);
    send_all(w.buf);
}

Action TcpPlayer::get_action(const PlayerView& view) {
    Writer w;
    write_get_action(w, view, debug_ != nullptr);
    send_all(w.buf);
    SocketReader& r = *reader_;
    while (true) {
        int32_t tag = next_tag();
        if (tag == ACTION_MESSAGE) {
            Action a = read_action(r);
            hand_over();
            return a;
        }
        if (tag == DEBUG_MESSAGE) {
            DebugCommand c = read_debug_command(r);        // without the viewer: read and ignored
            if (debug_) debug_command(c, view.current_tick);
        } else if (tag == DEBUG_UPDATE_DONE) {
            throw IoError("IO error: Unexpected debug update done message in get_action");
        } else if (tag == REQUEST_DEBUG_STATE) {
            if (!debug_) throw IoError("IO error: Requested debug state with no debug interface available");
            send_debug_state();
        } else {
            throw IoError("IO error: Unexpected tag " + std::to_string(tag));
        }
    }
}

void TcpPlayer::debug_update(const PlayerView& view) {
    Writer w;
    write_debug_update(w, view);
    send_all(w.buf);
    SocketReader& r = *reader_;
    while (true) {
        int32_t tag = next_tag();
        if (tag == DEBUG_UPDATE_DONE) {
            hand_over();
            return;
        }
        if (tag == DEBUG_MESSAGE) debug_command(read_debug_command(r), -1);
        else if (tag == REQUEST_DEBUG_STATE) send_debug_state();
        else if (tag == ACTION_MESSAGE) throw IoError("IO error: Unexpected action message in debug update");
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
