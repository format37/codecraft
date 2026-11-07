#include "mapgen.hpp"

#include <algorithm>
#include <deque>
#include <optional>
#include <set>
#include <stdexcept>
#include <tuple>

namespace cc {

namespace {

using T = EntityType;
const int MARGIN = 2;
const int SHARE_MIN = 30, SHARE_MAX = 55;
const int OCTAVES[2][2] = {{8, 2}, {4, 1}};
const int DIRS[8][2] = {{1, 0}, {-1, 0}, {0, 1}, {0, -1}, {1, 1}, {1, -1}, {-1, 1}, {-1, -1}};
const int64_t ONE = int64_t(1) << 16;

std::vector<std::pair<EntityType, Vec2>> round1_entities() {
    return {{T::BuilderBase, {5, 5}}, {T::BuilderUnit, {4, 4}}, {T::MeleeBase, {5, 15}}, {T::MeleeUnit, {4, 14}},
            {T::RangedBase, {15, 5}}, {T::RangedUnit, {14, 4}}, {T::Turret, {15, 15}}};
}

std::vector<std::pair<EntityType, Vec2>> round2_entities() {
    return {{T::BuilderBase, {5, 5}}, {T::BuilderUnit, {4, 4}}};
}

struct Box {
    int x0, y0, x1, y1;
};

std::optional<Box> seat0_box(const Options& o) {
    if (o.starting_entities.empty()) return std::nullopt;
    Box b{INT32_MAX, INT32_MAX, INT32_MIN, INT32_MIN};
    for (const auto& te : o.starting_entities) {
        int s = o.props[te.first].size;
        b.x0 = std::min(b.x0, te.second.x);
        b.y0 = std::min(b.y0, te.second.y);
        b.x1 = std::max(b.x1, te.second.x + s - 1);
        b.y1 = std::max(b.y1, te.second.y + s - 1);
    }
    return Box{b.x0 - MARGIN, b.y0 - MARGIN, b.x1 + MARGIN, b.y1 + MARGIN};
}

Box seat_box(const Box& b, int n, int seat) {
    Vec2 a = place(Vec2{b.x0, b.y0}, 1, n, seat), c = place(Vec2{b.x1, b.y1}, 1, n, seat);
    return Box{std::min(a.x, c.x), std::min(a.y, c.y), std::max(a.x, c.x), std::max(a.y, c.y)};
}

// a cell and its images under the eight symmetries of the square (with repeats)
std::vector<std::pair<int, int>> images(int x, int y, int n) {
    std::vector<std::pair<int, int>> out;
    for (auto ab : {std::make_pair(x, y), std::make_pair(y, x)}) {
        int a = ab.first, b = ab.second;
        out.push_back({a, b});
        out.push_back({n - 1 - a, b});
        out.push_back({a, n - 1 - b});
        out.push_back({n - 1 - a, n - 1 - b});
    }
    return out;
}

int64_t floor_shift16(int64_t v) { return v >= 0 ? v >> 16 : -((-v + ONE - 1) >> 16); }

std::vector<int64_t> noise(int q, PyRandom& rng) {
    std::vector<int64_t> out(static_cast<size_t>(q) * q, 0);
    for (const auto& oct : OCTAVES) {
        int spacing = oct[0], weight = oct[1];
        int ox = static_cast<int>(below(rng, static_cast<uint32_t>(spacing)));
        int oy = static_cast<int>(below(rng, static_cast<uint32_t>(spacing)));
        int cells = (q + spacing) / spacing + 2;
        std::vector<int> grads(static_cast<size_t>(cells) * cells);
        for (int& g : grads) g = static_cast<int>(rng.getrandbits(3));
        int64_t d = 2 * spacing;
        std::vector<int64_t> frac(static_cast<size_t>(spacing)), fade(static_cast<size_t>(spacing));
        for (int a = 0; a < spacing; a++) {
            int64_t c = 2 * a + 1;
            frac[static_cast<size_t>(a)] = (c * ONE) / d;
            fade[static_cast<size_t>(a)] = ((3 * c * c * d - 2 * c * c * c) * ONE) / (d * d * d);
        }
        for (int y = 0; y < q; y++) {
            int j = (y + oy) / spacing, b = (y + oy) % spacing;
            int64_t v = frac[static_cast<size_t>(b)], wy = fade[static_cast<size_t>(b)];
            for (int x = 0; x < q; x++) {
                int i = (x + ox) / spacing, a = (x + ox) % spacing;
                int64_t u = frac[static_cast<size_t>(a)], wx = fade[static_cast<size_t>(a)];
                size_t k = static_cast<size_t>(j * cells + i);
                const int* g00 = DIRS[grads[k]];
                const int* g10 = DIRS[grads[k + 1]];
                const int* g01 = DIRS[grads[k + static_cast<size_t>(cells)]];
                const int* g11 = DIRS[grads[k + static_cast<size_t>(cells) + 1]];
                int64_t n00 = g00[0] * u + g00[1] * v;
                int64_t n10 = g10[0] * (u - ONE) + g10[1] * v;
                int64_t n01 = g01[0] * u + g01[1] * (v - ONE);
                int64_t n11 = g11[0] * (u - ONE) + g11[1] * (v - ONE);
                int64_t nx0 = n00 + floor_shift16((n10 - n00) * wx);
                int64_t nx1 = n01 + floor_shift16((n11 - n01) * wx);
                out[static_cast<size_t>(y * q + x)] += (nx0 + floor_shift16((nx1 - nx0) * wy)) * weight;
            }
        }
    }
    return out;
}

void carve(const Options& o, std::vector<char>& cells) {
    int n = o.map_size;
    auto box = seat0_box(o);
    if (!box) return;
    auto idx = [n](int x, int y) { return static_cast<size_t>(y) * n + x; };
    std::vector<char> taken(static_cast<size_t>(n) * n, 0);
    for (int seat = 0; seat < 4; seat++)
        for (const auto& te : o.starting_entities) {
            int s = o.props[te.first].size;
            Vec2 p = place(te.second, s, n, seat);
            for (int dx = 0; dx < s; dx++)
                for (int dy = 0; dy < s; dy++) {
                    int x = p.x + dx, y = p.y + dy;
                    if (0 <= x && x < n && 0 <= y && y < n) taken[idx(x, y)] = 1;
                }
        }
    std::vector<std::vector<char>> zones;
    std::vector<std::vector<std::pair<int, int>>> zone_cells;
    for (int seat = 0; seat < 4; seat++) {
        Box b = seat_box(*box, n, seat);
        std::vector<char> z(static_cast<size_t>(n) * n, 0);
        std::vector<std::pair<int, int>> zc;
        for (int x = std::max(b.x0, 0); x < std::min(b.x1 + 1, n); x++)
            for (int y = std::max(b.y0, 0); y < std::min(b.y1 + 1, n); y++)
                if (!taken[idx(x, y)]) {
                    z[idx(x, y)] = 1;
                    zc.push_back({x, y});
                }
        zones.push_back(std::move(z));
        zone_cells.push_back(std::move(zc));
    }
    for (int target : {2, 3}) {
        if (zone_cells[0].empty() || zone_cells[static_cast<size_t>(target)].empty()) continue;
        bool overlap = false;
        for (const auto& c : zone_cells[0])
            if (zones[static_cast<size_t>(target)][idx(c.first, c.second)]) overlap = true;
        if (overlap) continue;
        // a 0-1 search: a step onto a resource costs 1, onto a free cell 0
        const int UNSEEN = 1 << 30;
        std::vector<int> dist(static_cast<size_t>(n) * n, UNSEEN);
        std::vector<int> parent(static_cast<size_t>(n) * n, -1);
        std::vector<std::pair<int, int>> starts = zone_cells[0];
        std::sort(starts.begin(), starts.end());
        std::deque<std::pair<int, int>> todo;          // (distance, cell index)
        for (const auto& c : starts) {
            dist[idx(c.first, c.second)] = 0;
            todo.push_back({0, static_cast<int>(idx(c.first, c.second))});
        }
        int end = -1;
        while (!todo.empty()) {
            auto dc = todo.front();
            todo.pop_front();
            int dd = dc.first, ci = dc.second;
            if (dd > dist[static_cast<size_t>(ci)]) continue;
            if (zones[static_cast<size_t>(target)][static_cast<size_t>(ci)]) {
                end = ci;
                break;
            }
            int x = ci % n, y = ci / n;
            const int NB[4][2] = {{x + 1, y}, {x - 1, y}, {x, y + 1}, {x, y - 1}};
            for (const auto& nb : NB) {
                int nx = nb[0], ny = nb[1];
                if (!(0 <= nx && nx < n && 0 <= ny && ny < n) || taken[idx(nx, ny)]) continue;
                int w = cells[idx(nx, ny)] ? 1 : 0;
                size_t ni = idx(nx, ny);
                if (dd + w < dist[ni]) {
                    dist[ni] = dd + w;
                    parent[ni] = ci;
                    if (w) todo.push_back({dd + 1, static_cast<int>(ni)});
                    else todo.push_front({dd, static_cast<int>(ni)});
                }
            }
        }
        while (end >= 0) {
            int x = end % n, y = end / n;
            if (cells[static_cast<size_t>(end)])
                for (auto c : images(x, y, n)) cells[idx(c.first, c.second)] = 0;
            end = parent[static_cast<size_t>(end)];
        }
    }
}

std::vector<Vec2> resource_cells(const Options& o, PyRandom& rng) {
    int n = o.map_size;
    int q = (n + 1) / 2;
    auto box = seat0_box(o);
    auto kept_free = [&](int x, int y) {
        if (!box) return false;
        for (auto c : images(x, y, n))
            if (box->x0 <= c.first && c.first <= box->x1 && box->y0 <= c.second && c.second <= box->y1) return true;
        return false;
    };
    std::vector<int64_t> values = noise(q, rng);
    std::vector<std::pair<int, int>> candidates;
    for (int x = 0; x < q; x++)
        for (int y = 0; y <= x; y++)
            if (!kept_free(x, y)) candidates.push_back({x, y});
    std::sort(candidates.begin(), candidates.end(), [&](const auto& a, const auto& b) {
        int64_t va = values[static_cast<size_t>(a.second * q + a.first)];
        int64_t vb = values[static_cast<size_t>(b.second * q + b.first)];
        if (va != vb) return va > vb;
        return a < b;
    });
    int share = SHARE_MIN + static_cast<int>(below(rng, static_cast<uint32_t>(SHARE_MAX - SHARE_MIN + 1)));
    std::vector<char> cells(static_cast<size_t>(n) * n, 0);
    size_t take = candidates.size() * static_cast<size_t>(share) / 100;
    for (size_t k = 0; k < take; k++)
        for (auto c : images(candidates[k].first, candidates[k].second, n))
            cells[static_cast<size_t>(c.second) * n + c.first] = 1;
    carve(o, cells);
    std::vector<Vec2> out;
    for (int x = 0; x < n; x++)
        for (int y = 0; y < n; y++)
            if (cells[static_cast<size_t>(y) * n + x]) out.push_back(Vec2{x, y});
    return out;
}

}  // namespace

uint32_t below(PyRandom& rng, uint32_t n) { return rng.randbelow(n); }

Options preset(const std::string& name) {
    Options o;
    if (name == "Round1") {
        o.fog_of_war = false;
        o.starting_entities = round1_entities();
    } else if (name == "Round2" || name == "Finals") {
        o.fog_of_war = true;
        o.starting_entities = round2_entities();
    } else {
        throw std::invalid_argument("unknown variant `" + name + "`, expected one of Round1, Round2, Finals, `Custom`");
    }
    return o;
}

Options options_from_json(const Json& d) {
    for (const char* key : {"map_size", "initial_resource", "passive_income", "max_pathfind_nodes", "fog_of_war",
                            "starting_entities"})
        if (!d.get(key)) throw JsonError(std::string("missing field `") + key + "`");
    Options o;
    if (const Json* ep = d.get("entity_properties"); ep && !ep->is_null()) {
        o.props = Properties();
        for (const auto& kv : ep->obj) {
            auto t = parse_type(kv.first);
            if (!t) throw JsonError("unknown variant `" + kv.first + "`");
            o.props.set(*t, properties_from_json(kv.second));
        }
    }
    o.map_size = static_cast<int32_t>(d.at("map_size").as_int());
    o.initial_resource = static_cast<int32_t>(d.at("initial_resource").as_int());
    o.passive_income = static_cast<int32_t>(d.at("passive_income").as_int());
    o.max_pathfind_nodes = static_cast<int32_t>(d.at("max_pathfind_nodes").as_int());
    o.fog_of_war = d.at("fog_of_war").as_bool();
    const Json* mt = d.get("max_tick_count");
    o.max_tick_count = mt && !mt->is_null() ? static_cast<int32_t>(mt->as_int()) : 1000;
    o.starting_entities.clear();
    for (const Json& te : d.at("starting_entities").arr) {
        if (te.arr.size() != 2) throw JsonError("a starting entity is [type, position]");
        o.starting_entities.push_back({type_from_json(te.arr[0]), vec_from_json(te.arr[1])});
    }
    return o;
}

Vec2 place(Vec2 pos, int size, int n, int seat) {
    int x = pos.x, y = pos.y;
    switch (seat) {
        case 1: return Vec2{n - x - size, n - y - size};
        case 2: return Vec2{n - y - size, x};
        case 3: return Vec2{y, n - x - size};
        default: return Vec2{x, y};
    }
}

Model create(const Options& o, int seats, PyRandom& rng) {
    if (seats < 1 || seats > 4) throw std::invalid_argument(std::to_string(seats) + " players: a map has four corners");
    Model m;
    m.map_size = o.map_size;
    m.fog_of_war = o.fog_of_war;
    m.props = o.props;
    m.max_tick_count = o.max_tick_count;
    m.max_pathfind_nodes = o.max_pathfind_nodes;
    m.current_tick = 0;
    m.passive_income = o.passive_income;
    for (int seat = 0; seat < seats; seat++) m.players[seat + 1] = Player{seat + 1, seat, 0, o.initial_resource};
    int32_t next_id = seats + 1;
    for (int seat = 0; seat < seats; seat++)
        for (const auto& te : o.starting_entities) {
            const EntityProperties& p = o.props[te.first];
            m.entities[next_id] = Entity{next_id, seat + 1, te.first, place(te.second, p.size, o.map_size, seat),
                                         p.max_health, true};
            next_id++;
        }
    int resource_health = o.props[T::Resource].max_health;
    for (Vec2 c : resource_cells(o, rng)) {
        m.entities[next_id] = Entity{next_id, std::nullopt, T::Resource, c, resource_health, true};
        next_id++;
    }
    m.next_id = next_id;
    return m;
}

}  // namespace cc
