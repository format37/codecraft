// The debug interface of the official runner's GUI mode (docs/doc-en.md: `debug_update`, DebugCommand, DebugState),
// as the runner answers it (docs/private/engine-probes/step9): what a client draws, in its own frame, and what the
// viewer tells it back (window, mouse, pressed keys, camera).
#pragma once
#include <cstdint>
#include <string>
#include <vector>

#include "trans.hpp"

namespace cc {

struct DebugVertex {
    bool world = false;            // a world position (x, y); without one the vertex is at the screen's (0, 0)
    float x = 0, y = 0;
    float sx = 0, sy = 0;          // screen offset, pixels, y up
    float r = 1, g = 1, b = 1, a = 1;
};

struct DebugData {
    enum Kind { Log = 0, Primitives = 1, PlacedText = 2 };
    Kind kind = Log;
    std::string text;                    // Log, PlacedText
    std::vector<DebugVertex> vertices;   // Primitives; PlacedText: its one vertex
    int32_t primitive = 0;               // 0 lines, 1 triangles
    float alignment = 0, size = 0;       // PlacedText
};

struct DebugCommand {
    enum Tag { Add = 0, Clear = 1, SetAutoFlush = 2, Flush = 3 };
    Tag tag = Clear;
    DebugData data;                      // Add
    bool enable = false;                 // SetAutoFlush
};

DebugCommand read_debug_command(SocketReader& r);

// What the viewer reports, in the global frame; write_debug_state turns the points into the seat's frame.
struct DebugInput {
    int32_t width = 800, height = 600;           // the drawing canvas, pixels
    float mouse_x = 0, mouse_y = 0;              // the mouse in the canvas: pixels, origin at the bottom left
    float world_x = 0, world_y = 0;              // the world point under the mouse
    std::vector<std::string> keys;               // the official names: A, Num1, Space, LShift, LCtrl, Left, F1...
    float camera_x = 40, camera_y = 40;          // the world point at the centre of the canvas
    float distance = 45;                         // half the height of the visible world
};

void write_debug_state(Writer& w, const DebugInput& in, int seat, int map_size);

// A point (not a cell) between a seat's frame and the global one: the frames of view.cpp, turned by quarter turns.
void point_from_view(int seat, float& x, float& y, int n);
void point_to_view(int seat, float& x, float& y, int n);

}  // namespace cc
