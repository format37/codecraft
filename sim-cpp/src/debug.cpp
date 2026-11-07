#include "debug.hpp"

#include <cmath>

namespace cc {

static DebugVertex read_vertex(SocketReader& r) {
    DebugVertex v;
    v.world = r.boolean();
    if (v.world) {
        v.x = r.f32();
        v.y = r.f32();
    }
    v.sx = r.f32();
    v.sy = r.f32();
    v.r = r.f32();
    v.g = r.f32();
    v.b = r.f32();
    v.a = r.f32();
    return v;
}

DebugCommand read_debug_command(SocketReader& r) {
    DebugCommand c;
    int32_t tag = r.i32();
    if (tag == DebugCommand::Add) {
        c.tag = DebugCommand::Add;
        int32_t kind = r.i32();
        if (kind == DebugData::Log) {
            c.data.kind = DebugData::Log;
            c.data.text = r.string();
        } else if (kind == DebugData::Primitives) {
            c.data.kind = DebugData::Primitives;
            int32_t n = r.count();
            c.data.vertices.reserve(static_cast<size_t>(n));
            for (int32_t k = 0; k < n; k++) c.data.vertices.push_back(read_vertex(r));
            c.data.primitive = r.i32();
            if (c.data.primitive != 0 && c.data.primitive != 1)
                throw IoError("IO error: Unexpected tag " + std::to_string(c.data.primitive));
        } else if (kind == DebugData::PlacedText) {
            c.data.kind = DebugData::PlacedText;
            c.data.vertices.push_back(read_vertex(r));
            c.data.text = r.string();
            c.data.alignment = r.f32();
            c.data.size = r.f32();
        } else {
            throw IoError("IO error: Unexpected tag " + std::to_string(kind));
        }
    } else if (tag == DebugCommand::Clear) {
        c.tag = DebugCommand::Clear;
    } else if (tag == DebugCommand::SetAutoFlush) {
        c.tag = DebugCommand::SetAutoFlush;
        c.enable = r.boolean();
    } else if (tag == DebugCommand::Flush) {
        c.tag = DebugCommand::Flush;
    } else {
        throw IoError("IO error: Unexpected tag " + std::to_string(tag));
    }
    return c;
}

void point_from_view(int seat, float& x, float& y, int n) {
    float a = x, b = y, fn = static_cast<float>(n);
    switch (seat) {
        case 1: x = fn - a; y = fn - b; break;
        case 2: x = fn - b; y = a; break;
        case 3: x = b; y = fn - a; break;
        default: break;
    }
}

void point_to_view(int seat, float& x, float& y, int n) {
    float a = x, b = y, fn = static_cast<float>(n);
    switch (seat) {
        case 1: x = fn - a; y = fn - b; break;
        case 2: x = b; y = fn - a; break;
        case 3: x = fn - b; y = a; break;
        default: break;
    }
}

void write_debug_state(Writer& w, const DebugInput& in, int seat, int map_size) {
    float wx = in.world_x, wy = in.world_y, cx = in.camera_x, cy = in.camera_y;
    point_to_view(seat, wx, wy, map_size);
    point_to_view(seat, cx, cy, map_size);
    w.i32(in.width);
    w.i32(in.height);
    w.f32(in.mouse_x);
    w.f32(in.mouse_y);
    w.f32(wx);
    w.f32(wy);
    w.i32(static_cast<int32_t>(in.keys.size()));
    for (const std::string& k : in.keys) w.string(k);
    w.f32(cx);                                    // camera: looking straight down, without perspective
    w.f32(cy);
    w.f32(0.0f);                                  // rotation
    w.f32(static_cast<float>(M_PI / 2));          // attack
    w.f32(in.distance);
    w.boolean(false);                             // perspective
    w.i32(seat);
}

}  // namespace cc
