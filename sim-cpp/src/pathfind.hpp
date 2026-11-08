// The A* of move orders and hunting auto-attacks, as sim/codecraft_sim/pathfind.py (the official engine's search,
// read off its machine code): the same heap keys, the same random draws in the same order, the same insertion orders.
#pragma once
#include <cstdint>
#include <algorithm>
#include <map>
#include <optional>
#include <vector>

#include "model.hpp"
#include "rng.hpp"

namespace cc {

constexpr int32_t FREE = -1;

struct Grid {
    int n;
    std::vector<int32_t> cells;     // the entity on each cell (y * n + x), FREE when none

    explicit Grid(int size) : n(size), cells(static_cast<size_t>(size) * size, FREE) {}
    static Grid of(const Model& m);
    int index(Vec2 p) const { return p.y * n + p.x; }
    bool inside(int x, int y) const { return 0 <= x && x < n && 0 <= y && y < n; }
    void place(int32_t eid, Vec2 pos, int size);
    void clear(Vec2 pos, int size);
    int32_t occupant(Vec2 p) const { return inside(p.x, p.y) ? cells[static_cast<size_t>(p.y * n + p.x)] : FREE; }
    bool is_free(Vec2 p) const { return inside(p.x, p.y) && cells[static_cast<size_t>(p.y * n + p.x)] == FREE; }
    bool free_square(Vec2 pos, int size) const;
    // the cells (on the map) within Manhattan distance r of a square, the square included: x, then y, as Python
    template <class F>
    void around(Vec2 pos, int size, int r, F f) const {
        int x0 = std::max(pos.x - r, 0), x1 = std::min(pos.x + size + r, n);
        for (int x = x0; x < x1; x++) {
            int gap = std::max({0, pos.x - x, x - (pos.x + size - 1)});
            int rest = r - gap;
            int y0 = std::max(pos.y - rest, 0), y1 = std::min(pos.y + size + rest, n);
            for (int y = y0; y < y1; y++) f(y * n + x);
        }
    }
};

// A resource or building of another player as a player was last shown it (FogMemory).
struct Remembered {
    std::optional<int32_t> player_id;
    Vec2 position;
    int size;
    int32_t health;
};

// The price of entering a cell for a path of a player's unit (-1: it cannot be entered): 1 for a free cell, or under
// fog of war for a cell the player does not see (2020 rules) or where its FogMemory remembers nothing (the arena's);
// with break_through, another player's entity or a resource costs the hits it takes, ceil(health / damage), with
// the health last seen for a remembered one (rules.py, entry_cost).
struct Cost {
    const std::vector<int32_t>* cells;
    const std::vector<uint8_t>* seen = nullptr;      // under fog of war
    const std::vector<int32_t>* memory = nullptr;    // under fog of war, the arena's rules: FogMemory::cells
    const std::map<int32_t, Remembered>* known = nullptr;
    bool break_through = false;
    int damage = 0;
    std::optional<int32_t> own;
    const std::map<int32_t, Entity>* entities = nullptr;

    int operator()(int i) const {
        const size_t k = static_cast<size_t>(i);
        std::optional<int32_t> player;
        int32_t health;
        if (seen && !(*seen)[k]) {
            int32_t o = memory ? (*memory)[k] : FREE;
            if (o == FREE) return 1;
            if (!break_through) return -1;
            const Remembered& r = known->at(o);
            player = r.player_id;
            health = r.health;
        } else {
            int32_t o = (*cells)[k];
            if (o == FREE) return 1;
            if (!break_through) return -1;
            const Entity& b = entities->at(o);
            player = b.player_id;
            health = b.health;
        }
        if (player == own || damage <= 0) return -1;
        int h = std::max(health, 1);
        return (h + damage - 1) / damage;
    }
};

std::optional<Vec2> first_step(const Grid& grid, Vec2 start, Vec2 target, int max_nodes, const Cost& cost,
                               PyRandom& rng);
std::optional<Vec2> hunt_step(const Grid& grid, Vec2 start, const std::vector<char>& goals, int reach, int max_nodes,
                              const Cost& cost, PyRandom& rng);

}  // namespace cc
