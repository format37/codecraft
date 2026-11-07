#include "pathfind.hpp"

#include <algorithm>
#include <cstdlib>

namespace cc {

Grid Grid::of(const Model& m) {
    Grid g(m.map_size);
    for (const auto& kv : m.entities) g.place(kv.first, kv.second.position, m.props[kv.second.entity_type].size);
    return g;
}

void Grid::place(int32_t eid, Vec2 pos, int size) {
    for (int y = std::max(pos.y, 0); y < std::min(pos.y + size, n); y++)
        for (int x = std::max(pos.x, 0); x < std::min(pos.x + size, n); x++)
            cells[static_cast<size_t>(y * n + x)] = eid;
}

void Grid::clear(Vec2 pos, int size) { place(FREE, pos, size); }

bool Grid::free_square(Vec2 pos, int size) const {
    if (pos.x < 0 || pos.y < 0 || pos.x + size > n || pos.y + size > n) return false;
    for (int y = pos.y; y < pos.y + size; y++)
        for (int x = pos.x; x < pos.x + size; x++)
            if (cells[static_cast<size_t>(y * n + x)] != FREE) return false;
    return true;
}

namespace {

constexpr int64_t INF = int64_t(1) << 60;

// A heap entry (f, tie, random, cell) of the Python search; `tie` is always 1 without `prefer` (the corpus check's).
struct Entry {
    int64_t f;
    double r;
    int cell;
};
// std::push_heap makes a max-heap: "a < b" when a comes out later
struct Later {
    bool operator()(const Entry& a, const Entry& b) const {
        if (a.f != b.f) return a.f > b.f;
        if (a.r != b.r) return a.r > b.r;
        return a.cell > b.cell;
    }
};

// Scratch arrays reused between searches, cleared by generation stamps.
struct Scratch {
    std::vector<int64_t> g;
    std::vector<int> parent;
    std::vector<uint32_t> seen_gen, closed_gen;
    std::vector<int> order;            // the cells in the order g got them (a Python dict's insertion order)
    std::vector<Entry> heap;
    uint32_t gen = 0;
    void reset(size_t cells) {
        if (g.size() != cells) {
            g.assign(cells, 0);
            parent.assign(cells, 0);
            seen_gen.assign(cells, 0);
            closed_gen.assign(cells, 0);
            gen = 0;
        }
        if (++gen == 0) {
            std::fill(seen_gen.begin(), seen_gen.end(), 0);
            std::fill(closed_gen.begin(), closed_gen.end(), 0);
            gen = 1;
        }
        order.clear();
        heap.clear();
    }
    bool has(int i) const { return seen_gen[static_cast<size_t>(i)] == gen; }
    int64_t get(int i) const { return has(i) ? g[static_cast<size_t>(i)] : INF; }
    void set(int i, int64_t v, int from) {
        if (!has(i)) {
            seen_gen[static_cast<size_t>(i)] = gen;
            order.push_back(i);
        }
        g[static_cast<size_t>(i)] = v;
        parent[static_cast<size_t>(i)] = from;
    }
    bool closed(int i) const { return closed_gen[static_cast<size_t>(i)] == gen; }
    void close(int i) { closed_gen[static_cast<size_t>(i)] = gen; }
    void push(Entry e) {
        heap.push_back(e);
        std::push_heap(heap.begin(), heap.end(), Later());
    }
    Entry pop() {
        std::pop_heap(heap.begin(), heap.end(), Later());
        Entry e = heap.back();
        heap.pop_back();
        return e;
    }
};

Scratch scratch;            // the game loop is single-threaded

std::optional<Vec2> step_to(int n, const Scratch& s, int start, int end) {
    if (end == start) return std::nullopt;
    int i = end;
    while (s.parent[static_cast<size_t>(i)] != start) i = s.parent[static_cast<size_t>(i)];
    return Vec2{i % n, i / n};
}

int pick(const std::vector<int>& cands, PyRandom& rng) {
    return cands.size() == 1 ? cands[0] : rng.choice(cands);
}

const int DX[4] = {1, -1, 0, 0};
const int DY[4] = {0, 0, 1, -1};

}  // namespace

std::optional<Vec2> first_step(const Grid& grid, Vec2 start, Vec2 target, int max_nodes, const Cost& cost,
                               PyRandom& rng) {
    int n = grid.n, tx = target.x, ty = target.y;
    int start_i = grid.index(start);
    int target_i = grid.inside(tx, ty) ? ty * n + tx : -1;
    Scratch& s = scratch;
    s.reset(static_cast<size_t>(n) * n);
    s.set(start_i, 0, start_i);
    s.push(Entry{std::abs(start.x - tx) + std::abs(start.y - ty), 0.0, start_i});
    int closed = 0;
    while (!s.heap.empty()) {
        int i = s.pop().cell;
        if (s.closed(i)) continue;
        s.close(i);
        closed++;
        if (i == target_i) return step_to(n, s, start_i, i);
        if (closed > max_nodes) break;
        int x = i % n, y = i / n;
        int64_t gi = s.g[static_cast<size_t>(i)];
        for (int k = 0; k < 4; k++) {
            int nx = x + DX[k], ny = y + DY[k];
            if (!(0 <= nx && nx < n && 0 <= ny && ny < n)) continue;
            int j = ny * n + nx;
            int c = cost(j);
            if (c < 0) continue;
            int64_t ng = gi + c;
            if (ng < s.get(j)) {
                s.set(j, ng, i);
                s.push(Entry{ng + std::abs(nx - tx) + std::abs(ny - ty), rng.random(), j});
            }
        }
    }
    // the target was not reached: the closest cell reached, the cheaper path first, one at random among equals
    bool any = false;
    int64_t best_h = 0, best_g = 0;
    std::vector<int> cands;
    for (int i : s.order) {
        int64_t h = std::abs(i % n - tx) + std::abs(i / n - ty), gi = s.g[static_cast<size_t>(i)];
        if (!any || h < best_h || (h == best_h && gi < best_g)) {
            any = true;
            best_h = h;
            best_g = gi;
            cands.assign(1, i);
        } else if (h == best_h && gi == best_g) {
            cands.push_back(i);
        }
    }
    return step_to(n, s, start_i, pick(cands, rng));
}

std::optional<Vec2> hunt_step(const Grid& grid, Vec2 start, const std::vector<char>& goals, int reach, int max_nodes,
                              const Cost& cost, PyRandom& rng) {
    int n = grid.n;
    int start_i = grid.index(start);
    if (goals[static_cast<size_t>(start_i)]) return std::nullopt;
    Scratch& s = scratch;
    s.reset(static_cast<size_t>(n) * n);
    s.set(start_i, 0, start_i);
    s.push(Entry{1, 0.0, start_i});
    int closed = 0;
    bool found_any = false;
    int64_t found = 0;
    while (!s.heap.empty()) {
        Entry e = s.pop();
        int i = e.cell;
        if (found_any && e.f > found) break;
        if (s.closed(i)) continue;
        s.close(i);
        closed++;
        if (closed > max_nodes) break;
        int64_t gi = s.g[static_cast<size_t>(i)];
        if (goals[static_cast<size_t>(i)] && !found_any) {
            found_any = true;
            found = gi;
        }
        int x = i % n, y = i / n;
        for (int k = 0; k < 4; k++) {
            int nx = x + DX[k], ny = y + DY[k];
            if (!(0 <= nx && nx < n && 0 <= ny && ny < n)) continue;
            int j = ny * n + nx;
            int c = cost(j);
            if (c < 0) continue;
            int64_t ng = gi + c;
            if (ng <= reach && ng < s.get(j)) {
                s.set(j, ng, i);
                s.push(Entry{ng + (goals[static_cast<size_t>(j)] ? 0 : 1), rng.random(), j});
            }
        }
    }
    std::vector<int> reached;
    for (int i : s.order)
        if (goals[static_cast<size_t>(i)]) reached.push_back(i);
    if (reached.empty()) return std::nullopt;
    int64_t nearest = INF;
    for (int i : reached) nearest = std::min(nearest, s.g[static_cast<size_t>(i)]);
    std::vector<int> cands;
    for (int i : reached)
        if (s.g[static_cast<size_t>(i)] == nearest) cands.push_back(i);
    return step_to(n, s, start_i, pick(cands, rng));
}

}  // namespace cc
