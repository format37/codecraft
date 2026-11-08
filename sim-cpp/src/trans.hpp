// The `trans` binary encoding of the official engine (sim/codecraft_sim/trans.py): little-endian i32, bool = one
// byte, string = i32 length + UTF-8, Option = bool tag + value, Vec = i32 count + items, enums = i32 tags.
#pragma once
#include <cstdint>
#include <cstring>
#include <string>
#include <utility>
#include <vector>

#include "model.hpp"
#include "net.hpp"

namespace cc {

struct Writer {
    std::string buf;
    void i32(int32_t v) {
        uint32_t u = static_cast<uint32_t>(v);
        char b[4] = {static_cast<char>(u & 0xff), static_cast<char>((u >> 8) & 0xff),
                     static_cast<char>((u >> 16) & 0xff), static_cast<char>((u >> 24) & 0xff)};
        buf.append(b, 4);
    }
    void boolean(bool v) { buf += static_cast<char>(v ? 1 : 0); }
    void f32(float v) {
        int32_t i;
        static_assert(sizeof i == sizeof v, "float is 32 bits");
        memcpy(&i, &v, sizeof i);
        i32(i);
    }
    void string(const std::string& s) {
        i32(static_cast<int32_t>(s.size()));
        buf += s;
    }
};

// A failure of a seat's connection or of its bytes; the text is the official engine's ("IO error: ...").
struct IoError : std::runtime_error {
    using std::runtime_error::runtime_error;
};

// Reads from a socket through a buffer; each recv waits at most the socket's SO_RCVTIMEO.
class SocketReader {
public:
    explicit SocketReader(net::socket_t fd) : fd_(fd) {}
    void read(void* out, size_t n);
    int32_t i32();
    float f32();
    bool boolean();
    int32_t count();
    std::string string();
    bool buffered() const { return pos_ < len_; }    // bytes already read from the socket and not yet taken
    net::socket_t fd() const { return fd_; }

private:
    net::socket_t fd_;
    char buf_[8192];
    size_t pos_ = 0, len_ = 0;
};

using Action = std::vector<std::pair<int32_t, EntityAction>>;   // a client's orders, in the order sent

void write_vec2(Writer& w, Vec2 v);
void write_properties_map(Writer& w, const Properties& props);
void write_entity(Writer& w, const Entity& e);
void write_entity_action(Writer& w, const EntityAction& a);
void write_player_view(Writer& w, const PlayerView& v);
void write_get_action(Writer& w, const PlayerView& v, bool debug_available = false);
void write_finish(Writer& w);
void write_debug_update(Writer& w, const PlayerView& v);
Action read_action(SocketReader& r);

constexpr int32_t DEBUG_MESSAGE = 0, ACTION_MESSAGE = 1, DEBUG_UPDATE_DONE = 2, REQUEST_DEBUG_STATE = 3;

}  // namespace cc
