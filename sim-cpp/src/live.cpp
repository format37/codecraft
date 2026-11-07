#include "live.hpp"

#include <arpa/inet.h>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <climits>
#include <cstdio>
#include <cstring>
#include <netinet/in.h>
#include <poll.h>
#include <stdexcept>
#include <sys/socket.h>
#include <unistd.h>

#include "jsonout.hpp"

extern const char VIEWER_HTML[];     // build/viewer_html.cpp, made from viewer/viewer.html by the Makefile
extern const char ENGINE_VERSION[];  // build/version.cpp: the version of the engine's source

namespace cc {

namespace {

bool send_all(int fd, const char* data, size_t n) {
    while (n > 0) {
        ssize_t k = send(fd, data, n, MSG_NOSIGNAL);
        if (k < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        data += k;
        n -= static_cast<size_t>(k);
    }
    return true;
}

bool send_all(int fd, const std::string& s) { return send_all(fd, s.data(), s.size()); }

// The page, with the engine's version where it says %ENGINE_VERSION%.
const std::string& viewer_page() {
    static const std::string page = [] {
        std::string s = VIEWER_HTML;
        const std::string token = "%ENGINE_VERSION%";
        for (size_t at; (at = s.find(token)) != std::string::npos;) s.replace(at, token.size(), ENGINE_VERSION);
        return s;
    }();
    return page;
}

void respond(int fd, int code, const char* type, const std::string& body) {
    const char* reason = code == 200 ? "OK" : code == 400 ? "Bad Request" : code == 403 ? "Forbidden" : "Not Found";
    std::string head = "HTTP/1.1 " + std::to_string(code) + " " + reason + "\r\nContent-Type: " + type +
                       "\r\nContent-Length: " + std::to_string(body.size()) +
                       "\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n";
    if (send_all(fd, head)) send_all(fd, body);
}

std::string lower(std::string s) {
    for (char& c : s) c = static_cast<char>(tolower(static_cast<unsigned char>(c)));
    return s;
}

std::string trim(const std::string& s) {
    size_t a = s.find_first_not_of(" \t"), b = s.find_last_not_of(" \t");
    return a == std::string::npos ? std::string() : s.substr(a, b - a + 1);
}

struct Request {
    std::string method, path, host, type, body;
};

bool read_request(int fd, Request& req) {
    std::string data;
    char buf[8192];
    size_t end;
    while ((end = data.find("\r\n\r\n")) == std::string::npos) {
        if (data.size() > 65536) return false;
        ssize_t n = recv(fd, buf, sizeof buf, 0);
        if (n <= 0) return false;
        data.append(buf, static_cast<size_t>(n));
    }
    std::string head = data.substr(0, end);
    size_t eol = head.find("\r\n");
    std::string line = head.substr(0, eol);
    size_t a = line.find(' '), b = a == std::string::npos ? a : line.find(' ', a + 1);
    if (b == std::string::npos) return false;
    req.method = line.substr(0, a);
    req.path = line.substr(a + 1, b - a - 1);
    size_t q = req.path.find('?');
    if (q != std::string::npos) req.path.resize(q);
    size_t length = 0;
    size_t pos = eol == std::string::npos ? head.size() : eol + 2;
    while (pos < head.size()) {
        size_t next = head.find("\r\n", pos);
        if (next == std::string::npos) next = head.size();
        std::string h = head.substr(pos, next - pos);
        size_t colon = h.find(':');
        if (colon != std::string::npos) {
            std::string name = lower(trim(h.substr(0, colon))), value = trim(h.substr(colon + 1));
            if (name == "content-length") length = static_cast<size_t>(strtoul(value.c_str(), nullptr, 10));
            else if (name == "host") req.host = lower(value);
            else if (name == "content-type") req.type = lower(value);
        }
        pos = next + 2;
    }
    if (length > (16u << 20)) return false;
    req.body = data.substr(end + 4);
    while (req.body.size() < length) {
        ssize_t n = recv(fd, buf, sizeof buf, 0);
        if (n <= 0) return false;
        req.body.append(buf, static_cast<size_t>(n));
    }
    req.body.resize(length);
    return true;
}

std::string frame(const std::string& name, const std::string& json) {
    return "event: " + name + "\ndata: " + json + "\n\n";
}

double number(const Json& body, const char* key, double fallback) {
    const Json* v = body.get(key);
    return v && v->is_number() ? v->as_double() : fallback;
}

bool flag(const Json& body, const char* key, bool fallback) {
    const Json* v = body.get(key);
    return v && v->kind == Json::Bool ? v->b : fallback;
}

std::string message(const std::string& text) {
    Out o;
    o.raw('{').key("message").str(text).raw('}');
    return o.s;
}

std::string cwd_path(const std::string& name) {
    char buf[PATH_MAX];
    return getcwd(buf, sizeof buf) ? std::string(buf) + "/" + name : name;
}

}  // namespace

Live::Live(Log& log, double speed) : log_(log) { control_.speed = speed; }

Live::~Live() {
    stop_ = true;
    events_cv_.notify_all();
    input_cv_.notify_all();
    if (acceptor_.joinable()) acceptor_.join();
    for (int k = 0; k < 40 && active_ > 0; k++) std::this_thread::sleep_for(std::chrono::milliseconds(50));
    if (listener_ >= 0) close(listener_);
}

int Live::listen(const std::string& host, int port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) throw std::runtime_error(strerror(errno));
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<uint16_t>(port));
    if (inet_pton(AF_INET, host.c_str(), &addr.sin_addr) != 1) {
        close(fd);
        throw std::runtime_error("bad address " + host);
    }
    if (bind(fd, reinterpret_cast<sockaddr*>(&addr), sizeof addr) != 0 || ::listen(fd, 16) != 0) {
        int err = errno;
        close(fd);
        throw std::runtime_error(host + ":" + std::to_string(port) + ": " + strerror(err));
    }
    socklen_t len = sizeof addr;
    getsockname(fd, reinterpret_cast<sockaddr*>(&addr), &len);
    listener_ = fd;
    port_ = ntohs(addr.sin_port);
    return port_;
}

void Live::start() { acceptor_ = std::thread([this] { accept_loop(); }); }

void Live::accept_loop() {
    while (!stop_) {
        pollfd p{listener_, POLLIN, 0};
        if (poll(&p, 1, 300) <= 0) continue;
        int fd = accept(listener_, nullptr, nullptr);
        if (fd < 0) continue;
        timeval tv{10, 0};                         // a request is read, an event written, within 10 s
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
        active_++;
        std::thread([this, fd] {
            serve(fd);
            active_--;
        }).detach();
    }
}

// Only the page itself may talk to the server: the Host must be this address (no DNS rebinding) and a POST must
// carry JSON (another site's form or no-cors fetch cannot, without a preflight this server never answers).
void Live::serve(int fd) {
    Request req;
    if (!read_request(fd, req)) {
        close(fd);
        return;
    }
    std::string port = std::to_string(port_);
    if (req.host != "127.0.0.1:" + port && req.host != "localhost:" + port && req.host != "[::1]:" + port) {
        respond(fd, 403, "text/plain", "forbidden\n");
    } else if (req.method == "GET" && (req.path == "/" || req.path == "/index.html")) {
        respond(fd, 200, "text/html; charset=utf-8", viewer_page());
    } else if (req.method == "GET" && req.path == "/events") {
        stream(fd);
        return;
    } else if (req.method == "POST" && req.type.rfind("application/json", 0) == 0) {
        Json body;
        try {
            body = parse_json(req.body.empty() ? "{}" : req.body);
        } catch (const std::exception& e) {
            respond(fd, 400, "text/plain", std::string(e.what()) + "\n");
            close(fd);
            return;
        }
        std::string reply = post(req.path, body);
        if (reply.empty()) respond(fd, 404, "text/plain", "not found\n");
        else respond(fd, 200, "application/json", reply);
    } else {
        respond(fd, req.method == "POST" ? 403 : 404, "text/plain", "no\n");
    }
    close(fd);
}

// The event stream: the game's events from its start, then each new one; a new game starts it again.
void Live::stream(int fd) {
    std::string head = "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n"
                       "Connection: keep-alive\r\n\r\n";
    int generation = -1;
    size_t sent = 0;
    bool ok = send_all(fd, head);
    while (ok && !stop_) {
        std::string out;
        {
            std::unique_lock<std::mutex> lock(mutex_);
            events_cv_.wait_for(lock, std::chrono::seconds(5), [&] {
                return stop_ || generation_ != generation || events_.size() > sent;
            });
            if (stop_) break;
            if (generation_ != generation) {
                generation = generation_;
                sent = 0;
            }
            for (; sent < events_.size() && out.size() < (4u << 20); sent++) out += events_[sent];
        }
        ok = send_all(fd, out.empty() ? std::string(": ping\n\n") : out);
    }
    close(fd);
}

std::string Live::post(const std::string& path, const Json& body) {
    if (path == "/control") {
        std::lock_guard<std::mutex> lock(mutex_);
        control_.playing = flag(body, "playing", control_.playing);
        control_.speed = std::max(0.0, number(body, "speed", control_.speed));
        control_.follow = flag(body, "follow", control_.follow);
        control_.display = static_cast<int>(number(body, "display", control_.display));
        if (flag(body, "step", false)) steps_++;
        inputs_++;
        input_cv_.notify_all();
        return "{}";
    }
    if (path == "/input") {
        DebugInput in;
        in.width = static_cast<int32_t>(number(body, "w", 800));
        in.height = static_cast<int32_t>(number(body, "h", 600));
        in.mouse_x = static_cast<float>(number(body, "mx", 0));
        in.mouse_y = static_cast<float>(number(body, "my", 0));
        in.world_x = static_cast<float>(number(body, "wx", 0));
        in.world_y = static_cast<float>(number(body, "wy", 0));
        in.camera_x = static_cast<float>(number(body, "cx", 40));
        in.camera_y = static_cast<float>(number(body, "cy", 40));
        in.distance = static_cast<float>(number(body, "dist", 45));
        const Json* keys = body.get("keys");
        if (keys && keys->kind == Json::Array)
            for (const Json& k : keys->arr)
                if (k.kind == Json::String && k.s.size() <= 32 && in.keys.size() < 64) in.keys.push_back(k.s);
        std::lock_guard<std::mutex> lock(mutex_);
        input_ = in;
        return "{}";
    }
    if (path == "/start") {
        const Json* config = body.get("config");
        if (!config || config->kind != Json::Object) return message("no config");
        std::lock_guard<std::mutex> lock(mutex_);
        config_ = *config;
        input_cv_.notify_all();
        return "{}";
    }
    if (path == "/reset") {
        std::unique_lock<std::mutex> lock(mutex_);
        reset_ = flag(body, "same_seed", false);
        inputs_++;
        input_cv_.notify_all();
        if (interrupt_) {
            interrupt_(false);                         // stop waiting for connections
            // a strategy that does not answer (a breakpoint): its connection is cut after a while
            if (!ended_cv_.wait_for(lock, std::chrono::milliseconds(1500), [&] { return !interrupt_; }) &&
                interrupt_)
                interrupt_(true);
        }
        return "{}";
    }
    if (path == "/new") {
        std::lock_guard<std::mutex> lock(mutex_);
        new_game_ = true;
        input_cv_.notify_all();
        return "{}";
    }
    if (path == "/save-state") return message(save_state(static_cast<int>(number(body, "tick", -1))));
    if (path == "/save-replay") return message(save_replay());
    return "";
}

std::string Live::save_state(int tick) {
    std::shared_ptr<GameRecord> r;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        r = record_;
    }
    if (!r) return "no game to save";
    try {
        Model m = r->history.at(tick);
        std::string name = cwd_path("codecraft-state-" + std::to_string(r->seed) + "-t" + std::to_string(tick) +
                                    ".json");
        FILE* f = fopen(name.c_str(), "wb");
        if (!f) return "cannot write " + name + ": " + strerror(errno);
        std::string text = dump_json(model_to_json(m), 2);
        fwrite(text.data(), 1, text.size(), f);
        fclose(f);
        log_.info("Saved the state of tick " + std::to_string(tick) + " to " + name);
        return "state of tick " + std::to_string(tick) + " saved to " + name + " (\"game\": {\"LoadFrom\": ...})";
    } catch (const std::exception& e) {
        return std::string("cannot save the state: ") + e.what();
    }
}

std::string Live::save_replay() {
    std::shared_ptr<GameRecord> r;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        r = record_;
    }
    if (r && !r->replay && !r->source.empty()) return "this is the replay " + r->source + " already";
    if (!r || !r->replay) return "no game to save";
    std::string bytes;
    {
        std::lock_guard<std::mutex> lock(r->replay_mutex);
        bytes = r->replay->bytes();
    }
    std::string name = cwd_path("codecraft-replay-" + std::to_string(r->seed) + ".dat");
    FILE* f = fopen(name.c_str(), "wb");
    if (!f) return "cannot write " + name + ": " + strerror(errno);
    fwrite(bytes.data(), 1, bytes.size(), f);
    fclose(f);
    log_.info("Saved the replay to " + name);
    return "replay saved to " + name;
}

void Live::new_game(const std::string& first_event, const std::string& json, std::shared_ptr<GameRecord> record) {
    std::lock_guard<std::mutex> lock(mutex_);
    generation_++;
    events_.clear();
    events_.push_back(frame(first_event, json));
    record_ = std::move(record);
    steps_ = 0;
    config_.reset();
    new_game_ = false;
    reset_.reset();
    events_cv_.notify_all();
}

void Live::event(const char* name, const std::string& json) {
    std::lock_guard<std::mutex> lock(mutex_);
    events_.push_back(frame(name, json));
    events_cv_.notify_all();
}

Control Live::control() {
    std::lock_guard<std::mutex> lock(mutex_);
    return control_;
}

bool Live::take_step() {
    std::lock_guard<std::mutex> lock(mutex_);
    if (steps_ == 0) return false;
    steps_--;
    return true;
}

void Live::wait(double seconds) {
    std::unique_lock<std::mutex> lock(mutex_);
    long seen = inputs_;
    input_cv_.wait_for(lock, std::chrono::duration<double>(seconds), [&] { return stop_ || inputs_ != seen; });
}

Json Live::wait_config() {
    std::unique_lock<std::mutex> lock(mutex_);
    input_cv_.wait(lock, [&] { return stop_ || config_.has_value(); });
    Json config = config_ ? *config_ : Json::object();
    config_.reset();
    return config;
}

void Live::set_interrupt(std::function<void(bool)> f) {
    std::lock_guard<std::mutex> lock(mutex_);
    interrupt_ = std::move(f);
    if (!interrupt_) ended_cv_.notify_all();
}

bool Live::reset_requested() {
    std::lock_guard<std::mutex> lock(mutex_);
    return reset_.has_value();
}

bool Live::take_reset(bool& same_seed) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (!reset_) return false;
    same_seed = *reset_;
    reset_.reset();
    return true;
}

Live::Next Live::wait_next() {
    std::unique_lock<std::mutex> lock(mutex_);
    input_cv_.wait(lock, [&] { return stop_ || new_game_ || reset_.has_value(); });
    if (reset_) return Next::Restart;
    new_game_ = false;
    return Next::NewGame;
}

void Live::wait_new_game() {
    std::unique_lock<std::mutex> lock(mutex_);
    input_cv_.wait(lock, [&] { return stop_ || new_game_; });
    new_game_ = false;
}

void Live::debug_ops(int seat, int layer, std::vector<DebugOp> ops) {
    Out o;
    o.raw('{').key("s").num(seat).key("t").num(layer).key("ops").raw('[');
    for (const DebugOp& op : ops) {
        o.sep();
        if (op.clear) {
            o.raw("{\"c\":1}");
            continue;
        }
        const DebugData& d = op.data;
        o.raw("{\"k\":").num(d.kind);
        if (d.kind == DebugData::Primitives) o.raw(",\"p\":").num(d.primitive);
        else o.raw(",\"x\":").str(d.text);
        if (d.kind != DebugData::Log) {
            o.raw(",\"v\":[");
            bool first = true;
            for (const DebugVertex& v : d.vertices) {
                if (!first) o.raw(',');
                first = false;
                if (v.world) o.flt(v.x).raw(',').flt(v.y);
                else o.raw("null,null");
                o.raw(',').flt(v.sx).raw(',').flt(v.sy).raw(',').flt(v.r).raw(',').flt(v.g).raw(',').flt(v.b);
                o.raw(',').flt(v.a);
            }
            o.raw(']');
        }
        if (d.kind == DebugData::PlacedText) o.raw(",\"al\":").flt(d.alignment).raw(",\"sz\":").flt(d.size);
        o.raw('}');
    }
    o.raw("]}");
    event("dbg", o.s);
}

DebugInput Live::debug_input() {
    std::lock_guard<std::mutex> lock(mutex_);
    return input_;
}

}  // namespace cc
