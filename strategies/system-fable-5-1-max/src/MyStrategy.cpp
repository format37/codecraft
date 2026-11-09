// system-fable-5-1-max: a seed strategy for the CodeCraft arena, written by Claude Fable 5.1.
//
// One file: compile.sh drops it into the official C++ client. Design (see README.md next to src/):
//   * every unit gets a one-step move each tick (the engine steps straight into an adjacent cell: no A*,
//     no node limit), planned on my own BFS fields and resolved so that two units never try to swap;
//   * builders own one mining cell each (sticky, the shortest walks granted first), stand next to a resource
//     and name it as a target; houses go on a pitch-5 lattice (2-wide corridors) next to the miners, with a
//     connectivity and reachability check on top; the ranged base site is reserved early;
//   * the army shoots with a kill-first assignment, steps into range only where its support outweighs the
//     shooters that cover the cell, gathers on open ground by the economy, and attacks the enemy base it knows
//     from the map's 8-fold symmetry only when it is clearly stronger, digging straight through resource walls;
//   * under fog the map is remembered (resources, enemy buildings) and the unseen part is inferred from
//     the symmetry of the generator.
// Tunables read CC_<NAME> from the environment (knob()), so variants need no recompiling; CC_DEBUG=1..3
// prints to stderr.
#include "MyStrategy.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <map>
#include <set>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace {

// ------------------------------------------------------------------------------------------- knobs
double knob(const char* name, double def) {
    static std::unordered_map<std::string, double> cache;
    auto it = cache.find(name);
    if (it != cache.end()) return it->second;
    std::string var = std::string("CC_") + name;
    const char* v = getenv(var.c_str());
    double value = v && *v ? atof(v) : def;
    cache[name] = value;
    return value;
}
int DEBUG = 0;

// ---------------------------------------------------------------------------------------- geometry
const int DX[4] = {1, -1, 0, 0};
const int DY[4] = {0, 0, 1, -1};

// Manhattan distance between two axis-aligned squares (0 when they touch or overlap), the engine's metric
inline int sqDist(int ax, int ay, int as, int bx, int by, int bs) {
    int dx = std::max({0, bx - (ax + as - 1), ax - (bx + bs - 1)});
    int dy = std::max({0, by - (ay + as - 1), ay - (by + bs - 1)});
    return dx + dy;
}

struct Ent {
    int id;
    EntityType type;
    int x, y, size, hp, maxHp;
    bool active;
    int owner;      // -1: a resource
    bool mine;
};

struct TypeProps {
    int size = 1, cost = 0, maxHp = 0, popProvide = 0, popUse = 0, sight = 0, range = 0, damage = 0;
    int buildScore = 0, destroyScore = 0;
    bool canMove = false, canBuild = false, canAttack = false, canRepair = false;
};

EntityAction actMove(int x, int y) {
    return EntityAction(std::make_shared<MoveAction>(Vec2Int(x, y), true, false), nullptr, nullptr, nullptr);
}
EntityAction actAttack(int id) {
    return EntityAction(nullptr, nullptr, std::make_shared<AttackAction>(std::make_shared<int>(id), nullptr), nullptr);
}
EntityAction actBuild(EntityType t, int x, int y) {
    return EntityAction(nullptr, std::make_shared<BuildAction>(t, Vec2Int(x, y)), nullptr, nullptr);
}
EntityAction actRepair(int id) {
    return EntityAction(nullptr, nullptr, nullptr, std::make_shared<RepairAction>(id));
}
EntityAction actIdle() { return EntityAction(nullptr, nullptr, nullptr, nullptr); }

// ------------------------------------------------------------------------------------------- Brain
class Brain {
public:
    Action act(const PlayerView& v);

private:
    // ---- static per game
    int n = 0, me = -1, mySeat = 0, nPlayers = 2;
    bool fog = false, round1 = false;
    TypeProps P[10];
    int enemyBaseX[4] = {0, 0, 0, 0}, enemyBaseY[4] = {0, 0, 0, 0};    // by seat, in my frame
    int rallyX = 16, rallyY = 16;

    // ---- per tick
    int tick = 0, myResource = 0, myScore = 0;
    std::vector<Ent> ents;
    std::vector<int> grid;                 // cell -> index into ents, -1 free
    std::vector<int> own, enemies;         // indexes
    std::vector<int> builders, rangedU, meleeU, bbases, rbases, mbases, houses, turrets;   // own, indexes
    std::vector<uint8_t> seen;             // currently in sight
    int popUsed = 0, popProvided = 0;
    int reserved = 0;                      // resources promised to pending building sites
    std::unordered_map<int, EntityAction> out;
    std::vector<uint8_t> stationary;       // cells of own units that will not move this tick
    std::vector<uint8_t> moverCell;        // cells of own units that may move this tick
    std::vector<int> threatR;              // cells an enemy ranged/turret can shoot now
    std::vector<int> threatR1;             // ... after one step of theirs
    std::vector<int> threatM;              // cells an enemy melee can hit after one step
    std::vector<int> builderDanger;        // cells a builder should leave
    std::vector<uint8_t> reservedCell;     // planned building footprints
    std::vector<uint8_t> reach;            // cells my builders can walk to from the base (this tick's passability)
    std::vector<int> slotTaken;            // mining slot cell -> builder id, -1 free
    std::vector<uint8_t> resKnown;         // a resource stands there (seen now, or remembered under fog)
    bool adjResource(int x, int y) const {
        for (int k = 0; k < 4; k++) {
            int nx = x + DX[k], ny = y + DY[k];
            if (inside(nx, ny) && resKnown[cell(nx, ny)]) return true;
        }
        return false;
    }
    std::unordered_set<int> handled;       // unit ids with an action decided
    struct Want {
        int idx;                          // ents index
        std::vector<int> prefs;           // candidate cells in order; -1 = stay
        int priority;
    };
    std::vector<Want> wants;

    // ---- memory across ticks
    std::vector<int8_t> lastState;         // -1 never seen, 0 empty, 1 resource
    std::vector<int> lastTick, firstTick;
    std::vector<int8_t> firstState;
    std::vector<int> resHp;
    struct Remembered { EntityType type; int x, y, size, hp, lastSeen, owner; };
    std::map<int, Remembered> enemyBuildings;
    std::map<int, Remembered> enemyUnitsMem;
    struct Site {
        EntityType type;
        int x, y, size, created;
        int clearedTick = -1;           // when its footprint lost its last resource (-1: free from the start / not yet)
        int builder = -1;               // builder id placing it
        int placedId = -1;
        bool placed = false;
    };
    std::vector<Site> sites;
    std::set<std::pair<int, int>> badSites;
    std::unordered_map<int, int> slotOf;   // builder id -> cell
    std::unordered_map<int, int> idleTicks; // unit id -> consecutive ticks it wanted to move and could not
    // diagnostics: what builders did, accumulated between debug prints
    long long statMine = 0, statWalkSlot = 0, statWalkSite = 0, statRepair = 0, statBuild = 0, statFlee = 0, statIdle = 0,
              statReslot = 0, statWalkDist = 0, statClear = 0;
    std::unordered_map<int, int> prevSlot;
    std::unordered_map<int, int> avoidSlot; // builder id -> a slot it gave up on (jam), until tick
    std::unordered_map<int, int> armyMemo; // unit id -> last decision tick (for hold timers)
    int rbSiteX = -1, rbSiteY = -1;
    bool attacking = false;
    int attackSince = -1;
    int blobX = -1, blobY = -1, frontX = -1, frontY = -1;
    std::unordered_set<int> staged;          // fresh units waiting at the rally for a batch to form
    int lastHomeThreatTick = -1000, rbBuilt = 0, stuckTicks = 0, rbTriggerTick = -1, stageSince = -1;
    bool baseGuessVisited[4] = {false, false, false, false};
    std::vector<int> enemyHist;
    int enemyEstimate = 0;
    double enemyPeak = 0;
    const PlayerView* view_ = nullptr;
    int targetSeat = -1;
    int lastEnemySeenTick = -1000;
    int lastEnemySeenX = -1, lastEnemySeenY = -1;
    long long cpuMicros = 0;
    int unitsLostTotal = 0;
    std::unordered_set<int> ownIdsLastTick;

    // ---- BFS scratch
    std::vector<int> dist, parent, queue;
    std::vector<uint32_t> stamp;
    uint32_t gen = 0;

    // helpers
    inline int cell(int x, int y) const { return y * n + x; }
    inline bool inside(int x, int y) const { return x >= 0 && y >= 0 && x < n && y < n; }
    inline bool isFree(int c) const { return grid[c] < 0; }
    bool freeSquare(int x, int y, int s) const;
    bool onBaseRing(int c) const {
        int x = c % n, y = c / n;
        for (int idx : own) {
            const Ent& e = ents[idx];
            if ((e.type == BUILDER_BASE || e.type == RANGED_BASE || e.type == MELEE_BASE) && sqDist(x, y, 1, e.x, e.y, e.size) == 1) return true;
        }
        return false;
    }
    bool believedResource(int c) const;
    int imageEarliest(int x, int y, int8_t& state) const;
    void setup(const PlayerView& v);
    void parse(const PlayerView& v);
    void updateMemory();
    void computeThreats();
    void bfs(const std::vector<int>& sources, const std::vector<uint8_t>& pass, int maxDist = 1 << 30);
    void dial(const std::vector<int>& sources, const std::vector<uint8_t>& cost, int maxCost);
    int bfsNearest(int start, const std::vector<uint8_t>& pass, const std::vector<uint8_t>& goal, int maxDist,
                   int& firstStep);
    int bfsBest(int start, const std::vector<uint8_t>& pass, const std::vector<uint8_t>& goal,
                const std::vector<int>& bonus, int maxDist, int extra, int& firstStep);
    int firstStepTo(int start, int target) const;
    std::vector<uint8_t> passFor(bool unitsPass, bool resourcesPass) const;
    void economy();
    void evictFromSites();
    void planSites();
    bool siteOk(EntityType t, int x, int y, int s, const std::vector<uint8_t>& pass) const;
    bool footprintSeen(int x, int y, int s) const;
    bool cellKnown(int c) const;
    int footprintResources(int x, int y, int s) const;
    int connectivityLoss(int x, int y, int s, const std::vector<uint8_t>& pass) const;
    void assignBuildersToSites(const std::vector<uint8_t>& pass);
    void wantToward(int idx, const std::vector<uint8_t>& pass, int priority);
    int assignToCells(const std::vector<int>& cells, int k, int limit, std::vector<uint8_t>& pass,
                      std::unordered_set<int>& busy, int preferId, int priority,
                      const std::function<void(int, int)>& arrived);
    void repairDamaged(const std::vector<uint8_t>& pass);
    void mining(const std::vector<uint8_t>& pass);
    void produce();
    void military();
    void resolveMoves();
    void flee(int idx, const std::vector<uint8_t>& pass);
    int estimateIncome() const;
    void debugPrint();
    void accumulateStats();
    void debugMap();
};

// ------------------------------------------------------------------------------------- setup/parse
void Brain::setup(const PlayerView& v) {
    n = v.mapSize;
    me = v.myId;
    fog = v.fogOfWar;
    nPlayers = static_cast<int>(v.players.size());
    for (const auto& kv : v.entityProperties) {
        TypeProps& p = P[static_cast<int>(kv.first)];
        const EntityProperties& e = kv.second;
        p.size = e.size;
        p.cost = e.initialCost;
        p.maxHp = e.maxHealth;
        p.popProvide = e.populationProvide;
        p.popUse = e.populationUse;
        p.sight = e.sightRange;
        p.buildScore = e.buildScore;
        p.destroyScore = e.destroyScore;
        p.canMove = e.canMove;
        p.canBuild = e.build != nullptr;
        p.canRepair = e.repair != nullptr;
        if (e.attack) {
            p.canAttack = true;
            p.range = e.attack->attackRange;
            p.damage = e.attack->damage;
        }
    }
    // my seat: the player ids are seat + 1 in a created game; other seats' bases in my frame
    mySeat = me - 1;
    auto place = [&](int x, int y, int size, int seat, int& ox, int& oy) {
        switch (seat) {
            case 1: ox = n - x - size; oy = n - y - size; break;
            case 2: ox = n - y - size; oy = x; break;
            case 3: ox = y; oy = n - x - size; break;
            default: ox = x; oy = y; break;
        }
    };
    auto toView = [&](int x, int y, int size, int seat, int& ox, int& oy) {
        switch (seat) {
            case 1: ox = n - x - size; oy = n - y - size; break;
            case 2: ox = y; oy = n - x - size; break;
            case 3: ox = n - y - size; oy = x; break;
            default: ox = x; oy = y; break;
        }
    };
    for (int seat = 0; seat < 4; seat++) {
        int gx, gy;
        place(5, 5, 5, seat, gx, gy);
        toView(gx, gy, 5, mySeat, enemyBaseX[seat], enemyBaseY[seat]);
    }
    lastState.assign(static_cast<size_t>(n) * n, -1);
    firstState.assign(static_cast<size_t>(n) * n, -1);
    lastTick.assign(static_cast<size_t>(n) * n, -1);
    firstTick.assign(static_cast<size_t>(n) * n, -1);
    resHp.assign(static_cast<size_t>(n) * n, 0);
    dist.assign(static_cast<size_t>(n) * n, 0);
    parent.assign(static_cast<size_t>(n) * n, -1);
    stamp.assign(static_cast<size_t>(n) * n, 0);
    queue.resize(static_cast<size_t>(n) * n + 1);
    rallyX = rallyY = static_cast<int>(knob("RALLY", 16));
}

void Brain::parse(const PlayerView& v) {
    tick = v.currentTick;
    for (const Player& p : v.players)
        if (p.id == me) {
            myResource = p.resource;
            myScore = p.score;
        }
    ents.clear();
    ents.reserve(v.entities.size());
    grid.assign(static_cast<size_t>(n) * n, -1);
    own.clear(); enemies.clear(); builders.clear(); rangedU.clear(); meleeU.clear();
    bbases.clear(); rbases.clear(); mbases.clear(); houses.clear(); turrets.clear();
    popUsed = popProvided = 0;
    for (const Entity& e : v.entities) {
        Ent x;
        x.id = e.id;
        x.type = e.entityType;
        x.x = e.position.x;
        x.y = e.position.y;
        x.size = P[static_cast<int>(e.entityType)].size;
        x.hp = e.health;
        x.maxHp = P[static_cast<int>(e.entityType)].maxHp;
        x.active = e.active;
        x.owner = e.playerId ? *e.playerId : -1;
        x.mine = e.playerId && *e.playerId == me;
        int idx = static_cast<int>(ents.size());
        ents.push_back(x);
        for (int yy = x.y; yy < x.y + x.size; yy++)
            for (int xx = x.x; xx < x.x + x.size; xx++)
                if (inside(xx, yy)) grid[cell(xx, yy)] = idx;
        if (x.mine) {
            own.push_back(idx);
            const TypeProps& tp = P[static_cast<int>(x.type)];
            popUsed += tp.popUse;
            if (x.active) popProvided += tp.popProvide;
            switch (x.type) {
                case BUILDER_UNIT: builders.push_back(idx); break;
                case RANGED_UNIT: rangedU.push_back(idx); break;
                case MELEE_UNIT: meleeU.push_back(idx); break;
                case BUILDER_BASE: bbases.push_back(idx); break;
                case RANGED_BASE: rbases.push_back(idx); break;
                case MELEE_BASE: mbases.push_back(idx); break;
                case HOUSE: houses.push_back(idx); break;
                case TURRET: turrets.push_back(idx); break;
                default: break;
            }
        } else if (x.owner >= 0) {
            enemies.push_back(idx);
        }
    }
    if (tick == 0) round1 = !rbases.empty() || !mbases.empty();
    // sight
    seen.assign(static_cast<size_t>(n) * n, fog ? 0 : 1);
    if (fog) {
        for (int idx : own) {
            const Ent& e = ents[idx];
            int r = P[static_cast<int>(e.type)].sight;
            int x0 = std::max(e.x - r, 0), x1 = std::min(e.x + e.size + r, n);
            for (int x = x0; x < x1; x++) {
                int gap = std::max({0, e.x - x, x - (e.x + e.size - 1)});
                int rest = r - gap;
                int y0 = std::max(e.y - rest, 0), y1 = std::min(e.y + e.size + rest, n);
                for (int y = y0; y < y1; y++) seen[cell(x, y)] = 1;
            }
        }
    }
}

void Brain::updateMemory() {
    const size_t cells = static_cast<size_t>(n) * n;
    for (size_t c = 0; c < cells; c++) {
        if (!seen[c]) continue;
        int g = grid[c];
        int8_t st = (g >= 0 && ents[g].type == RESOURCE) ? 1 : 0;
        if (firstState[c] < 0) {
            firstState[c] = st;
            firstTick[c] = tick;
        }
        lastState[c] = st;
        lastTick[c] = tick;
        resHp[c] = st ? ents[g].hp : 0;
    }
    // enemy buildings and units
    for (int idx : enemies) {
        const Ent& e = ents[idx];
        Remembered r{e.type, e.x, e.y, e.size, e.hp, tick, e.owner};
        if (P[static_cast<int>(e.type)].canMove) enemyUnitsMem[e.id] = r;
        else enemyBuildings[e.id] = r;
    }
    std::unordered_set<int> visibleIds;
    for (int idx : enemies) visibleIds.insert(ents[idx].id);
    for (auto it = enemyBuildings.begin(); it != enemyBuildings.end();) {
        const Remembered& r = it->second;
        bool gone = false;
        if (!visibleIds.count(it->first)) {
            // any of its cells in sight while it is not there: it is gone
            for (int y = r.y; y < r.y + r.size && !gone; y++)
                for (int x = r.x; x < r.x + r.size; x++)
                    if (inside(x, y) && seen[cell(x, y)]) { gone = true; break; }
        }
        if (gone) it = enemyBuildings.erase(it);
        else ++it;
    }
    resKnown.assign(cells, 0);
    for (size_t c = 0; c < cells; c++) {
        int g = grid[c];
        if (g >= 0) resKnown[c] = ents[g].type == RESOURCE ? 1 : 0;
        else resKnown[c] = (!seen[c] && lastState[c] == 1) ? 1 : 0;
    }
    for (auto it = enemyUnitsMem.begin(); it != enemyUnitsMem.end();) {
        if (visibleIds.count(it->first)) { ++it; continue; }
        const Remembered& r = it->second;
        bool stale = tick - r.lastSeen > static_cast<int>(knob("UNIT_MEMORY", 12)) ||
                     (inside(r.x, r.y) && seen[cell(r.x, r.y)]);
        if (stale) it = enemyUnitsMem.erase(it);
        else ++it;
    }
    if (!enemies.empty()) {
        lastEnemySeenTick = tick;
    }
    // losses
    std::unordered_set<int> ids;
    for (int idx : own) ids.insert(ents[idx].id);
    for (int id : ownIdsLastTick)
        if (!ids.count(id)) unitsLostTotal++;
    ownIdsLastTick.swap(ids);
}

// the state of a cell never seen, from the earliest-seen of its images under the map's symmetries
int Brain::imageEarliest(int x, int y, int8_t& state) const {
    int best = 1 << 30;
    state = -1;
    const int xs[2] = {x, y}, ys[2] = {y, x};
    for (int k = 0; k < 2; k++) {
        int a = xs[k], b = ys[k];
        const int cx[4] = {a, n - 1 - a, a, n - 1 - a};
        const int cy[4] = {b, b, n - 1 - b, n - 1 - b};
        for (int j = 0; j < 4; j++) {
            int c = cell(cx[j], cy[j]);
            if (firstTick[c] >= 0 && firstTick[c] < best) {
                best = firstTick[c];
                state = firstState[c];
            }
        }
    }
    return best;
}

bool Brain::believedResource(int c) const {
    if (lastState[c] >= 0) return lastState[c] == 1;
    int8_t st;
    imageEarliest(c % n, c / n, st);
    return st == 1;
}

bool Brain::freeSquare(int x, int y, int s) const {
    if (x < 0 || y < 0 || x + s > n || y + s > n) return false;
    for (int yy = y; yy < y + s; yy++)
        for (int xx = x; xx < x + s; xx++) {
            int c = cell(xx, yy);
            if (grid[c] >= 0 || reservedCell[c]) return false;
            if (fog && !seen[c] && believedResource(c)) return false;
        }
    return true;
}

// ------------------------------------------------------------------------------------------ threats
void Brain::computeThreats() {
    const size_t cells = static_cast<size_t>(n) * n;
    threatR.assign(cells, 0);
    threatR1.assign(cells, 0);
    threatM.assign(cells, 0);
    builderDanger.assign(cells, 0);
    auto paint = [&](std::vector<int>& field, int ex, int ey, int es, int r) {
        int x0 = std::max(ex - r, 0), x1 = std::min(ex + es + r, n);
        for (int x = x0; x < x1; x++) {
            int gap = std::max({0, ex - x, x - (ex + es - 1)});
            int rest = r - gap;
            int y0 = std::max(ey - rest, 0), y1 = std::min(ey + es + rest, n);
            for (int y = y0; y < y1; y++) field[cell(x, y)]++;
        }
    };
    auto consider = [&](EntityType t, int x, int y, int size, bool active) {
        const TypeProps& tp = P[static_cast<int>(t)];
        if (!tp.canAttack || t == BUILDER_UNIT) return;
        if (!active) return;
        if (t == TURRET) {
            paint(threatR, x, y, size, tp.range);
            paint(threatR1, x, y, size, tp.range);
            paint(builderDanger, x, y, size, tp.range + 1);
        } else if (t == RANGED_UNIT) {
            paint(threatR, x, y, size, tp.range);
            paint(threatR1, x, y, size, tp.range + 1);
            paint(builderDanger, x, y, size, tp.range + 1);
        } else if (t == MELEE_UNIT) {
            paint(threatM, x, y, size, tp.range + 1);
            paint(builderDanger, x, y, size, tp.range + 2);
        }
    };
    for (int idx : enemies) consider(ents[idx].type, ents[idx].x, ents[idx].y, ents[idx].size, ents[idx].active);
    // remembered units out of sight count a little for builders only (they flee what they saw)
    std::unordered_set<int> visibleIds;
    for (int idx : enemies) visibleIds.insert(ents[idx].id);
    for (const auto& kv : enemyUnitsMem) {
        if (visibleIds.count(kv.first)) continue;
        const Remembered& r = kv.second;
        if ((r.type == RANGED_UNIT || r.type == MELEE_UNIT) && tick - r.lastSeen <= 6) paint(builderDanger, r.x, r.y, 1, 3);
    }
}

// ---------------------------------------------------------------------------------------------- BFS
void Brain::bfs(const std::vector<int>& sources, const std::vector<uint8_t>& pass, int maxDist) {
    if (++gen == 0) {
        std::fill(stamp.begin(), stamp.end(), 0);
        gen = 1;
    }
    int head = 0, tail = 0;
    for (int s : sources) {
        if (stamp[s] == gen) continue;
        stamp[s] = gen;
        dist[s] = 0;
        parent[s] = -1;
        queue[tail++] = s;
    }
    while (head < tail) {
        int c = queue[head++];
        int d = dist[c];
        if (d >= maxDist) continue;
        int x = c % n, y = c / n;
        for (int k = 0; k < 4; k++) {
            int nx = x + DX[k], ny = y + DY[k];
            if (!inside(nx, ny)) continue;
            int j = cell(nx, ny);
            if (stamp[j] == gen || !pass[j]) continue;
            stamp[j] = gen;
            dist[j] = d + 1;
            parent[j] = c;
            queue[tail++] = j;
        }
    }
}

// Dijkstra with small integer costs (Dial's buckets): cost[c] = 0 impassable, else the price of entering c
void Brain::dial(const std::vector<int>& sources, const std::vector<uint8_t>& cost, int maxCost) {
    if (++gen == 0) {
        std::fill(stamp.begin(), stamp.end(), 0);
        gen = 1;
    }
    const int B = maxCost + 1;
    std::vector<std::vector<int>> buckets(static_cast<size_t>(B));
    std::vector<uint8_t> closed(stamp.size(), 0);
    int pending = 0;
    for (int s0 : sources) {
        if (stamp[s0] == gen) continue;
        stamp[s0] = gen;
        dist[s0] = 0;
        parent[s0] = -1;
        buckets[0].push_back(s0);
        pending++;
    }
    for (int d = 0; pending > 0; d++) {
        std::vector<int>& bk = buckets[static_cast<size_t>(d % B)];
        size_t i = 0;
        while (i < bk.size()) {
            int c = bk[i++];
            if (dist[c] != d) continue;     // a stale entry
            if (closed[c]) continue;
            closed[c] = 1;
            pending--;
            int x = c % n, y = c / n;
            for (int k = 0; k < 4; k++) {
                int nx = x + DX[k], ny = y + DY[k];
                if (!inside(nx, ny)) continue;
                int j = cell(nx, ny);
                int w = cost[j];
                if (w == 0) continue;
                int nd = d + w;
                if (stamp[j] != gen || nd < dist[j]) {
                    if (stamp[j] != gen) pending++;
                    else if (closed[j]) continue;
                    stamp[j] = gen;
                    dist[j] = nd;
                    parent[j] = c;
                    buckets[static_cast<size_t>(nd % B)].push_back(j);
                }
            }
        }
        bk.clear();
        if (d > 100000) break;
    }
}

// BFS from one cell: the goal cell with the best (distance - bonus) among those within `extra` of the nearest;
// returns it (or -1) and the first step toward it
int Brain::bfsBest(int start, const std::vector<uint8_t>& pass, const std::vector<uint8_t>& goal,
                   const std::vector<int>& bonus, int maxDist, int extra, int& firstStep) {
    firstStep = -1;
    if (++gen == 0) {
        std::fill(stamp.begin(), stamp.end(), 0);
        gen = 1;
    }
    int head = 0, tail = 0;
    stamp[start] = gen;
    dist[start] = 0;
    parent[start] = -1;
    queue[tail++] = start;
    int best = -1, bestKey = 1 << 30, limit = maxDist;
    auto consider = [&](int c) {
        int key = dist[c] * 4 - bonus[c];
        if (key < bestKey) { bestKey = key; best = c; }
        if (limit == maxDist) limit = std::min(maxDist, dist[c] + extra);
    };
    if (goal[start]) consider(start);
    while (head < tail) {
        int c = queue[head++];
        int d = dist[c];
        if (d >= limit) continue;
        int x = c % n, y = c / n;
        for (int k = 0; k < 4; k++) {
            int nx = x + DX[k], ny = y + DY[k];
            if (!inside(nx, ny)) continue;
            int j = cell(nx, ny);
            if (stamp[j] == gen) continue;
            stamp[j] = gen;
            dist[j] = d + 1;
            parent[j] = c;
            if (goal[j]) consider(j);
            if (!pass[j]) continue;
            queue[tail++] = j;
        }
    }
    if (best >= 0 && best != start) firstStep = firstStepTo(start, best);
    return best;
}

// BFS from one cell to the nearest goal cell; returns the goal (or -1) and the first step toward it
int Brain::bfsNearest(int start, const std::vector<uint8_t>& pass, const std::vector<uint8_t>& goal, int maxDist,
                      int& firstStep) {
    firstStep = -1;
    if (++gen == 0) {
        std::fill(stamp.begin(), stamp.end(), 0);
        gen = 1;
    }
    int head = 0, tail = 0;
    stamp[start] = gen;
    dist[start] = 0;
    parent[start] = -1;
    queue[tail++] = start;
    if (goal[start]) return start;
    while (head < tail) {
        int c = queue[head++];
        int d = dist[c];
        if (d >= maxDist) continue;
        int x = c % n, y = c / n;
        for (int k = 0; k < 4; k++) {
            int nx = x + DX[k], ny = y + DY[k];
            if (!inside(nx, ny)) continue;
            int j = cell(nx, ny);
            if (stamp[j] == gen) continue;
            stamp[j] = gen;
            dist[j] = d + 1;
            parent[j] = c;
            if (goal[j]) {
                firstStep = firstStepTo(start, j);
                return j;
            }
            if (!pass[j]) continue;
            queue[tail++] = j;
        }
    }
    return -1;
}

int Brain::firstStepTo(int start, int target) const {
    int c = target;
    while (parent[c] != start && parent[c] != -1) c = parent[c];
    return c;
}

// passability for my units this tick: free cells, plus my own movers (they are expected to make way)
std::vector<uint8_t> Brain::passFor(bool unitsPass, bool resourcesPass) const {
    std::vector<uint8_t> pass(static_cast<size_t>(n) * n, 0);
    for (size_t c = 0; c < pass.size(); c++) {
        int g = grid[c];
        if (g < 0) {
            bool res = resKnown[c] || (fog && !seen[c] && believedResource(static_cast<int>(c)));
            pass[c] = res ? (resourcesPass ? 1 : 0) : 1;
        } else {
            const Ent& e = ents[g];
            if (e.type == RESOURCE) pass[c] = resourcesPass ? 1 : 0;
            else if (e.mine && P[static_cast<int>(e.type)].canMove) pass[c] = (unitsPass && !stationary[c]) ? 1 : 0;
            else pass[c] = 0;
        }
        if (pass[c] && !reservedCell.empty() && reservedCell[c] == 1) pass[c] = 0;     // a planned footprint: keep out
    }
    return pass;
}

// units standing on a planned footprint step off it (the build needs it empty)
void Brain::evictFromSites() {
    if (reservedCell.empty()) return;
    for (int idx : own) {
        const Ent& u = ents[idx];
        if (!P[static_cast<int>(u.type)].canMove || handled.count(u.id)) continue;
        int c = cell(u.x, u.y);
        if (reservedCell[c] != 1) continue;
        Want w{idx, {}, 8};
        std::vector<std::pair<int, int>> opts;
        for (int k = 0; k < 4; k++) {
            int nx = u.x + DX[k], ny = u.y + DY[k];
            if (!inside(nx, ny)) continue;
            int j = cell(nx, ny);
            int g = grid[j];
            bool free = (g < 0 && !resKnown[j]) || (g >= 0 && ents[g].mine && P[static_cast<int>(ents[g].type)].canMove && !stationary[j]);
            if (!free) continue;
            opts.push_back({reservedCell[j] == 1 ? 1 : 0, j});
        }
        std::sort(opts.begin(), opts.end());
        for (auto& o : opts) w.prefs.push_back(o.second);
        w.prefs.push_back(-1);
        wants.push_back(w);
        handled.insert(u.id);
    }
}

// ------------------------------------------------------------------------------------------ economy
int Brain::estimateIncome() const {
    int inc = 0;
    for (int idx : builders)
        if (adjResource(ents[idx].x, ents[idx].y)) inc++;
    return inc;
}

// would a building of size s at (x, y) cut free cells off from the rest? (the count of cells reachable from
// my builder base's surroundings that would be lost beyond the footprint itself)
int Brain::connectivityLoss(int x, int y, int s, const std::vector<uint8_t>& pass) const {
    // reachable before
    std::vector<uint8_t> p2 = pass;
    for (int yy = y; yy < y + s; yy++)
        for (int xx = x; xx < x + s; xx++) p2[cell(xx, yy)] = 0;
    // sources: cells around my builder base (or any own building)
    std::vector<int> src;
    for (int idx : bbases) {
        const Ent& b = ents[idx];
        for (int xx = b.x - 1; xx <= b.x + b.size; xx++)
            for (int yy = b.y - 1; yy <= b.y + b.size; yy++)
                if (inside(xx, yy) && sqDist(xx, yy, 1, b.x, b.y, b.size) == 1 && pass[cell(xx, yy)]) src.push_back(cell(xx, yy));
    }
    if (src.empty())
        for (int idx : own)
            if (pass[cell(ents[idx].x, ents[idx].y)]) src.push_back(cell(ents[idx].x, ents[idx].y));
    if (src.empty()) return 0;
    Brain* self = const_cast<Brain*>(this);
    self->bfs(src, pass);
    int before = 0;
    for (size_t c = 0; c < pass.size(); c++)
        if (stamp[c] == gen) before++;
    std::vector<int> src2;
    for (int c : src)
        if (p2[c]) src2.push_back(c);
    if (src2.empty()) return 1 << 20;
    self->bfs(src2, p2);
    int after = 0;
    for (size_t c = 0; c < pass.size(); c++)
        if (stamp[c] == gen) after++;
    int footprintReachable = 0;
    for (int yy = y; yy < y + s; yy++)
        for (int xx = x; xx < x + s; xx++)
            if (pass[cell(xx, yy)]) footprintReachable++;
    return before - after - footprintReachable;
}

bool Brain::cellKnown(int c) const {
    if (lastState[c] >= 0) return true;
    int8_t st;
    imageEarliest(c % n, c / n, st);
    return st >= 0;
}

bool Brain::footprintSeen(int x, int y, int s) const {
    if (!fog) return true;
    for (int yy = y; yy < y + s; yy++)
        for (int xx = x; xx < x + s; xx++)
            if (!cellKnown(cell(xx, yy))) return false;
    return true;
}

int Brain::footprintResources(int x, int y, int s) const {
    int r = 0;
    for (int yy = y; yy < y + s; yy++)
        for (int xx = x; xx < x + s; xx++) {
            int c = cell(xx, yy);
            int g = grid[c];
            if (g >= 0) {
                if (ents[g].type == RESOURCE) r++;
                else if (!P[static_cast<int>(ents[g].type)].canMove) return 1 << 20;
            } else if (resKnown[c] || (fog && !seen[c] && believedResource(c))) {
                r++;
            }
        }
    return r;
}

bool Brain::siteOk(EntityType t, int x, int y, int s, const std::vector<uint8_t>& pass) const {
    if (!freeSquare(x, y, s)) return false;
    if (!footprintSeen(x, y, s)) return false;
    if (badSites.count({x, y})) return false;
    if (rbSiteX >= 0 && t != RANGED_BASE && sqDist(x, y, s, rbSiteX, rbSiteY, 5) <= 1) return false;
    // keep my bases' spawn ring free: no building touching a base of mine
    for (int idx : own) {
        const Ent& e = ents[idx];
        if (e.type == BUILDER_BASE || e.type == RANGED_BASE || e.type == MELEE_BASE)
            if (sqDist(x, y, s, e.x, e.y, e.size) <= 1) return false;
    }
    for (const Site& st : sites)
        if (!st.placed && sqDist(x, y, s, st.x, st.y, st.size) == 0) return false;
    // at least one adjacent passable cell for the placer, reachable from my base
    bool adj = false;
    for (int xx = x - 1; xx <= x + s && !adj; xx++)
        for (int yy = y - 1; yy <= y + s; yy++)
            if (inside(xx, yy) && sqDist(xx, yy, 1, x, y, s) == 1 && pass[cell(xx, yy)] && (reach.empty() || reach[cell(xx, yy)])) { adj = true; break; }
    if (!adj) return false;
    if (connectivityLoss(x, y, s, pass) > 0) return false;
    return true;
}

void Brain::planSites() {
    // drop finished / stale sites
    for (auto it = sites.begin(); it != sites.end();) {
        Site& s = *it;
        bool drop = false;
        if (s.placed) {
            // the entity exists and is active -> done; gone -> drop
            bool found = false, active = false;
            for (int idx : own)
                if (ents[idx].id == s.placedId) { found = true; active = ents[idx].active; }
            if (!found || active) drop = true;
        } else {
            // was it placed meanwhile? look for an own inactive building of that type at the site
            for (int idx : own) {
                const Ent& e = ents[idx];
                if (e.type == s.type && e.x == s.x && e.y == s.y) {
                    s.placed = true;
                    s.placedId = e.id;
                }
            }
            if (!s.placed) {
                int expire = static_cast<int>(knob("SITE_EXPIRE", 70));
                int since = s.created;
                bool hasRes = footprintResources(s.x, s.y, s.size) > 0;
                if (hasRes) {
                    expire = static_cast<int>(knob("CLEAR_EXPIRE", 200));
                    s.clearedTick = -2;                       // being cleared
                } else if (s.clearedTick == -2) {
                    s.clearedTick = tick;                     // just cleared: the usual time to place it, from now
                }
                if (s.clearedTick >= 0) since = s.clearedTick;
                if (tick - since > expire) {
                    badSites.insert({s.x, s.y});
                    if (s.type == RANGED_BASE) rbSiteX = rbSiteY = -1;
                    drop = true;
                } else {
                    // a building or an enemy there: give it up; resources inside: a ranged base site waits for
                    // the builders to mine them (they are drawn to them), any other site is given up
                    bool blocked = false, resourcesInside = false;
                    for (int yy = s.y; yy < s.y + s.size; yy++)
                        for (int xx = s.x; xx < s.x + s.size; xx++) {
                            int g = grid[cell(xx, yy)];
                            if (g >= 0 && ents[g].type == RESOURCE) resourcesInside = true;
                            else if (g >= 0 && !P[static_cast<int>(ents[g].type)].canMove) blocked = true;
                            else if (g >= 0 && !ents[g].mine) blocked = true;
                        }
                    if (blocked || (resourcesInside && s.type != RANGED_BASE)) {
                        badSites.insert({s.x, s.y});
                        if (s.type == RANGED_BASE) rbSiteX = rbSiteY = -1;
                        drop = true;
                    }
                }
            }
        }
        if (drop) {
            if (DEBUG >= 2) fprintf(stderr, "[t%d] site %d at (%d,%d) dropped (%s)\n", tick, static_cast<int>(s.type), s.x, s.y, s.placed ? "done/gone" : "unplaced");
            it = sites.erase(it);
        } else {
            ++it;
        }
    }
    reservedCell.assign(static_cast<size_t>(n) * n, 0);
    for (const Site& s : sites)
        if (!s.placed) {
            uint8_t v = footprintResources(s.x, s.y, s.size) > 0 ? 2 : 1;     // 2: being cleared, walkable
            for (int yy = s.y; yy < s.y + s.size; yy++)
                for (int xx = s.x; xx < s.x + s.size; xx++) reservedCell[cell(xx, yy)] = v;
        }
    if (rbSiteX >= 0)
        for (int yy = rbSiteY; yy < rbSiteY + 5; yy++)
            for (int xx = rbSiteX; xx < rbSiteX + 5; xx++)
                if (inside(xx, yy) && !reservedCell[cell(xx, yy)]) reservedCell[cell(xx, yy)] = 1;

    std::vector<uint8_t> pass = passFor(true, false);
    int income = estimateIncome();
    // what my builders can reach: from the base's ring, or from the builders themselves
    {
        std::vector<int> src;
        for (int idx : bbases) {
            const Ent& b = ents[idx];
            for (int xx = b.x - 1; xx <= b.x + b.size; xx++)
                for (int yy = b.y - 1; yy <= b.y + b.size; yy++)
                    if (inside(xx, yy) && sqDist(xx, yy, 1, b.x, b.y, b.size) == 1 && pass[cell(xx, yy)]) src.push_back(cell(xx, yy));
        }
        for (int idx : builders) src.push_back(cell(ents[idx].x, ents[idx].y));
        reach.assign(static_cast<size_t>(n) * n, 0);
        if (!src.empty()) {
            bfs(src, pass);
            for (size_t c = 0; c < reach.size(); c++) reach[c] = stamp[c] == gen ? 1 : 0;
        }
    }

    // ---- houses
    int housesPending = 0, popPending = 0;
    for (const Site& s : sites)
        if (s.type == HOUSE) { housesPending++; popPending += P[HOUSE].popProvide; }
    for (int idx : own) {
        const Ent& e = ents[idx];
        if (!e.active && !P[static_cast<int>(e.type)].canMove) {
            bool counted = false;
            for (const Site& s : sites) if (s.placedId == e.id) counted = true;
            if (!counted) popPending += P[static_cast<int>(e.type)].popProvide;
        }
    }
    int margin = popProvided + popPending - popUsed;
    int wantMargin = static_cast<int>(knob("POP_MARGIN", 4) + income * knob("POP_AHEAD", 0.8));
    int maxPending = std::clamp(1 + income / static_cast<int>(knob("HOUSE_PAR_DIV", 8)), 2, 5);
    if (static_cast<int>(builders.size()) < static_cast<int>(knob("SMALL_ECON", 7))) maxPending = 1;
    int popCap = static_cast<int>(knob("POP_CAP", 400));
    bool wantHouse = margin < wantMargin && housesPending < maxPending && popProvided + popPending < popCap &&
                     !builders.empty() && (myResource + income * 10 >= P[HOUSE].cost || housesPending == 0);
    if (wantHouse) {
        // candidates: any free 3x3 on my side, scored by the walk from the builders and the base, the mining
        // slots it eats, the buildings it touches (walls form), a nudge toward the pitch-4 lattice; the best
        // few are checked for connectivity and reachability
        int bx = bbases.empty() ? 5 : ents[bbases[0]].x, by = bbases.empty() ? 5 : ents[bbases[0]].y;
        double bcx = 0, bcy = 0;
        for (int b : builders) { bcx += ents[b].x; bcy += ents[b].y; }
        bcx /= builders.size(); bcy /= builders.size();
        int bestX = -1, bestY = -1;
        int pitch = static_cast<int>(knob("HOUSE_PITCH", 5));
        std::vector<std::pair<double, int>> cands;
        for (int y = 0; y + 3 <= n && y <= n / 2 + 4; y++)
            for (int x = 0; x + 3 <= n && x <= n / 2 + 4; x++) {
                if (!freeSquare(x, y, 3) || badSites.count({x, y})) continue;
                if (!footprintSeen(x, y, 3)) continue;
                // reachable ring cell (cheap pre-check)
                bool reachable = false;
                for (int xx = x - 1; xx <= x + 3 && !reachable; xx++)
                    for (int yy = y - 1; yy <= y + 3; yy++)
                        if (inside(xx, yy) && sqDist(xx, yy, 1, x, y, 3) == 1 && reach[cell(xx, yy)]) { reachable = true; break; }
                if (!reachable) continue;
                int dMin = 1 << 30;
                for (int b : builders) dMin = std::min(dMin, std::abs(x + 1 - ents[b].x) + std::abs(y + 1 - ents[b].y));
                double d = dMin * knob("HOUSE_W_NEAREST", 1.0) +
                           (std::abs(x + 1 - bcx) + std::abs(y + 1 - bcy)) * knob("HOUSE_W_BUILDERS", 0.2) +
                           (std::abs(x + 1 - (bx + 2)) + std::abs(y + 1 - (by + 2))) * knob("HOUSE_W_BASE", 0.2);
                int slots = 0, touching = 0;
                for (int yy = y - 1; yy <= y + 3; yy++)
                    for (int xx = x - 1; xx <= x + 3; xx++) {
                        if (!inside(xx, yy)) continue;
                        if (xx >= x && xx < x + 3 && yy >= y && yy < y + 3) continue;
                        if (adjResource(xx, yy) && !resKnown[cell(xx, yy)]) slots++;
                        int g = grid[cell(xx, yy)];
                        if ((g >= 0 && !P[static_cast<int>(ents[g].type)].canMove && ents[g].type != RESOURCE) || reservedCell[cell(xx, yy)]) touching++;
                    }
                double score = d + slots * knob("HOUSE_SLOT_PEN", 0.8) + touching * knob("HOUSE_TOUCH_PEN", 2.0) +
                               ((x % pitch) || (y % pitch) ? knob("HOUSE_OFFGRID_PEN", 3.0) : 0) +
                               (std::abs(x + 1 - rallyX) + std::abs(y + 1 - rallyY) < 8 ? knob("HOUSE_RALLY_PEN", 6.0) : 0) +
                               (threatR1[cell(x, y)] ? 100 : 0);
                cands.push_back({score, cell(x, y)});
            }
        std::sort(cands.begin(), cands.end());
        int checks = 0;
        for (const auto& pr : cands) {
            if (checks++ >= static_cast<int>(knob("HOUSE_CHECKS", 24))) break;
            int x = pr.second % n, y = pr.second / n;
            if (!siteOk(HOUSE, x, y, 3, pass)) continue;
            bestX = x;
            bestY = y;
            break;
        }
        if (bestX >= 0) {
            Site s{HOUSE, bestX, bestY, 3, tick};
            sites.push_back(s);
            for (int yy = bestY; yy < bestY + 3; yy++)
                for (int xx = bestX; xx < bestX + 3; xx++) reservedCell[cell(xx, yy)] = 1;
            if (DEBUG >= 2) fprintf(stderr, "[t%d] house site (%d,%d) margin=%d income=%d\n", tick, bestX, bestY, margin, income);
        }
    }

    // ---- ranged base (fog rounds): one, once the economy runs; its site is reserved as soon as a free one
    // is known, so that the houses leave it alone
    bool rbPending = false;
    for (const Site& s : sites) if (s.type == RANGED_BASE) rbPending = true;
    bool homeThreatened = false;
    for (int idx : enemies) {
        const Ent& e = ents[idx];
        if (e.type == RANGED_UNIT || e.type == MELEE_UNIT || e.type == TURRET)
            if (e.x + e.y < static_cast<int>(knob("HOME_RADIUS", 40))) homeThreatened = true;
    }
    if (homeThreatened) lastHomeThreatTick = tick;
    bool rbSafe = tick - lastHomeThreatTick > static_cast<int>(knob("RB_SAFE_TICKS", 40));
    if (!round1 && rbases.empty() && !rbPending && (rbSafe || rbBuilt == 0)) {
        int needIncome = static_cast<int>(knob("RB_INCOME", 18));
        int deadline = static_cast<int>(knob("RB_TICK", 200));
        int nb = static_cast<int>(builders.size());
        bool trigger = income >= needIncome || (tick >= deadline && nb >= 8) || myResource >= 450;
        // a reserved site stays valid while it is free of entities
        if (rbSiteX >= 0) {
            bool still = true;
            for (int yy = rbSiteY; yy < rbSiteY + 5 && still; yy++)
                for (int xx = rbSiteX; xx < rbSiteX + 5; xx++) {
                    int g = grid[cell(xx, yy)];
                    if ((g >= 0 && !P[static_cast<int>(ents[g].type)].canMove) || resKnown[cell(xx, yy)]) { still = false; break; }
                }
            if (!still) {
                if (DEBUG >= 1) fprintf(stderr, "[t%d] ranged base reservation (%d,%d) lost\n", tick, rbSiteX, rbSiteY);
                rbSiteX = rbSiteY = -1;
            }
        }
        if (rbSiteX < 0 || trigger) {
            // site: a 5x5 near the builder base, toward the map's centre, connectivity-checked
            int bx = bbases.empty() ? 5 : ents[bbases[0]].x, by = bbases.empty() ? 5 : ents[bbases[0]].y;
            int bestX = rbSiteX, bestY = rbSiteY;
            double best = 1e18;
            // without a reservation, look for a free site every few ticks; at the trigger also allow resources
            bool search = rbSiteX < 0 ? (tick % 5 == 0) : false;
            if (trigger && rbTriggerTick < 0) rbTriggerTick = tick;
            bool patient = rbTriggerTick >= 0 && tick - rbTriggerTick < static_cast<int>(knob("RB_WAIT_FREE", 40));
            int passes = (trigger && rbSiteX < 0) ? (patient ? 1 : 2) : (search ? 1 : 0);
            if (trigger && rbSiteX < 0 && tick % 5 != 0 && patient) passes = 1;
            for (int pass2 = 0; pass2 < passes && (pass2 == 0 || bestX < 0); pass2++) {
                int maxRes = pass2 == 0 ? 0 : static_cast<int>(knob("RB_MAX_RES", 8));
                for (int y = 0; y + 5 <= n && y <= n / 2; y++)
                    for (int x = 0; x + 5 <= n && x <= n / 2; x++) {
                        if (badSites.count({x, y})) continue;
                        int r = footprintResources(x, y, 5);
                        if (r > maxRes) continue;
                        if (!footprintSeen(x, y, 5)) continue;
                        bool overlapsSite = false;
                        for (const Site& st : sites)
                            if (!st.placed && sqDist(x, y, 5, st.x, st.y, st.size) == 0) overlapsSite = true;
                        if (overlapsSite) continue;
                        double d = std::abs(x + 2 - (bx + 2)) + std::abs(y + 2 - (by + 2));
                        double toward = std::abs((x + 2) - (y + 2)) * 0.3;     // prefer the diagonal
                        int ringMissing = 0;
                        for (int xx = x - 1; xx <= x + 5; xx++)
                            for (int yy = y - 1; yy <= y + 5; yy++) {
                                if (sqDist(xx, yy, 1, x, y, 5) != 1) continue;
                                if (!inside(xx, yy)) { ringMissing++; continue; }
                                int g = grid[cell(xx, yy)];
                                if ((g >= 0 && !P[static_cast<int>(ents[g].type)].canMove) || resKnown[cell(xx, yy)] || reservedCell[cell(xx, yy)]) ringMissing++;
                            }
                        double score = d + toward + (x + y < 14 ? 6 : 0) + r * knob("RB_RES_PEN", 8.0) + ringMissing * knob("RB_RING_PEN", 1.5);
                        if (score >= best) continue;
                        if (r == 0) {
                            if (!siteOk(RANGED_BASE, x, y, 5, pass)) continue;
                        } else {
                            // the footprint minus its resources must not cut the map (approximation: check as if free)
                            std::vector<uint8_t> p3 = pass;
                            for (int yy = y; yy < y + 5; yy++)
                                for (int xx = x; xx < x + 5; xx++) p3[cell(xx, yy)] = 1;
                            if (connectivityLoss(x, y, 5, p3) > 0) continue;
                            bool touchesBase = false;
                            for (int idx : own) {
                                const Ent& e = ents[idx];
                                if ((e.type == BUILDER_BASE || e.type == RANGED_BASE || e.type == MELEE_BASE) &&
                                    sqDist(x, y, 5, e.x, e.y, e.size) <= 1) touchesBase = true;
                            }
                            if (touchesBase) continue;
                        }
                        best = score;
                        bestX = x;
                        bestY = y;
                    }
            }
            if (bestX >= 0 && (bestX != rbSiteX || bestY != rbSiteY)) {
                rbSiteX = bestX;
                rbSiteY = bestY;
                for (int yy = bestY; yy < bestY + 5; yy++)
                    for (int xx = bestX; xx < bestX + 5; xx++) reservedCell[cell(xx, yy)] = 1;
                if (DEBUG >= 1) fprintf(stderr, "[t%d] ranged base site reserved (%d,%d)\n", tick, bestX, bestY);
            }
            if (trigger && rbSiteX >= 0 && (rbSafe || rbBuilt == 0)) {
                sites.push_back(Site{RANGED_BASE, rbSiteX, rbSiteY, 5, tick});
                rbBuilt++;
                rbTriggerTick = -1;
                if (DEBUG >= 1) fprintf(stderr, "[t%d] ranged base site (%d,%d) started\n", tick, rbSiteX, rbSiteY);
            } else if (trigger && DEBUG >= 2 && tick % 20 == 0) {
                fprintf(stderr, "[t%d] no ranged base site yet\n", tick);
            }
        }
    }
    if (!rbases.empty()) rbSiteX = rbSiteY = -1;
    // more ranged bases when the money piles up: +500 score each and another unit a tick
    if (!rbases.empty() && !rbPending && rbSafe && tick < static_cast<int>(knob("RB_EXTRA_UNTIL", 880)) &&
        static_cast<int>(rbases.size()) < static_cast<int>(knob("RB_MAX", 3)) &&
        myResource >= static_cast<int>(knob("RB_EXTRA_RES", 1200)) && static_cast<int>(builders.size()) >= 40) {
        int bx = bbases.empty() ? 5 : ents[bbases[0]].x, by = bbases.empty() ? 5 : ents[bbases[0]].y;
        int bestX = -1, bestY = -1;
        double best = 1e18;
        for (int y = 0; y + 5 <= n && y <= n / 2; y++)
            for (int x = 0; x + 5 <= n && x <= n / 2; x++) {
                if (badSites.count({x, y})) continue;
                if (footprintResources(x, y, 5) != 0 || !footprintSeen(x, y, 5)) continue;
                double d = std::abs(x + 2 - (bx + 2)) + std::abs(y + 2 - (by + 2));
                if (d >= best) continue;
                if (!siteOk(RANGED_BASE, x, y, 5, pass)) continue;
                best = d;
                bestX = x;
                bestY = y;
            }
        if (bestX >= 0) {
            sites.push_back(Site{RANGED_BASE, bestX, bestY, 5, tick});
            rbBuilt++;
            if (DEBUG >= 1) fprintf(stderr, "[t%d] extra ranged base site (%d,%d)\n", tick, bestX, bestY);
        }
    }

    // ---- turrets: when enemies threaten the mining grounds and the economy is rich
    int turretsPending = 0;
    for (const Site& s : sites) if (s.type == TURRET) turretsPending++;
    // a pair of turrets at the rally once the ranged base stands: the anchor of the defence
    if (knob("TURRETS", 1) > 0 && turretsPending == 0 && !rbases.empty() && static_cast<int>(builders.size()) >= 20 &&
        static_cast<int>(turrets.size()) < static_cast<int>(knob("TURRET_BASE", 2)) && myResource >= static_cast<int>(knob("TURRET_RES0", 120))) {
        int bestX = -1, bestY = -1;
        double best = 1e18;
        for (int y = std::max(0, rallyY - 6); y <= std::min(n - 2, rallyY + 6); y++)
            for (int x = std::max(0, rallyX - 6); x <= std::min(n - 2, rallyX + 6); x++) {
                if (!freeSquare(x, y, 2) || badSites.count({x, y})) continue;
                double d = std::abs(x - rallyX) + std::abs(y - rallyY) - (x + y) * 0.15;    // the far side of the rally
                for (int t : turrets) d += std::max(0, 5 - sqDist(x, y, 2, ents[t].x, ents[t].y, 2)) * 2;   // apart
                if (d >= best) continue;
                if (!siteOk(TURRET, x, y, 2, pass)) continue;
                best = d;
                bestX = x;
                bestY = y;
            }
        if (bestX >= 0) {
            sites.push_back(Site{TURRET, bestX, bestY, 2, tick});
            for (int yy = bestY; yy < bestY + 2; yy++)
                for (int xx = bestX; xx < bestX + 2; xx++) reservedCell[cell(xx, yy)] = 1;
            turretsPending++;
            if (DEBUG >= 1) fprintf(stderr, "[t%d] rally turret site (%d,%d)\n", tick, bestX, bestY);
        }
    }
    if (knob("TURRETS", 1) > 0 && turretsPending == 0 && !builders.empty() && builders.size() >= 25 &&
        myResource >= static_cast<int>(knob("TURRET_RES", 100)) && static_cast<int>(turrets.size()) < static_cast<int>(knob("TURRET_MAX", 8))) {
        // the enemy unit nearest to my buildings/builders within 20
        int tx = -1, ty = -1, bestD = 1 << 30;
        for (int idx : enemies) {
            const Ent& e = ents[idx];
            if (!(e.type == RANGED_UNIT || e.type == MELEE_UNIT)) continue;
            for (int b : builders) {
                int d = std::abs(ents[b].x - e.x) + std::abs(ents[b].y - e.y);
                if (d < bestD) { bestD = d; tx = e.x; ty = e.y; }
            }
        }
        if (tx >= 0 && bestD <= static_cast<int>(knob("TURRET_TRIGGER", 14))) {
            // a 2x2 between the threat and my builders' centre, on my side
            double cx = 0, cy = 0;
            for (int b : builders) { cx += ents[b].x; cy += ents[b].y; }
            cx /= builders.size(); cy /= builders.size();
            int bestX = -1, bestY = -1;
            double best = 1e18;
            for (int y = 0; y + 2 <= n; y++)
                for (int x = 0; x + 2 <= n; x++) {
                    if (!freeSquare(x, y, 2)) continue;
                    double dThreat = std::abs(x + 0.5 - tx) + std::abs(y + 0.5 - ty);
                    if (dThreat < 7 || dThreat > 14) continue;
                    if (threatR[cell(x, y)]) continue;
                    double dHome = std::abs(x + 0.5 - cx) + std::abs(y + 0.5 - cy);
                    double score = dThreat + dHome * 0.7;
                    if (score >= best) continue;
                    if (!siteOk(TURRET, x, y, 2, pass)) continue;
                    best = score;
                    bestX = x;
                    bestY = y;
                }
            if (bestX >= 0) {
                sites.push_back(Site{TURRET, bestX, bestY, 2, tick});
                for (int yy = bestY; yy < bestY + 2; yy++)
                    for (int xx = bestX; xx < bestX + 2; xx++) reservedCell[cell(xx, yy)] = 1;
                if (DEBUG >= 1) fprintf(stderr, "[t%d] turret site (%d,%d) vs threat (%d,%d)\n", tick, bestX, bestY, tx, ty);
            }
        }
    }
}

// a builder walks to a given cell along the BFS gradient (dist decreasing toward the cell): its preferences
void Brain::wantToward(int idx, const std::vector<uint8_t>& pass, int priority) {
    const Ent& b = ents[idx];
    int c = cell(b.x, b.y);
    Want w{idx, {}, priority};
    int d0 = dist[c];
    for (int kk = 0; kk < 4; kk++) {
        int nx = b.x + DX[kk], ny = b.y + DY[kk];
        if (!inside(nx, ny)) continue;
        int j = cell(nx, ny);
        if (stamp[j] == gen && dist[j] < d0 && pass[j]) w.prefs.push_back(j);
    }
    for (int kk = 0; kk < 4; kk++) {
        int nx = b.x + DX[kk], ny = b.y + DY[kk];
        if (!inside(nx, ny)) continue;
        int j = cell(nx, ny);
        if (stamp[j] == gen && dist[j] == d0 && pass[j]) w.prefs.push_back(j);
    }
    w.prefs.push_back(-1);
    wants.push_back(w);
}

// For each target cell (in order), the nearest free builder within `limit` walks there (one builder per cell);
// a builder standing on its cell is `arrived`. Returns the number assigned. `pass` is updated: an arrived
// builder's cell becomes a wall for the next searches.
int Brain::assignToCells(const std::vector<int>& cells, int k, int limit, std::vector<uint8_t>& pass,
                         std::unordered_set<int>& busy, int preferId, int priority,
                         const std::function<void(int, int)>& arrived) {
    int taken = 0;
    std::vector<uint8_t> used(cells.size(), 0);
    // 1. builders already standing on one of the cells keep it
    for (size_t i = 0; i < cells.size() && taken < k; i++) {
        int g = grid[cells[i]];
        if (g < 0 || !ents[g].mine || ents[g].type != BUILDER_UNIT) continue;
        if (busy.count(ents[g].id) || handled.count(ents[g].id)) continue;
        used[i] = 1;
        busy.insert(ents[g].id);
        handled.insert(ents[g].id);
        stationary[cells[i]] = 1;
        pass[cells[i]] = 0;
        arrived(g, cells[i]);
        taken++;
    }
    // 2. the rest: per cell, the nearest builder (the preferred one first when it is near)
    for (size_t i = 0; i < cells.size() && taken < k; i++) {
        if (used[i]) continue;
        int c = cells[i];
        if (!pass[c]) continue;                 // someone stands there (not a builder of mine, or busy)
        std::vector<int> src{c};
        bfs(src, pass, limit);
        int best = -1, bestD = 1 << 30;
        for (int idx : builders) {
            const Ent& b = ents[idx];
            if (busy.count(b.id) || handled.count(b.id)) continue;
            int bc = cell(b.x, b.y);
            if (stamp[bc] != gen) continue;
            int d = dist[bc] + (adjResource(b.x, b.y) ? static_cast<int>(knob("MINER_PEN", 3)) : 0);
            if (b.id == preferId) d -= 1000;
            if (d < bestD) { bestD = d; best = idx; }
        }
        if (best < 0) continue;
        used[i] = 1;
        busy.insert(ents[best].id);
        handled.insert(ents[best].id);
        wantToward(best, pass, priority);
        taken++;
    }
    return taken;
}

// builders walk to sites, place them, and repair what stands unfinished
void Brain::assignBuildersToSites(const std::vector<uint8_t>& passIn) {
    reserved = 0;
    bool houseReserved = false, placerDispatched = false;
    std::vector<uint8_t> pass = passIn;
    std::unordered_set<int> busyBuilders;
    int income = estimateIncome();
    int committed = 0;                        // costs of the sites whose placer is on its way or waiting
    int repairBudget = std::max(3, static_cast<int>(builders.size() * knob("REPAIR_SHARE", 0.4)));
    std::stable_sort(sites.begin(), sites.end(), [](const Site& a, const Site& b) {
        int pa = a.type == RANGED_BASE ? 0 : (a.placed ? 1 : 2), pb = b.type == RANGED_BASE ? 0 : (b.placed ? 1 : 2);
        return pa < pb;
    });
    for (Site& s : sites) {
        int cost = P[static_cast<int>(s.type)].cost;
        auto ringOf = [&](bool freeOnly) {
            std::vector<std::pair<int, int>> ring;      // (score, cell): nearer the builders first
            double bcx = 0, bcy = 0;
            for (int b : builders) { bcx += ents[b].x; bcy += ents[b].y; }
            if (!builders.empty()) { bcx /= builders.size(); bcy /= builders.size(); }
            for (int xx = s.x - 1; xx <= s.x + s.size; xx++)
                for (int yy = s.y - 1; yy <= s.y + s.size; yy++) {
                    if (!inside(xx, yy) || sqDist(xx, yy, 1, s.x, s.y, s.size) != 1) continue;
                    int c = cell(xx, yy);
                    int g = grid[c];
                    bool ok = pass[c] || (g >= 0 && ents[g].mine && ents[g].type == BUILDER_UNIT && !stationary[c]);
                    if (freeOnly && !pass[c]) ok = false;
                    if (!ok || reservedCell[c]) continue;
                    if (!reach.empty() && !reach[c] && !(g >= 0)) continue;
                    ring.push_back({static_cast<int>(std::abs(xx - bcx) + std::abs(yy - bcy)), c});
                }
            std::sort(ring.begin(), ring.end());
            std::vector<int> out;
            for (auto& pr : ring) out.push_back(pr.second);
            return out;
        };
        if (!s.placed && footprintResources(s.x, s.y, s.size) > 0) {
            // clear the footprint first: a few builders mine the resources inside from cells next to them
            std::vector<int> cells;
            std::vector<uint8_t> seenCell(static_cast<size_t>(n) * n, 0);
            for (int yy = s.y; yy < s.y + s.size; yy++)
                for (int xx = s.x; xx < s.x + s.size; xx++) {
                    if (!resKnown[cell(xx, yy)]) continue;
                    for (int k = 0; k < 4; k++) {
                        int nx = xx + DX[k], ny = yy + DY[k];
                        if (!inside(nx, ny)) continue;
                        int c = cell(nx, ny);
                        if (seenCell[c] || resKnown[c]) continue;
                        int g = grid[c];
                        bool standable = (g < 0) || (g >= 0 && ents[g].mine && ents[g].type == BUILDER_UNIT && !stationary[c]);
                        if (!standable) continue;
                        if (!reach.empty() && !reach[c] && g < 0) continue;
                        seenCell[c] = 1;
                        cells.push_back(c);
                    }
                }
            assignToCells(cells, static_cast<int>(knob("CLEARERS", 5)), static_cast<int>(knob("SITE_WALK", 40)), pass,
                          busyBuilders, -1, 5, [&](int bIdx, int c) {
                              const Ent& b = ents[bIdx];
                              int rid = -1, bestHp = 1 << 30;
                              for (int kk = 0; kk < 4; kk++) {
                                  int nx = b.x + DX[kk], ny = b.y + DY[kk];
                                  if (!inside(nx, ny) || nx < s.x || nx >= s.x + s.size || ny < s.y || ny >= s.y + s.size) continue;
                                  int g = grid[cell(nx, ny)];
                                  if (g >= 0 && ents[g].type == RESOURCE && ents[g].hp < bestHp) { bestHp = ents[g].hp; rid = ents[g].id; }
                              }
                              (void)c;
                              out[b.id] = rid >= 0 ? actAttack(rid) : actIdle();
                          });
            continue;
        }
        if (!s.placed) {
            // one placer: it walks when the building will be affordable on arrival, or (one site at a time)
            // when the population is about to fill, and waits by the site
            bool popTight = popProvided - popUsed <= static_cast<int>(knob("RESERVE_MARGIN", 0));
            bool popSoon = popProvided - popUsed <= static_cast<int>(knob("DISPATCH_MARGIN", 1));
            if (s.type == RANGED_BASE) reserved += cost;
            std::vector<int> ring = ringOf(false);
            if (DEBUG >= 2 && s.type == RANGED_BASE && tick % 10 == 0) {
                int inside_ = 0;
                for (int yy = s.y; yy < s.y + s.size; yy++)
                    for (int xx = s.x; xx < s.x + s.size; xx++) if (grid[cell(xx, yy)] >= 0) inside_++;
                fprintf(stderr, "[t%d] RB site (%d,%d): ring %zu cells, %d entities inside, res %d committed %d income %d placer %d\n",
                        tick, s.x, s.y, ring.size(), inside_, myResource, committed, income, s.builder);
            }
            if (ring.empty()) continue;
            // is a builder already there?
            int there = -1;
            for (int c : ring) {
                int g = grid[c];
                if (g >= 0 && ents[g].mine && ents[g].type == BUILDER_UNIT && !busyBuilders.count(ents[g].id) && !handled.count(ents[g].id)) { there = g; break; }
            }
            bool soon = myResource - committed + income * 4 >= cost || (s.type == HOUSE && popTight);
            if (there >= 0 && soon) {
                const Ent& b = ents[there];
                int c = cell(b.x, b.y);
                busyBuilders.insert(b.id);
                handled.insert(b.id);
                stationary[c] = 1;
                pass[c] = 0;
                s.builder = b.id;
                out[b.id] = actBuild(s.type, s.x, s.y);
                committed += cost;
                if (s.type == TURRET || (s.type == HOUSE && popTight && !houseReserved)) {
                    reserved += cost;
                    houseReserved = true;
                }
                continue;
            }
            // dispatch: how far is the nearest builder?
            std::vector<int> src;
            for (int c : ring) if (pass[c]) src.push_back(c);
            if (src.empty()) continue;
            bfs(src, pass, static_cast<int>(knob("SITE_WALK", 40)));
            int best = -1, bestD = 1 << 30;
            for (int idx : builders) {
                const Ent& b = ents[idx];
                if (busyBuilders.count(b.id) || handled.count(b.id)) continue;
                int bc = cell(b.x, b.y);
                if (stamp[bc] != gen) continue;
                int d = dist[bc] + (adjResource(b.x, b.y) ? static_cast<int>(knob("MINER_PEN", 3)) : 0);
                if (b.id == s.builder) d -= 1000;
                if (d < bestD) { bestD = d; best = idx; }
            }
            if (best < 0) continue;
            int d = std::max(0, dist[cell(ents[best].x, ents[best].y)]);
            bool affordable = myResource - committed + income * std::max(0, d - 1) >= cost;
            bool early = s.type == HOUSE && popSoon && !placerDispatched && committed == 0 && d <= static_cast<int>(knob("EARLY_WALK", 14));
            if (!affordable && !early) continue;
            placerDispatched = true;
            committed += cost;
            s.builder = ents[best].id;
            busyBuilders.insert(ents[best].id);
            handled.insert(ents[best].id);
            wantToward(best, pass, 5);
            if (d <= 2 && (s.type == TURRET || (s.type == HOUSE && popTight && !houseReserved))) {
                reserved += cost;
                houseReserved = true;
            }
            continue;
        }
        // placed: repairers, each with its own ring cell
        int hpLeft = 0;
        for (int idx : own) if (ents[idx].id == s.placedId) hpLeft = ents[idx].maxHp - ents[idx].hp;
        int k = s.type == RANGED_BASE || s.type == BUILDER_BASE || s.type == MELEE_BASE
                    ? static_cast<int>(knob("RB_REPAIRERS", 7))
                    : (s.type == TURRET ? static_cast<int>(knob("TURRET_REPAIRERS", 4)) : static_cast<int>(knob("HOUSE_REPAIRERS", 3)));
        if (hpLeft <= 6) k = std::min(k, 2);
        if (static_cast<int>(builders.size()) < 4) k = std::min(k, 2);
        else if (static_cast<int>(builders.size()) < 12) k = std::min(k, 3);
        k = std::min(k, repairBudget);
        if (k <= 0) continue;
        std::vector<int> ring = ringOf(false);
        int placedId = s.placedId;
        repairBudget -= assignToCells(ring, k, static_cast<int>(knob("REPAIR_WALK", 25)), pass, busyBuilders, -1, 5,
                                      [&](int bIdx, int) { out[ents[bIdx].id] = actRepair(placedId); });
    }
}

// damaged active buildings and units near home get repaired when no enemy shoots at them
void Brain::repairDamaged(const std::vector<uint8_t>& passIn) {
    std::vector<uint8_t> pass = passIn;
    std::unordered_set<int> busy;
    for (int idx : own) {
        const Ent& e = ents[idx];
        if (!e.active || e.hp >= e.maxHp) continue;
        if (e.type == BUILDER_UNIT || e.type == WALL) continue;
        bool pendingSite = false;
        for (const Site& s : sites) if (s.placedId == e.id) pendingSite = true;
        if (pendingSite) continue;
        if (threatR[cell(e.x, e.y)] && e.type != TURRET) continue;
        int k = e.type == TURRET ? 4 : (e.maxHp - e.hp > 40 ? 3 : 1);
        if (P[static_cast<int>(e.type)].canMove) k = 1;
        std::vector<int> ring;
        for (int xx = e.x - 1; xx <= e.x + e.size; xx++)
            for (int yy = e.y - 1; yy <= e.y + e.size; yy++) {
                if (!inside(xx, yy) || sqDist(xx, yy, 1, e.x, e.y, e.size) != 1) continue;
                int c = cell(xx, yy);
                int g = grid[c];
                if (pass[c] || (g >= 0 && ents[g].mine && ents[g].type == BUILDER_UNIT && !stationary[c])) ring.push_back(c);
            }
        int id = e.id;
        assignToCells(ring, k, static_cast<int>(knob("REPAIR_WALK", 25)), pass, busy, -1, 5,
                      [&](int bIdx, int) { out[ents[bIdx].id] = actRepair(id); });
    }
}

void Brain::flee(int idx, const std::vector<uint8_t>& pass) {
    const Ent& b = ents[idx];
    int c = cell(b.x, b.y);
    Want w{idx, {}, 9};
    std::vector<std::pair<int, int>> opts;
    for (int k = 0; k < 4; k++) {
        int nx = b.x + DX[k], ny = b.y + DY[k];
        if (!inside(nx, ny)) continue;
        int j = cell(nx, ny);
        if (!pass[j]) continue;
        // lower danger first, then toward home (small x+y)
        int score = builderDanger[j] * 100 + threatR1[j] * 300 + (nx + ny);
        opts.push_back({score, j});
    }
    std::sort(opts.begin(), opts.end());
    int here = builderDanger[c] * 100 + threatR1[c] * 300 + (b.x + b.y);
    for (const auto& o : opts)
        if (o.first < here) w.prefs.push_back(o.second);
    w.prefs.push_back(-1);
    wants.push_back(w);
    handled.insert(b.id);
}

// mining: each builder stands on its own slot (a free cell next to a known resource) and names the resource
void Brain::mining(const std::vector<uint8_t>& pass) {
    const size_t cells = static_cast<size_t>(n) * n;
    // slot cells: passable (or an own builder stands there), next to a resource, not reserved, not in range
    std::vector<uint8_t> slot(cells, 0), slotSafe(cells, 0);
    for (size_t c = 0; c < cells; c++) {
        int g = grid[c];
        bool standable = pass[c] || (g >= 0 && ents[g].mine && ents[g].type == BUILDER_UNIT && !stationary[c]);
        if (!standable || reservedCell[c]) continue;
        int x = static_cast<int>(c) % n, y = static_cast<int>(c) / n;
        if (!adjResource(x, y)) continue;
        slot[c] = 1;
        slotSafe[c] = (builderDanger[c] == 0) ? 1 : 0;
    }
    slotTaken.assign(cells, -1);
    std::unordered_set<int> targeted;         // resource entities already mined by a builder this tick
    // 1. builders already on a slot keep it (stability), unless in danger
    std::vector<int> rest;
    for (int idx : builders) {
        const Ent& b = ents[idx];
        if (handled.count(b.id)) continue;
        int c = cell(b.x, b.y);
        if (slot[c] && builderDanger[c] == 0 && slotTaken[c] < 0) {
            // keep: pick the adjacent resource with the lowest hp
            int bestRes = -1, bestHp = 1 << 30;
            for (int k = 0; k < 4; k++) {
                int nx = b.x + DX[k], ny = b.y + DY[k];
                if (!inside(nx, ny)) continue;
                int g = grid[cell(nx, ny)];
                if (g >= 0 && ents[g].type == RESOURCE) {
                    int resNb = 0;
                    for (int kk = 0; kk < 4; kk++) {
                        int ax = nx + DX[kk], ay = ny + DY[kk];
                        if (inside(ax, ay) && resKnown[cell(ax, ay)]) resNb++;
                    }
                    // a cell deep in the mass opens a new lane; a weak one is finished first; one another
                    // builder already mines is left to it when there is a choice
                    int score = ents[g].hp + (3 - resNb) * static_cast<int>(knob("DIG_W", -12)) +
                                (targeted.count(g) ? static_cast<int>(knob("SHARE_PEN", 40)) : 0);
                    if (score < bestHp) { bestHp = score; bestRes = g; }
                }
            }
            targeted.insert(bestRes);
            slotTaken[c] = b.id;
            slotOf[b.id] = c;
            stationary[c] = 1;
            handled.insert(b.id);
            out[b.id] = actAttack(ents[bestRes].id);
        } else {
            rest.push_back(idx);
        }
    }
    // resource mass around a slot: resources within 3 cells (a slot by a big field beats a dead-end strip)
    std::vector<int> mass(cells, 0);
    {
        std::vector<int> rowsum(cells, 0);
        for (int y = 0; y < n; y++) {
            int acc = 0;
            for (int x = 0; x < n; x++) {
                acc += resKnown[cell(x, y)];
                if (x - 7 >= 0) acc -= resKnown[cell(x - 7, y)];
                int cx = x - 3;
                if (cx >= 0) rowsum[cell(cx, y)] = acc;
            }
            for (int x = n - 3; x < n; x++) {
                int v = 0;
                for (int xx = std::max(0, x - 3); xx <= std::min(n - 1, x + 3); xx++) v += resKnown[cell(xx, y)];
                rowsum[cell(x, y)] = v;
            }
        }
        for (int x = 0; x < n; x++) {
            int acc = 0;
            for (int y = 0; y < n; y++) {
                acc += rowsum[cell(x, y)];
                if (y - 7 >= 0) acc -= rowsum[cell(x, y - 7)];
                int cy = y - 3;
                if (cy >= 0) mass[cell(x, cy)] = acc;
            }
            for (int y = n - 3; y < n; y++) {
                int v = 0;
                for (int yy = std::max(0, y - 3); yy <= std::min(n - 1, y + 3); yy++) v += rowsum[cell(x, yy)];
                mass[cell(x, y)] = v;
            }
        }
        int wMass = static_cast<int>(knob("MASS_W", 0));
        for (size_t c = 0; c < cells; c++) mass[c] = std::min(mass[c], 24) * wMass;
    }
    // 2. the others: the nearest free slot (safe ones first), sticky to the previous choice when close
    const std::vector<uint8_t> pass2 = passFor(true, false);     // the miners just fixed are walls now
    for (size_t c = 0; c < cells; c++) if (slot[c] && stationary[c]) slot[c] = 0;
    std::vector<uint8_t> goal(cells, 0);
    auto rebuildGoal = [&](bool safeOnly) {
        for (size_t c = 0; c < cells; c++) goal[c] = (slot[c] && slotTaken[c] < 0 && (!safeOnly || slotSafe[c])) ? 1 : 0;
    };
    std::sort(rest.begin(), rest.end(), [&](int a, int b) { return ents[a].id < ents[b].id; });
    int maxWalk = static_cast<int>(knob("MINE_WALK", 50));
    // sticky: every builder keeps the slot it was walking to, claimed before anyone searches
    std::unordered_map<int, int> sticky;
    for (int idx : rest) {
        const Ent& b = ents[idx];
        auto it = slotOf.find(b.id);
        if (it == slotOf.end()) continue;
        int prev = it->second;
        if (slot[prev] && slotTaken[prev] < 0 && slotSafe[prev]) {
            slotTaken[prev] = b.id;
            sticky[b.id] = prev;
        }
    }
    std::vector<uint8_t> g1(cells, 0);
    // every builder's nearest free slot; then the shortest walks are granted first, and a builder whose slot
    // was taken by a nearer one searches again
    struct Cand { int idx, target, step, d; };
    std::vector<Cand> cands;
    auto search = [&](int idx) {
        const Ent& b = ents[idx];
        int c = cell(b.x, b.y);
        int step = -1, target = -1;
        auto st = sticky.find(b.id);
        if (st != sticky.end()) {
            int prev = st->second;
            g1[prev] = 1;
            int t = bfsNearest(c, pass2, g1, maxWalk, step);
            g1[prev] = 0;
            if (t >= 0) return Cand{idx, t, step, dist[t]};
            slotTaken[prev] = -1;          // unreachable now: release it
            sticky.erase(st);
        }
        int extra = static_cast<int>(knob("SLOT_EXTRA", 4));
        auto av = avoidSlot.find(b.id);
        int avoid = av != avoidSlot.end() ? av->second : -1;
        rebuildGoal(true);
        if (avoid >= 0) goal[avoid] = 0;
        target = bfsBest(c, pass2, goal, mass, maxWalk, extra, step);
        if (target < 0) {
            rebuildGoal(false);
            if (avoid >= 0) goal[avoid] = 0;
            target = bfsBest(c, pass2, goal, mass, maxWalk, extra, step);
        }
        if (avoid >= 0 && target >= 0) avoidSlot.erase(b.id);
        return Cand{idx, target, step, target >= 0 ? dist[target] : (1 << 20)};
    };
    for (int idx : rest) cands.push_back(search(idx));
    std::vector<uint8_t> done(ents.size(), 0);
    for (int round = 0; round < 3 && !cands.empty(); round++) {
        std::sort(cands.begin(), cands.end(), [](const Cand& a, const Cand& b) { return a.d < b.d; });
        std::vector<Cand> again;
        for (const Cand& cd : cands) {
            const Ent& b = ents[cd.idx];
            int c = cell(b.x, b.y);
            if (cd.target < 0) {
                done[cd.idx] = 0;
                again.push_back(cd);
                continue;
            }
            bool isSticky = sticky.count(b.id) && sticky[b.id] == cd.target;
            if (!isSticky && slotTaken[cd.target] >= 0 && slotTaken[cd.target] != b.id) {
                // taken meanwhile by a nearer builder: search again next round
                again.push_back(search(cd.idx));
                continue;
            }
            slotTaken[cd.target] = b.id;
            slotOf[b.id] = cd.target;
            handled.insert(b.id);
            done[cd.idx] = 1;
            if (cd.target == c) {
                stationary[c] = 1;
                out[b.id] = actIdle();
                continue;
            }
            Want w{cd.idx, {}, 3};
            if (cd.step >= 0) w.prefs.push_back(cd.step);
            w.prefs.push_back(-1);
            wants.push_back(w);
        }
        cands.clear();
        for (const Cand& cd : again)
            if (!done[cd.idx]) cands.push_back(cd);
        // only the re-searched ones go another round; the hopeless ones fall through
        bool anyTarget = false;
        for (const Cand& cd : cands) if (cd.target >= 0) anyTarget = true;
        if (!anyTarget) break;
    }
    for (const Cand& cd : cands) {
        if (done[cd.idx]) continue;
        const Ent& b = ents[cd.idx];
        int c = cell(b.x, b.y);
        int step = -1;
        slotOf.erase(b.id);
        // nothing reachable: dig toward the nearest believed resource through resources
        std::vector<uint8_t> passDig = passFor(true, true);
        std::vector<uint8_t> g2(cells, 0);
        for (size_t cc = 0; cc < cells; cc++) g2[cc] = resKnown[cc];
        int t2 = bfsNearest(c, passDig, g2, 80, step);
        handled.insert(b.id);
        if (t2 >= 0 && step >= 0) {
            int g = grid[step];
            if (g >= 0 && ents[g].type == RESOURCE) {
                stationary[c] = 1;
                out[b.id] = actAttack(ents[g].id);
            } else if (resKnown[step]) {
                Want w{cd.idx, {step, -1}, 3};        // a remembered resource: step there (it mines it, or finds it gone)
                wants.push_back(w);
            } else {
                Want w{cd.idx, {step, -1}, 3};
                wants.push_back(w);
            }
        } else {
            // wander toward the centre to discover resources
            Want w{cd.idx, {}, 1};
            std::vector<std::pair<int, int>> opts;
            for (int k = 0; k < 4; k++) {
                int nx = b.x + DX[k], ny = b.y + DY[k];
                if (inside(nx, ny) && pass2[cell(nx, ny)]) opts.push_back({-(nx + ny), cell(nx, ny)});
            }
            std::sort(opts.begin(), opts.end());
            for (auto& o : opts) w.prefs.push_back(o.second);
            w.prefs.push_back(-1);
            wants.push_back(w);
        }
    }
}

void Brain::produce() {
    int nBuilders = static_cast<int>(builders.size());
    int nRanged = static_cast<int>(rangedU.size());
    int nMelee = static_cast<int>(meleeU.size());
    int freePop = popProvided - popUsed;
    int budget = myResource - reserved;
    int income = estimateIncome();
    // how many slots are there for more builders, within a walk of the base?
    int freeSlots = 0;
    {
        std::vector<uint8_t> passB = passFor(true, false);
        std::vector<int> src;
        for (int bi : bbases) {
            const Ent& b = ents[bi];
            for (int xx = b.x - 1; xx <= b.x + b.size; xx++)
                for (int yy = b.y - 1; yy <= b.y + b.size; yy++)
                    if (inside(xx, yy) && sqDist(xx, yy, 1, b.x, b.y, b.size) == 1 && passB[cell(xx, yy)]) src.push_back(cell(xx, yy));
        }
        if (!src.empty()) bfs(src, passB, static_cast<int>(knob("SLOT_REACH", 60)));
        for (size_t c = 0; c < slotTaken.size(); c++)
            if (slotTaken[c] < 0 && (src.empty() || stamp[c] == gen)) {
                int g = grid[c];
                bool standable = g < 0 || (ents[g].mine && ents[g].type == BUILDER_UNIT);
                if (!standable || reservedCell[c] || builderDanger[c] || stationary[c] || resKnown[c]) continue;
                int x = static_cast<int>(c) % n, y = static_cast<int>(c) / n;
                bool adj = adjResource(x, y);
                if (!adj && fog)
                    for (int k = 0; k < 4 && !adj; k++) {
                        int nx = x + DX[k], ny = y + DY[k];
                        if (inside(nx, ny) && !seen[cell(nx, ny)] && lastState[cell(nx, ny)] < 0 && believedResource(cell(nx, ny))) adj = true;
                    }
                if (adj) freeSlots++;
            }
    }
    int builderCap = static_cast<int>(knob("BUILDER_CAP", 80));
    if (nPlayers > 2) builderCap = static_cast<int>(knob("BUILDER_CAP4", 60));
    int lateTick = static_cast<int>(knob("BUILDER_STOP_TICK", 800));
    bool wantBuilder = nBuilders < builderCap && freeSlots >= static_cast<int>(knob("SLOTS_MIN", 2)) && tick < lateTick;
    // under attack at home the builder base keeps producing (they are the economy), ranged first though
    bool threatened = false;
    for (int idx : enemies) {
        const Ent& e = ents[idx];
        if ((e.type == RANGED_UNIT || e.type == MELEE_UNIT) && e.x + e.y < static_cast<int>(knob("HOME_RADIUS", 40))) threatened = true;
    }
    // ranged before builders when threatened or when the builder count is comfortable
    bool rangedFirst = threatened || nBuilders >= nRanged * knob("BR_RATIO", 1.4) + knob("BR_OFFSET", 8) ||
                       enemyEstimate > nRanged + nMelee;
    auto spawnCell = [&](const Ent& base, const std::vector<int>& field, bool fieldValid, bool towardEnemy) {
        int best = -1;
        long long bestScore = 1LL << 60;
        for (int xx = base.x - 1; xx <= base.x + base.size; xx++)
            for (int yy = base.y - 1; yy <= base.y + base.size; yy++) {
                if (!inside(xx, yy) || sqDist(xx, yy, 1, base.x, base.y, base.size) != 1) continue;
                int c = cell(xx, yy);
                if (grid[c] >= 0 || reservedCell[c]) continue;
                long long score;
                if (fieldValid && stamp[c] == gen) score = field[c] * 4;
                else score = towardEnemy ? -(xx + yy) : (xx + yy);
                score = score * 4 + (threatR1[c] ? 1000 : 0);
                if (score < bestScore) { bestScore = score; best = c; }
            }
        return best;
    };
    auto tryBuild = [&](const std::vector<int>& bases, EntityType ut, bool towardEnemy, bool useField) {
        if (bases.empty() || freePop < P[static_cast<int>(ut)].popUse) return false;
        int count = ut == BUILDER_UNIT ? nBuilders : (ut == RANGED_UNIT ? nRanged : nMelee);
        int cost = P[static_cast<int>(ut)].cost + count;
        if (budget < cost) return false;
        for (int bi : bases) {
            const Ent& base = ents[bi];
            if (!base.active) continue;
            int c = spawnCell(base, dist, useField, towardEnemy);
            if (c < 0) continue;
            out[base.id] = actBuild(ut, c % n, c / n);
            budget -= cost;
            freePop -= P[static_cast<int>(ut)].popUse;
            if (ut == BUILDER_UNIT) nBuilders++; else if (ut == RANGED_UNIT) nRanged++; else nMelee++;
            return true;
        }
        return false;
    };
    // a field toward free mining slots for the builder spawn side
    std::vector<int> src;
    for (size_t c = 0; c < slotTaken.size(); c++)
        if (slotTaken[c] < 0 && grid[c] < 0 && !reservedCell[c] && !resKnown[c] &&
            adjResource(static_cast<int>(c) % n, static_cast<int>(c) / n))
            src.push_back(static_cast<int>(c));
    bool fieldValid = false;
    if (!src.empty()) {
        std::vector<uint8_t> pass = passFor(true, false);
        bfs(src, pass);
        fieldValid = true;
    }
    int meleeShare = static_cast<int>(knob("MELEE_SHARE", 0));      // melee per 10 ranged (round 1)
    auto army = [&]() {
        bool did = false;
        if (!rbases.empty()) did = tryBuild(rbases, RANGED_UNIT, true, false) || did;
        if (!mbases.empty() && meleeShare > 0 && nMelee * 10 < nRanged * meleeShare + 10) did = tryBuild(mbases, MELEE_UNIT, true, false) || did;
        return did;
    };
    if (rangedFirst) {
        army();
        if (wantBuilder) tryBuild(bbases, BUILDER_UNIT, false, fieldValid);
    } else {
        if (wantBuilder) tryBuild(bbases, BUILDER_UNIT, false, fieldValid);
        army();
    }
    // late game: spend everything on units (build score), houses handled by planSites
    (void)income;
}

void Brain::economy() {
    std::vector<uint8_t> pass = passFor(true, false);
    // 1. builders in danger flee
    for (int idx : builders) {
        const Ent& b = ents[idx];
        int c = cell(b.x, b.y);
        if (builderDanger[c] > 0 || threatM[c] > 0) flee(idx, pass);
    }
    // 2. sites
    planSites();
    evictFromSites();
    pass = passFor(true, false);
    assignBuildersToSites(pass);
    pass = passFor(true, false);
    repairDamaged(pass);
    pass = passFor(true, false);
    // 3. mining
    mining(pass);
    // 4. production
    produce();
}

// ------------------------------------------------------------------------------------------ military
void Brain::military() {
    std::vector<int> army;
    for (int idx : rangedU) army.push_back(idx);
    for (int idx : meleeU) army.push_back(idx);
    std::vector<int> shooters = army;
    for (int idx : turrets) if (ents[idx].active) shooters.push_back(idx);

    // ---- 1. targets in range: a kill-first assignment
    struct Tgt { int idx; int hpLeft; int prio; std::vector<int> shooters; };
    std::vector<Tgt> tgts;
    std::unordered_map<int, int> tgtIndex;   // ents idx -> tgts index
    std::vector<std::vector<int>> inRange(ents.size());     // shooter idx -> target idxs
    for (int sh : shooters) {
        const Ent& u = ents[sh];
        if (!u.active) continue;
        int range = P[static_cast<int>(u.type)].range;
        for (int e : enemies) {
            const Ent& t = ents[e];
            if (sqDist(u.x, u.y, u.size, t.x, t.y, t.size) <= range) {
                inRange[sh].push_back(e);
                auto it = tgtIndex.find(e);
                if (it == tgtIndex.end()) {
                    int prio;
                    switch (t.type) {
                        case RANGED_UNIT: prio = 100; break;
                        case MELEE_UNIT: prio = 90; break;
                        case TURRET: prio = t.active ? 85 : 40; break;
                        case BUILDER_UNIT: prio = 70; break;
                        case BUILDER_BASE: case RANGED_BASE: case MELEE_BASE: prio = 60; break;
                        case HOUSE: prio = 30; break;
                        default: prio = 10; break;
                    }
                    tgtIndex[e] = static_cast<int>(tgts.size());
                    tgts.push_back(Tgt{e, t.hp, prio, {}});
                }
                tgts[tgtIndex[e]].shooters.push_back(sh);
            }
        }
    }
    std::unordered_set<int> assigned;      // shooter ents idx
    std::vector<int> order(tgts.size());
    for (size_t i = 0; i < order.size(); i++) order[i] = static_cast<int>(i);
    std::sort(order.begin(), order.end(), [&](int a, int b) {
        if (tgts[a].prio != tgts[b].prio) return tgts[a].prio > tgts[b].prio;
        return tgts[a].hpLeft < tgts[b].hpLeft;
    });
    auto shoot = [&](int sh, int target) {
        assigned.insert(sh);
        handled.insert(ents[sh].id);
        out[ents[sh].id] = actAttack(ents[target].id);
        stationary[cell(ents[sh].x, ents[sh].y)] = 1;
        tgts[tgtIndex[target]].hpLeft -= P[static_cast<int>(ents[sh].type)].damage;
    };
    // pass 1: kills, the most valuable first
    for (int ti : order) {
        Tgt& t = tgts[ti];
        int dmgAvail = 0;
        std::vector<int> avail;
        for (int sh : t.shooters) if (!assigned.count(sh)) { avail.push_back(sh); dmgAvail += P[static_cast<int>(ents[sh].type)].damage; }
        if (dmgAvail < t.hpLeft) continue;
        std::sort(avail.begin(), avail.end(), [&](int a, int b) { return inRange[a].size() < inRange[b].size(); });
        for (int sh : avail) {
            if (t.hpLeft <= 0) break;
            shoot(sh, t.idx);
        }
    }
    // pass 2: the rest shoot the best target in range (highest prio, then lowest hp)
    for (int sh : shooters) {
        if (assigned.count(sh) || inRange[sh].empty()) continue;
        int best = -1, bestKey = -1;
        for (int e : inRange[sh]) {
            Tgt& t = tgts[tgtIndex[e]];
            int key = t.prio * 1000 - std::max(t.hpLeft, 0);
            if (key > bestKey) { bestKey = key; best = e; }
        }
        shoot(sh, best);
    }

    // ---- 2. the enemy's military, seen now or remembered; its strength over a window
    struct Foe { int x, y, hp; EntityType type; bool visible; };
    std::vector<Foe> foes;
    std::unordered_set<int> visibleIds;
    for (int e : enemies) {
        const Ent& t = ents[e];
        visibleIds.insert(t.id);
        if (t.type == RANGED_UNIT || t.type == MELEE_UNIT || (t.type == TURRET && t.active)) foes.push_back({t.x, t.y, t.hp, t.type, true});
    }
    int visibleMil = static_cast<int>(foes.size());
    for (const auto& kv : enemyUnitsMem)
        if (!visibleIds.count(kv.first) && (kv.second.type == RANGED_UNIT || kv.second.type == MELEE_UNIT))
            foes.push_back({kv.second.x, kv.second.y, kv.second.hp, kv.second.type, false});
    int distinctRecent = 0;
    for (const auto& kv : enemyUnitsMem)
        if ((kv.second.type == RANGED_UNIT || kv.second.type == MELEE_UNIT) && tick - kv.second.lastSeen <= static_cast<int>(knob("EST_WINDOW", 40))) distinctRecent++;
    enemyHist.push_back(std::max(visibleMil, distinctRecent));
    if (static_cast<int>(enemyHist.size()) > static_cast<int>(knob("EST_WINDOW", 40))) enemyHist.erase(enemyHist.begin());
    int enemyEst = 0;
    for (int v : enemyHist) enemyEst = std::max(enemyEst, v);
    // what we have seen of them does not vanish: a slowly fading memory of their peak strength
    enemyPeak = std::max(enemyPeak * knob("PEAK_DECAY", 0.997), static_cast<double>(enemyEst));
    enemyEst = std::max(enemyEst, static_cast<int>(enemyPeak * knob("PEAK_SHARE", 0.8)));
    enemyEstimate = enemyEst;
    // the scores say who is ahead: attacking from behind is how armies are lost
    int enemyScore = 0;
    for (const Player& p : view_->players)
        if (p.id != me) enemyScore = std::max(enemyScore, p.score);
    bool economyOk = myScore >= enemyScore * knob("ATTACK_SCORE_RATIO", 0.95);
    // an unseen enemy whose score matches mine is assumed to field a real army (up to a cap: a huge army of
    // mine must not wait forever for a sighting)
    if (enemyScore >= myScore * knob("PEER_SCORE", 0.9))
        enemyEst = std::max(enemyEst, std::min(static_cast<int>(army.size()), static_cast<int>(knob("PEER_CAP", 55))));
    enemyEstimate = enemyEst;

    int nArmy = static_cast<int>(army.size());
    if (nArmy == 0) return;

    // ---- 3. the mode: defend what is threatened; attack when clearly stronger; else hold at the rally
    int tx = 0, ty = 0, threatAtHome = 0;
    for (const Foe& f : foes) {
        int dmin = 1 << 30;
        for (int idx : own) {
            const Ent& o = ents[idx];
            if (o.type == RANGED_UNIT || o.type == MELEE_UNIT) continue;
            dmin = std::min(dmin, sqDist(f.x, f.y, 1, o.x, o.y, o.size));
        }
        if (dmin <= static_cast<int>(knob("DEFEND_DIST", 12))) {
            threatAtHome++;
            tx += f.x;
            ty += f.y;
        }
    }
    int attackMin = static_cast<int>(knob("ATTACK_MIN", 20));
    int retreatMin = static_cast<int>(knob("RETREAT_MIN", 8));
    int attackTick = static_cast<int>(knob("ATTACK_TICK", 700));
    double attackRatio = knob("ATTACK_RATIO", 1.3);
    bool strong = nArmy >= std::max(attackMin, static_cast<int>(enemyEst * attackRatio + 3));
    bool sighted = lastEnemySeenTick >= 0 && enemyEst > 0;
    bool blindOk = tick >= static_cast<int>(knob("ATTACK_BLIND_TICK", 450));
    int seenEst0 = 0;
    for (int v : enemyHist) seenEst0 = std::max(seenEst0, v);
    seenEst0 = std::max(seenEst0, static_cast<int>(enemyPeak * knob("PEAK_SHARE", 0.8)));
    bool lateBig = tick >= static_cast<int>(knob("ATTACK_BIG_TICK", 450)) &&
                   nArmy >= std::max(static_cast<int>(knob("ATTACK_BIG", 45)), static_cast<int>(seenEst0 * attackRatio + 3));
    if (!attacking && ((strong && (sighted || blindOk) && (economyOk || lateBig)) || lateBig ||
                       (tick >= attackTick && nArmy >= std::max(retreatMin, static_cast<int>(seenEst0 * 1.1))))) {
        attacking = true;
        attackSince = tick;
        if (DEBUG >= 1) fprintf(stderr, "[t%d] ATTACK: army %d vs est %d (score %d vs %d)\n", tick, nArmy, enemyEst, myScore, enemyScore);
    }
    int seenEst = 0;
    for (int v : enemyHist) seenEst = std::max(seenEst, v);
    seenEst = std::max(seenEst, static_cast<int>(enemyPeak * knob("PEAK_SHARE", 0.8)));
    if (attacking && (nArmy < retreatMin || nArmy < seenEst * knob("ATTACK_STOP", 1.05))) {
        attacking = false;
        if (DEBUG >= 1) fprintf(stderr, "[t%d] attack off: army %d vs est %d\n", tick, nArmy, enemyEst);
    }
    enum Mode { HOLD, DEFEND, ATTACK };
    Mode mode = threatAtHome > 0 ? DEFEND : (attacking ? ATTACK : HOLD);

    // the rally: beyond my buildings toward the map's centre
    {
        double sx = 0, sy = 0;
        int k = 0;
        for (int idx : own) {
            const Ent& e = ents[idx];
            if (P[static_cast<int>(e.type)].canMove || e.type == WALL) continue;
            sx += e.x + e.size / 2.0;
            sy += e.y + e.size / 2.0;
            k++;
        }
        if (k == 0) { sx = 7; sy = 7; k = 1; }
        // the economy's centre: where the builders are; the army waits a little beyond it toward the enemy
        double bx = 0, by = 0;
        if (!builders.empty()) {
            for (int idx : builders) { bx += ents[idx].x; by += ents[idx].y; }
            bx /= builders.size(); by /= builders.size();
        } else { bx = sx / k; by = sy / k; }
        int ahead = static_cast<int>(knob("RALLY_AHEAD", 6));
        rallyX = std::clamp(static_cast<int>(bx) + ahead, static_cast<int>(knob("RALLY", 14)), static_cast<int>(knob("RALLY_MAX", 40)));
        rallyY = std::clamp(static_cast<int>(by) + ahead, static_cast<int>(knob("RALLY", 14)), static_cast<int>(knob("RALLY_MAX", 40)));
    }
    // the goal (in HOLD: the most open cell near the rally); goalSize > 1: a building, aim at its ring
    int goalX = rallyX, goalY = rallyY, goalSize = 1;
    if (mode == HOLD) {
        int best = -1;
        for (int y = std::max(0, rallyY - 5); y <= std::min(n - 1, rallyY + 5); y++)
            for (int x = std::max(0, rallyX - 5); x <= std::min(n - 1, rallyX + 5); x++) {
                int c = cell(x, y);
                if (grid[c] >= 0 && !(ents[grid[c]].mine && P[static_cast<int>(ents[grid[c]].type)].canMove)) continue;
                if (resKnown[c] || reservedCell[c]) continue;
                int open = 0;
                for (int yy = std::max(0, y - 3); yy <= std::min(n - 1, y + 3); yy++)
                    for (int xx = std::max(0, x - 3); xx <= std::min(n - 1, x + 3); xx++) {
                        if (std::abs(xx - x) + std::abs(yy - y) > 3) continue;
                        int cc = cell(xx, yy);
                        if ((grid[cc] < 0 && !resKnown[cc] && !reservedCell[cc]) || (grid[cc] >= 0 && ents[grid[cc]].mine && P[static_cast<int>(ents[grid[cc]].type)].canMove)) open++;
                    }
                int score = open * 4 - (std::abs(x - rallyX) + std::abs(y - rallyY));
                if (score > best) { best = score; goalX = x; goalY = y; }
            }
    }
    if (mode == DEFEND) {
        goalX = tx / threatAtHome;
        goalY = ty / threatAtHome;
    } else if (mode == ATTACK) {
        double cx = frontX >= 0 ? frontX : 0, cy = frontY >= 0 ? frontY : 0;
        long long best = 1LL << 60;
        int gx = -1, gy = -1;
        // 1. what is in sight: units first (they run), then buildings; the nearest to the front
        for (int e : enemies) {
            const Ent& t = ents[e];
            long long d = static_cast<long long>(std::abs(t.x - cx) + std::abs(t.y - cy));
            if (P[static_cast<int>(t.type)].canMove) d -= 6;
            if (d < best) { best = d; gx = t.x; gy = t.y; goalSize = t.size; }
        }
        // 2. remembered buildings, bases first
        if (gx < 0)
            for (const auto& kv : enemyBuildings) {
                const Remembered& r = kv.second;
                long long d = static_cast<long long>(std::abs(r.x - cx) + std::abs(r.y - cy));
                if (r.type == BUILDER_BASE || r.type == RANGED_BASE || r.type == MELEE_BASE) d -= 10;
                if (d < best) { best = d; gx = r.x; gy = r.y; goalSize = r.size; }
            }
        // 3. the enemy bases where the map's symmetry puts them, each once
        if (gx < 0)
            for (int seat = 0; seat < nPlayers; seat++) {
                if (seat == mySeat || baseGuessVisited[seat]) continue;
                long long d = static_cast<long long>(std::abs(enemyBaseX[seat] + 2 - cx) + std::abs(enemyBaseY[seat] + 2 - cy));
                if (d < best) { best = d; gx = enemyBaseX[seat]; gy = enemyBaseY[seat]; goalSize = 5; }
            }
        // 4. the last enemy unit seen
        if (gx < 0) {
            int latest = -1;
            for (const auto& kv : enemyUnitsMem)
                if (kv.second.lastSeen > latest) { latest = kv.second.lastSeen; gx = kv.second.x; gy = kv.second.y; goalSize = 1; }
        }
        // 5. sweep: the cell farthest from anything of mine that we have not seen lately
        if (gx < 0) {
            long long bestAge = -1;
            for (int y = 2; y < n - 2; y += 4)
                for (int x = 2; x < n - 2; x += 4) {
                    int c = cell(x, y);
                    long long age = lastTick[c] < 0 ? 100000 : tick - lastTick[c];
                    if (resKnown[c] || (fog && !seen[c] && believedResource(c))) continue;
                    long long key = age * 4 - (std::abs(x - static_cast<int>(cx)) + std::abs(y - static_cast<int>(cy)));
                    if (key > bestAge) { bestAge = key; gx = x; gy = y; goalSize = 1; }
                }
        }
        goalX = gx < 0 ? rallyX : gx;
        goalY = gy < 0 ? rallyY : gy;
    }
    // a base guess is visited once the front stands by it and nothing stands there
    for (int seat = 0; seat < nPlayers; seat++) {
        if (seat == mySeat || baseGuessVisited[seat] || frontX < 0) continue;
        if (sqDist(frontX, frontY, 1, enemyBaseX[seat], enemyBaseY[seat], 5) <= 3) {
            bool anything = false;
            for (int e : enemies) if (sqDist(ents[e].x, ents[e].y, ents[e].size, enemyBaseX[seat], enemyBaseY[seat], 5) <= 2) anything = true;
            if (!anything) {
                baseGuessVisited[seat] = true;
                if (DEBUG >= 1) fprintf(stderr, "[t%d] enemy base guess of seat %d visited: nothing there\n", tick, seat);
            }
        }
    }
    goalX = std::clamp(goalX, 0, n - 1);
    goalY = std::clamp(goalY, 0, n - 1);
    auto goalSources = [&](const std::vector<uint8_t>& p) {
        std::vector<int> src;
        int reachR = goalSize > 1 ? 2 : 2;
        for (int y = std::max(0, goalY - reachR); y <= std::min(n - 1, goalY + goalSize - 1 + reachR); y++)
            for (int x = std::max(0, goalX - reachR); x <= std::min(n - 1, goalX + goalSize - 1 + reachR); x++)
                if (sqDist(x, y, 1, goalX, goalY, goalSize) >= (goalSize > 1 ? 1 : 0) && p[cell(x, y)]) src.push_back(cell(x, y));
        if (src.empty()) {
            // nothing passable around it (unknown ground): take the ring as it is
            for (int y = std::max(0, goalY - 1); y <= std::min(n - 1, goalY + goalSize); y++)
                for (int x = std::max(0, goalX - 1); x <= std::min(n - 1, goalX + goalSize); x++)
                    if (sqDist(x, y, 1, goalX, goalY, goalSize) == (goalSize > 1 ? 1 : 0)) src.push_back(cell(x, y));
        }
        return src;
    };

    // ---- 4. movement fields: the march field from the goal (Dial, resources priced as digging), the rally field
    std::vector<uint8_t> pass = passFor(true, false);
    std::vector<uint8_t> passUnits = pass;        // my own units (even stationary) are passable for the fields
    for (int idx : army) passUnits[cell(ents[idx].x, ents[idx].y)] = 1;
    const int DIG = static_cast<int>(knob("DIG_COST", 6));
    std::vector<uint8_t> cost(static_cast<size_t>(n) * n, 0);
    for (size_t c = 0; c < cost.size(); c++) {
        int g = grid[c];
        if (passUnits[c]) cost[c] = 1;
        else if ((g >= 0 && ents[g].type == RESOURCE) || (g < 0 && (resKnown[c] || (fog && !seen[c] && believedResource(static_cast<int>(c)))))) cost[c] = static_cast<uint8_t>(DIG);
        else cost[c] = 0;
    }
    const int INF = 1 << 20;
    std::vector<int> fieldGoal;
    {
        std::vector<int> src = goalSources(passUnits);
        for (int c : src) if (cost[c] == 0) cost[c] = 1;
        dial(src, cost, DIG);
        fieldGoal = dist;
        for (size_t c = 0; c < fieldGoal.size(); c++) if (stamp[c] != gen) fieldGoal[c] = INF;
    }
    // a small raid far from the army: the nearest few units go at it, the rest keep to the rally
    std::vector<int> detachment;
    std::vector<int> fieldRaid;
    if (mode == DEFEND && threatAtHome <= static_cast<int>(knob("RAID_MAX", 3))) {
        std::vector<std::pair<int, int>> byDist;
        for (int idx : army)
            if (!handled.count(ents[idx].id)) byDist.push_back({fieldGoal[cell(ents[idx].x, ents[idx].y)], idx});
        std::sort(byDist.begin(), byDist.end());
        int k = std::min(static_cast<int>(byDist.size()), threatAtHome * 4 + 2);
        for (int i = 0; i < k; i++) detachment.push_back(byDist[i].second);
        fieldRaid = fieldGoal;
        // the others hold at the rally
        mode = HOLD;
        goalX = rallyX;
        goalY = rallyY;
        goalSize = 1;
    }
    std::vector<int> fieldRally;
    int holdR = 2;
    if (mode == HOLD) {
        // the most open cell near the rally, and a packing radius that holds the army with room to spare
        int best = -1;
        int rc = cell(rallyX, rallyY);
        for (int y = std::max(0, rallyY - 8); y <= std::min(n - 1, rallyY + 8); y++)
            for (int x = std::max(0, rallyX - 8); x <= std::min(n - 1, rallyX + 8); x++) {
                int c = cell(x, y);
                if (!passUnits[c] || reservedCell[c]) continue;
                bool nearBase = false;
                for (int idx : own) {
                    const Ent& e = ents[idx];
                    if ((e.type == BUILDER_BASE || e.type == RANGED_BASE || e.type == MELEE_BASE) && sqDist(x, y, 1, e.x, e.y, e.size) <= 3) nearBase = true;
                }
                if (nearBase) continue;
                int open = 0;
                for (int yy = std::max(0, y - 4); yy <= std::min(n - 1, y + 4); yy++)
                    for (int xx = std::max(0, x - 4); xx <= std::min(n - 1, x + 4); xx++)
                        if (std::abs(xx - x) + std::abs(yy - y) <= 4 && passUnits[cell(xx, yy)] && !reservedCell[cell(xx, yy)]) open++;
                int score = open * 3 - (std::abs(x - rallyX) + std::abs(y - rallyY)) * 2;
                if (score > best) { best = score; rc = c; }
            }
        std::vector<int> src{rc};
        std::vector<uint8_t> p2 = passUnits;
        p2[rc] = 1;
        bfs(src, p2, 40);
        fieldRally = dist;
        int need = static_cast<int>(std::ceil(nArmy * knob("PACK", 1.6)));
        int perRing[41] = {0};
        for (size_t c = 0; c < p2.size(); c++)
            if (stamp[c] == gen && dist[c] <= 40) perRing[dist[c]]++;
        int acc = 0;
        holdR = 40;
        for (int d = 0; d <= 40; d++) {
            acc += perRing[d];
            if (acc >= need) { holdR = std::max(2, d); break; }
        }
        for (size_t c = 0; c < fieldRally.size(); c++) if (stamp[c] != gen) fieldRally[c] = INF;
        int unreachable = 0;
        for (int idx : army) if (fieldRally[cell(ents[idx].x, ents[idx].y)] >= INF) unreachable++;
        if (unreachable * 2 > nArmy) {
            // most of the army cannot get there: gather instead at the most open cell near the army itself
            double cx = 0, cy = 0;
            for (int idx : army) { cx += ents[idx].x; cy += ents[idx].y; }
            int ax = static_cast<int>(cx / nArmy), ay = static_cast<int>(cy / nArmy);
            int best2 = -1, rc2 = cell(ax, ay);
            for (int y = std::max(0, ay - 6); y <= std::min(n - 1, ay + 6); y++)
                for (int x = std::max(0, ax - 6); x <= std::min(n - 1, ax + 6); x++) {
                    int c = cell(x, y);
                    if (!passUnits[c] || reservedCell[c] || onBaseRing(c)) continue;
                    int open = 0;
                    for (int yy = std::max(0, y - 3); yy <= std::min(n - 1, y + 3); yy++)
                        for (int xx = std::max(0, x - 3); xx <= std::min(n - 1, x + 3); xx++)
                            if (std::abs(xx - x) + std::abs(yy - y) <= 3 && passUnits[cell(xx, yy)] && !onBaseRing(cell(xx, yy))) open++;
                    int score = open * 4 - (std::abs(x - rallyX) + std::abs(y - rallyY)) / 2;
                    if (score > best2) { best2 = score; rc2 = c; }
                }
            std::vector<int> src2{rc2};
            std::vector<uint8_t> p3 = passUnits;
            p3[rc2] = 1;
            bfs(src2, p3, 40);
            fieldRally = dist;
            for (size_t c = 0; c < fieldRally.size(); c++) if (stamp[c] != gen) fieldRally[c] = INF;
            rc = rc2;
            if (DEBUG >= 2 && tick % 50 == 0) fprintf(stderr, "[t%d] rally (%d,%d) unreachable for %d of %d: gathering at (%d,%d)\n", tick, rallyX, rallyY, unreachable, nArmy, rc % n, rc / n);
        }
        frontX = rc % n;
        frontY = rc / n;
    }
    // the march: the vanguard and the median of its group; the ones far ahead of the median wait
    int medianF = 0, vanguardF = INF;
    if (mode != HOLD) {
        std::vector<int> vals;
        for (int idx : army) {
            int f = fieldGoal[cell(ents[idx].x, ents[idx].y)];
            if (f < INF) { vals.push_back(f); if (f < vanguardF) { vanguardF = f; frontX = ents[idx].x; frontY = ents[idx].y; } }
        }
        std::vector<int> grp;
        for (int f : vals) if (f <= vanguardF + static_cast<int>(knob("GROUP_DEPTH", 30))) grp.push_back(f);
        if (!grp.empty()) {
            std::sort(grp.begin(), grp.end());
            medianF = grp[grp.size() / 2];
        }
    }
    int leadMax = static_cast<int>(knob("LEAD_MAX", 1000));
    bool cohesive = true;
    int group = nArmy;
    // staging: a unit born while the army is away waits at the rally; a batch leaves together
    {
        std::unordered_set<int> alive;
        for (int idx : army) alive.insert(ents[idx].id);
        for (auto it = staged.begin(); it != staged.end();) {
            if (!alive.count(*it)) it = staged.erase(it); else ++it;
        }
        if (mode == HOLD) {
            staged.clear();
        } else {
            for (int idx : army) {
                const Ent& u = ents[idx];
                if (!armyMemo.count(u.id)) {
                    armyMemo[u.id] = tick;
                    // born now: far from the vanguard -> staged
                    int f = fieldGoal[cell(u.x, u.y)];
                    if (f >= INF || f > vanguardF + static_cast<int>(knob("GROUP_DEPTH", 30))) staged.insert(u.id);
                }
            }
            if (!staged.empty()) {
                int ready = 0;
                for (int idx : army)
                    if (staged.count(ents[idx].id)) {
                        int d = std::abs(ents[idx].x - rallyX) + std::abs(ents[idx].y - rallyY);
                        if (d <= static_cast<int>(knob("STAGE_R", 8))) ready++;
                    }
                if (ready >= static_cast<int>(knob("BATCH", 5)) || ready >= static_cast<int>(staged.size())) {
                    if (ready >= static_cast<int>(knob("BATCH", 5)) || tick - stageSince > static_cast<int>(knob("STAGE_MAX", 60))) {
                        staged.clear();
                        stageSince = tick;
                    }
                } else {
                    if (stageSince < 0) stageSince = tick;
                }
            } else {
                stageSince = tick;
            }
        }
    }
    std::vector<int> fieldStage;
    if (!staged.empty()) {
        std::vector<int> src{cell(rallyX, rallyY)};
        std::vector<uint8_t> p2 = passUnits;
        p2[cell(rallyX, rallyY)] = 1;
        bfs(src, p2, 60);
        fieldStage = dist;
        for (size_t c = 0; c < fieldStage.size(); c++) if (stamp[c] != gen) fieldStage[c] = INF;
    }

    // ---- 5. each mover
    double advRatio = knob("ADV_RATIO", 1.15);
    int balR = static_cast<int>(knob("BAL_R_MINE", 6)), balF = static_cast<int>(knob("BAL_R_FOE", 7));
    auto power = [&](int ux, int uy, int& mine, int& foe) {
        mine = foe = 0;
        for (int idx : army) {
            const Ent& a = ents[idx];
            if (std::abs(a.x - ux) + std::abs(a.y - uy) <= balR) mine += P[static_cast<int>(a.type)].damage * (a.hp + 5) / 10;
        }
        for (int t : turrets) {
            const Ent& a = ents[t];
            if (a.active && sqDist(ux, uy, 1, a.x, a.y, a.size) <= balR) mine += P[TURRET].damage * 3;
        }
        for (const Foe& f : foes)
            if (std::abs(f.x - ux) + std::abs(f.y - uy) <= balF) foe += P[static_cast<int>(f.type)].damage * (f.hp + 5) / 10;
    };
    auto stepOk = [&](int j) {
        if (!inside(j % n, j / n)) return false;
        int g = grid[j];
        if (g < 0) return !resKnown[j];
        const Ent& o = ents[g];
        return o.mine && P[static_cast<int>(o.type)].canMove && !stationary[j];
    };
    int movedToward = 0, held = 0, retreated = 0, advanced = 0, dug = 0, waited = 0, offRing = 0;
    // a unit that would stand still on a base's spawn ring steps off it instead
    auto holdHere = [&](int idx) {
        const Ent& u = ents[idx];
        int c = cell(u.x, u.y);
        if (onBaseRing(c)) {
            Want w{idx, {}, 6};
            std::vector<std::pair<int, int>> opts;
            for (int k = 0; k < 4; k++) {
                int nx = u.x + DX[k], ny = u.y + DY[k];
                if (!inside(nx, ny)) continue;
                int j = cell(nx, ny);
                if (!stepOk(j)) continue;
                opts.push_back({(onBaseRing(j) ? 10 : 0) + threatR1[j] * 50 + (reservedCell[j] == 1 ? 5 : 0), j});
            }
            std::sort(opts.begin(), opts.end());
            for (auto& o : opts) w.prefs.push_back(o.second);
            w.prefs.push_back(-1);
            wants.push_back(w);
            handled.insert(u.id);
            offRing++;
            return;
        }
        held++;
        stationary[c] = 1;
        handled.insert(u.id);
        out[u.id] = actIdle();
    };
    for (int idx : army) {
        const Ent& u = ents[idx];
        if (handled.count(u.id)) continue;
        int c = cell(u.x, u.y);
        int nearestFoe = 1 << 30, fx = 0, fy = 0;
        for (const Foe& f : foes) {
            int d = std::abs(f.x - u.x) + std::abs(f.y - u.y);
            if (d < nearestFoe) { nearestFoe = d; fx = f.x; fy = f.y; }
        }
        Want w{idx, {}, 7};
        bool damaged = u.hp <= P[static_cast<int>(u.type)].maxHp / 2;
        int contact = static_cast<int>(knob("CONTACT", 8));
        if (nearestFoe <= contact) {
            int mine, foe;
            power(u.x, u.y, mine, foe);
            bool advance = !damaged && mine >= foe * advRatio;
            std::vector<std::pair<int, int>> opts;
            if (advance) {
                // a step is taken only where my support outweighs what can shoot that cell
                for (int k = 0; k < 4; k++) {
                    int nx = u.x + DX[k], ny = u.y + DY[k];
                    if (!inside(nx, ny)) continue;
                    int j = cell(nx, ny);
                    if (!stepOk(j)) continue;
                    int support = 0;
                    for (int a : army)
                        if (std::abs(ents[a].x - nx) + std::abs(ents[a].y - ny) <= 5) support++;
                    for (int t : turrets) if (ents[t].active && sqDist(nx, ny, 1, ents[t].x, ents[t].y, 2) <= 5) support += 2;
                    int shooters = threatR1[j] + threatM[j];
                    if (shooters > 0 && support < shooters * knob("STEP_RATIO", 1.3) + 1) continue;
                    opts.push_back({std::abs(fx - nx) + std::abs(fy - ny) + threatM[j] * 3, j});
                }
                std::sort(opts.begin(), opts.end());
                for (auto& o : opts) if (o.first < nearestFoe) w.prefs.push_back(o.second);
                if (w.prefs.empty()) {
                    holdHere(idx);          // no safe step: hold the edge
                    continue;
                }
                advanced++;
            } else if (threatR1[c] || threatM[c] || damaged) {
                for (int k = 0; k < 4; k++) {
                    int nx = u.x + DX[k], ny = u.y + DY[k];
                    if (!inside(nx, ny)) continue;
                    int j = cell(nx, ny);
                    if (!stepOk(j)) continue;
                    int score = threatR1[j] * 60 + threatR[j] * 100 + threatM[j] * 80 - (std::abs(fx - nx) + std::abs(fy - ny)) * 4;
                    opts.push_back({score, j});
                }
                int here = threatR1[c] * 60 + threatR[c] * 100 + threatM[c] * 80 - nearestFoe * 4;
                std::sort(opts.begin(), opts.end());
                for (auto& o : opts) if (o.first < here) w.prefs.push_back(o.second);
                retreated++;
            } else {
                holdHere(idx);
                continue;
            }
            w.prefs.push_back(-1);
            wants.push_back(w);
            handled.insert(u.id);
            continue;
        }
        // no contact: march (attack/defend/detachment) or gather (hold); staged units gather at the rally
        bool inDetachment = !fieldRaid.empty() && std::find(detachment.begin(), detachment.end(), idx) != detachment.end();
        bool isStaged = !inDetachment && !fieldStage.empty() && staged.count(u.id);
        const std::vector<int>& fld = inDetachment ? fieldRaid : (isStaged ? fieldStage : (mode == HOLD ? fieldRally : fieldGoal));
        int here = fld[c];
        bool marching = (inDetachment || mode != HOLD) && !isStaged;
        if (isStaged && here <= static_cast<int>(knob("STAGE_R", 8)) / 2 + 1) {
            holdHere(idx);
            continue;
        }
        if (!marching && here <= holdR) {
            holdHere(idx);
            continue;
        }
        if (marching && !inDetachment && here < medianF - leadMax) {
            waited++;          // ahead of the group: wait for it
            holdHere(idx);
            continue;
        }
        if (here >= INF) {
            // the goal is unreachable from here: greedy by distance, off the rings
            std::vector<std::pair<int, int>> opts;
            int gx = mode == HOLD ? rallyX : goalX, gy = mode == HOLD ? rallyY : goalY;
            for (int k = 0; k < 4; k++) {
                int nx = u.x + DX[k], ny = u.y + DY[k];
                if (!inside(nx, ny)) continue;
                int j = cell(nx, ny);
                if (!stepOk(j)) continue;
                opts.push_back({(std::abs(nx - gx) + std::abs(ny - gy)) * 2 + (onBaseRing(j) ? 20 : 0) + threatR1[j] * 50, j});
            }
            std::sort(opts.begin(), opts.end());
            int hereScore = (std::abs(u.x - gx) + std::abs(u.y - gy)) * 2 + (onBaseRing(c) ? 20 : 0);
            for (auto& o : opts) if (o.first < hereScore) w.prefs.push_back(o.second);
            if (w.prefs.empty()) { holdHere(idx); continue; }
            w.prefs.push_back(-1);
            wants.push_back(w);
            handled.insert(u.id);
            movedToward++;
            continue;
        }
        // the best neighbour by the field; a resource in the way is shot
        int bestJ = -1, bestV = here, bestRes = -1, bestResV = here;
        for (int k = 0; k < 4; k++) {
            int nx = u.x + DX[k], ny = u.y + DY[k];
            if (!inside(nx, ny)) continue;
            int j = cell(nx, ny);
            int v = fld[j];
            if (v >= here) continue;
            if (stepOk(j)) {
                if (threatR1[j] && !threatR1[c] && !inDetachment) continue;     // not into range alone
                int vv = v * 4 + threatR1[j] * 50;
                if (bestJ < 0 || vv < bestV) { bestV = vv; bestJ = j; }
            } else if (marching && grid[j] >= 0 && ents[grid[j]].type == RESOURCE && u.type == RANGED_UNIT) {
                if (v < bestResV) { bestResV = v; bestRes = grid[j]; }
            }
        }
        if (bestJ >= 0) {
            w.prefs.push_back(bestJ);
            // alternatives: other neighbours that do not go backward
            for (int k = 0; k < 4; k++) {
                int nx = u.x + DX[k], ny = u.y + DY[k];
                if (!inside(nx, ny)) continue;
                int j = cell(nx, ny);
                if (j != bestJ && fld[j] <= here && stepOk(j) && !(threatR1[j] && !threatR1[c])) w.prefs.push_back(j);
            }
            w.prefs.push_back(-1);
            wants.push_back(w);
            handled.insert(u.id);
            movedToward++;
        } else if (bestRes >= 0) {
            out[u.id] = actAttack(ents[bestRes].id);
            stationary[c] = 1;
            handled.insert(u.id);
            dug++;
        } else if (marching && u.type == RANGED_UNIT) {
            // nothing closer is free: shoot a resource within range that lies on the way (lower field)
            int range = P[static_cast<int>(u.type)].range;
            int best = -1, bestKey = 1 << 30;
            for (int y = std::max(0, u.y - range); y <= std::min(n - 1, u.y + range); y++)
                for (int x = std::max(0, u.x - range); x <= std::min(n - 1, u.x + range); x++) {
                    if (std::abs(x - u.x) + std::abs(y - u.y) > range) continue;
                    int j = cell(x, y);
                    int g = grid[j];
                    if (g < 0 || ents[g].type != RESOURCE || fld[j] >= here) continue;
                    int key = fld[j] * 4 + ents[g].hp;
                    if (key < bestKey) { bestKey = key; best = g; }
                }
            if (best >= 0) {
                out[u.id] = actAttack(ents[best].id);
                dug++;
                stationary[c] = 1;
                handled.insert(u.id);
            } else {
                holdHere(idx);
            }
        } else {
            holdHere(idx);
        }
    }
    (void)cohesive; (void)group;
    if (DEBUG >= 2 && tick % 50 == 0)
        fprintf(stderr, "[t%d] army %d mode %s est %d vanguard (%d,%d) f=%d median %d goal (%d,%d) shoot %zu adv %d ret %d hold %d move %d wait %d dig %d offring %d det %zu\n",
                tick, nArmy, mode == HOLD ? "HOLD" : mode == DEFEND ? "DEFEND" : "ATTACK", enemyEst, frontX, frontY, vanguardF, medianF,
                goalX, goalY, assigned.size(), advanced, retreated, held, movedToward, waited, dug, offRing, detachment.size());
}

// ------------------------------------------------------------------------------------ move resolver
void Brain::resolveMoves() {
    std::sort(wants.begin(), wants.end(), [](const Want& a, const Want& b) { return a.priority > b.priority; });
    std::vector<int> claimed(static_cast<size_t>(n) * n, -1);    // cell -> ents idx
    std::vector<int> chosen(ents.size(), -2);                    // ents idx -> cell or -1 stay
    std::vector<int> cellOf(ents.size());
    for (size_t i = 0; i < ents.size(); i++) cellOf[i] = cell(ents[i].x, ents[i].y);
    // several passes: a unit whose target is taken or swaps falls back to its next preference
    std::vector<size_t> pending(wants.size());
    for (size_t i = 0; i < pending.size(); i++) pending[i] = i;
    std::vector<size_t> prefPos(wants.size(), 0);
    for (int pass = 0; pass < 4 && !pending.empty(); pass++) {
        std::vector<size_t> next;
        for (size_t wi : pending) {
            Want& w = wants[wi];
            bool done = false;
            while (prefPos[wi] < w.prefs.size()) {
                int c = w.prefs[prefPos[wi]];
                if (c < 0) { chosen[w.idx] = -1; done = true; break; }
                int g = grid[c];
                bool ok = true;
                if (claimed[c] >= 0 && claimed[c] != w.idx) ok = false;
                if (ok && g >= 0) {
                    const Ent& o = ents[g];
                    if (!(o.mine && P[static_cast<int>(o.type)].canMove)) ok = false;    // a building / resource / enemy
                    else if (stationary[c]) ok = false;
                    else if (chosen[g] == -1) ok = false;                           // it decided to stay
                    else if (chosen[g] == cellOf[w.idx]) ok = false;                // a swap
                }
                if (ok) {
                    claimed[c] = w.idx;
                    chosen[w.idx] = c;
                    done = true;
                    break;
                }
                prefPos[wi]++;
            }
            if (!done) chosen[w.idx] = -1;
            else if (chosen[w.idx] >= 0) next.push_back(wi);     // re-check for swaps that appear later
        }
        // verify: a claimed cell whose occupant later chose to stay or to swap -> retry
        pending.clear();
        for (size_t wi : next) {
            Want& w = wants[wi];
            int c = chosen[w.idx];
            int g = grid[c];
            if (g >= 0 && (chosen[g] == -1 || chosen[g] == cellOf[w.idx])) {
                claimed[c] = -1;
                chosen[w.idx] = -2;
                prefPos[wi]++;
                pending.push_back(wi);
            }
        }
    }
    // a unit that wanted to move and could not: count it; after two such ticks it may sidestep to any free cell
    for (const Want& w : wants) {
        const Ent& u = ents[w.idx];
        int c = chosen[w.idx];
        bool wanted = !w.prefs.empty() && w.prefs[0] >= 0;
        if (c >= 0 || !wanted) { idleTicks[u.id] = 0; continue; }
        int& it = idleTicks[u.id];
        it++;
        if (it >= 2) {
            std::vector<int> opts;
            for (int k = 0; k < 4; k++) {
                int nx = u.x + DX[k], ny = u.y + DY[k];
                if (!inside(nx, ny)) continue;
                int j = cell(nx, ny);
                if (grid[j] >= 0 || claimed[j] >= 0 || reservedCell[j]) continue;
                if (threatR1[j] && u.type == BUILDER_UNIT) continue;
                opts.push_back(j);
            }
            if (!opts.empty()) {
                int j = opts[static_cast<size_t>((tick * 7919 + u.id * 104729) % static_cast<int>(opts.size()))];
                claimed[j] = w.idx;
                chosen[w.idx] = j;
                it = 0;
            }
            if (u.type == BUILDER_UNIT) {
                auto so = slotOf.find(u.id);
                if (so != slotOf.end()) {
                    avoidSlot[u.id] = so->second;
                    slotOf.erase(so);
                }
            }
        }
    }
    for (const Want& w : wants) {
        const Ent& u = ents[w.idx];
        int c = chosen[w.idx];
        if (c >= 0) out[u.id] = actMove(c % n, c / n);
        else if (!out.count(u.id)) out[u.id] = actIdle();
    }
}

void Brain::accumulateStats() {
    for (int idx : builders) {
        const Ent& b = ents[idx];
        auto it = out.find(b.id);
        bool hasSlot = slotOf.count(b.id);
        if (it == out.end()) { statIdle++; continue; }
        const EntityAction& a = it->second;
        if (a.attackAction) statMine++;
        else if (a.buildAction) statBuild++;
        else if (a.repairAction) statRepair++;
        else if (a.moveAction) {
            if (hasSlot && slotOf[b.id] != cell(b.x, b.y)) {
                statWalkSlot++;
                statWalkDist += std::abs(slotOf[b.id] % n - b.x) + std::abs(slotOf[b.id] / n - b.y);
            } else if (builderDanger[cell(b.x, b.y)]) statFlee++;
            else statWalkSite++;
        } else statIdle++;
        auto ps = prevSlot.find(b.id);
        if (hasSlot && ps != prevSlot.end() && ps->second != slotOf[b.id]) statReslot++;
    }
    prevSlot.clear();
    for (const auto& kv : slotOf) prevSlot[kv.first] = kv.second;
}

void Brain::debugPrint() {
    if (DEBUG >= 2) {
        long long tot = statMine + statWalkSlot + statWalkSite + statRepair + statBuild + statFlee + statIdle;
        if (tot > 0)
            fprintf(stderr, "[t%4d] builder-ticks since last: mine %.0f%% walkSlot %.0f%% (avg dist %.1f) walkSite %.0f%% repair %.0f%% build %.0f%% flee %.0f%% idle %.0f%%; reslots %lld\n",
                    tick, 100.0 * statMine / tot, 100.0 * statWalkSlot / tot, statWalkSlot ? static_cast<double>(statWalkDist) / statWalkSlot : 0.0,
                    100.0 * statWalkSite / tot, 100.0 * statRepair / tot, 100.0 * statBuild / tot, 100.0 * statFlee / tot, 100.0 * statIdle / tot, statReslot);
        statMine = statWalkSlot = statWalkSite = statRepair = statBuild = statFlee = statIdle = statReslot = statWalkDist = 0;
    }
    int mining = 0, building = 0, repairing = 0, moving = 0, idle = 0;
    for (int idx : builders) {
        auto it = out.find(ents[idx].id);
        if (it == out.end()) { idle++; continue; }
        const EntityAction& a = it->second;
        if (a.attackAction) mining++;
        else if (a.buildAction) building++;
        else if (a.repairAction) repairing++;
        else if (a.moveAction) moving++;
        else idle++;
    }
    int hp = 0;
    for (const Site& st : sites) if (st.type == HOUSE) hp++;
    if (DEBUG >= 2) {
        int far[4] = {0, 0, 0, 0};
        for (int idx : builders) {
            auto so = slotOf.find(ents[idx].id);
            if (so == slotOf.end()) continue;
            int d = std::abs(so->second % n - ents[idx].x) + std::abs(so->second / n - ents[idx].y);
            if (d == 0) continue;
            far[d <= 5 ? 0 : d <= 12 ? 1 : d <= 25 ? 2 : 3]++;
        }
        int nearerFree = 0;
        {
            std::vector<uint8_t> passW = passFor(true, false);
            for (int idx : builders) {
                auto so = slotOf.find(ents[idx].id);
                if (so == slotOf.end()) continue;
                int d = std::abs(so->second % n - ents[idx].x) + std::abs(so->second / n - ents[idx].y);
                if (d <= 12) continue;
                std::vector<int> src{cell(ents[idx].x, ents[idx].y)};
                bfs(src, passW, 8);
                bool found = false;
                for (size_t c = 0; c < passW.size() && !found; c++)
                    if (stamp[c] == gen && slotTaken[c] < 0 && !reservedCell[c] && !resKnown[c] && grid[c] < 0 && adjResource(static_cast<int>(c) % n, static_cast<int>(c) / n)) found = true;
                if (found) nearerFree++;
            }
        }
        fprintf(stderr, "[t%4d] walkers by slot distance: <=5:%d <=12:%d <=25:%d >25:%d (far ones with a free slot within 8: %d); sites:", tick, far[0], far[1], far[2], far[3], nearerFree);
        for (const Site& st : sites)
            fprintf(stderr, " %s(%d,%d)%s%s", st.type == HOUSE ? "H" : st.type == RANGED_BASE ? "RB" : "T", st.x, st.y,
                    st.placed ? "+" : "", (!st.placed && footprintResources(st.x, st.y, st.size) > 0) ? "r" : "");
        fprintf(stderr, "\n");
    }
    fprintf(stderr, "[t%4d] res=%5d score=%6d pop=%d/%d B=%zu(mine %d build %d rep %d walk %d idle %d) R=%zu M=%zu H=%zu(+%d) T=%zu RB=%zu %s cpu=%lldms lost=%d\n",
            tick, myResource, myScore, popUsed, popProvided, builders.size(), mining, building, repairing, moving, idle,
            rangedU.size(), meleeU.size(), houses.size(), hp, turrets.size(), rbases.size(), attacking ? "ATTACK" : "gather",
            cpuMicros / 1000, unitsLostTotal);
}

void Brain::debugMap() {
    int w = std::min(n, static_cast<int>(knob("MAP_W", 44)));
    fprintf(stderr, "--- map t%d (x 0..%d, y top=%d)\n", tick, w - 1, w - 1);
    for (int y = w - 1; y >= 0; y--) {
        std::string line;
        for (int x = 0; x < w; x++) {
            int c = cell(x, y);
            int g = grid[c];
            char ch = '.';
            if (g >= 0) {
                const Ent& e = ents[g];
                if (e.type == RESOURCE) ch = 'r';
                else if (!e.mine) ch = 'E';
                else if (e.type == BUILDER_UNIT) {
                    auto it = out.find(e.id);
                    ch = 'x';
                    if (it != out.end()) {
                        if (it->second.attackAction) ch = 'B';
                        else if (it->second.repairAction) ch = 'p';
                        else if (it->second.buildAction) ch = 'c';
                        else if (it->second.moveAction) ch = 'b';
                    }
                } else if (e.type == RANGED_UNIT) ch = 'A';
                else if (e.type == MELEE_UNIT) ch = 'M';
                else if (e.type == HOUSE) ch = e.active ? 'h' : 'u';
                else ch = '#';
            } else if (resKnown[c]) ch = 'R';
            else if (fog && !seen[c]) ch = lastState[c] < 0 ? (believedResource(c) ? ',' : ' ') : ' ';
            else if (reservedCell[c]) ch = '_';
            line += ch;
        }
        fprintf(stderr, "%s\n", line.c_str());
    }
}

Action Brain::act(const PlayerView& v) {
    auto t0 = std::chrono::steady_clock::now();
    if (n == 0) {
        DEBUG = static_cast<int>(knob("DEBUG", 0));
        setup(v);
    }
    view_ = &v;
    parse(v);
    updateMemory();
    computeThreats();
    out.clear();
    wants.clear();
    handled.clear();
    stationary.assign(static_cast<size_t>(n) * n, 0);
    if (reservedCell.empty()) reservedCell.assign(static_cast<size_t>(n) * n, 0);
    // buildings and inactive things never move; mark their cells (units use passFor)
    military();
    economy();
    resolveMoves();
    // everything of mine gets an explicit order (an empty one stops a stale order)
    for (int idx : own) {
        const Ent& e = ents[idx];
        if (!out.count(e.id)) out[e.id] = actIdle();
    }
    cpuMicros += std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now() - t0).count();
    if (DEBUG >= 2) accumulateStats();
    if (DEBUG >= 1 && (tick % 50 == 0 || tick == v.maxTickCount - 1)) debugPrint();
    else if (DEBUG >= 2 && tick < 120 && tick % 5 == 0) debugPrint();
    if (DEBUG >= 3 && tick % static_cast<int>(knob("MAP_EVERY", 50)) == 0) debugMap();
    Action result;
    result.entityActions = out;
    return result;
}

Brain brain;

}  // namespace

MyStrategy::MyStrategy() {}

Action MyStrategy::getAction(const PlayerView& playerView, DebugInterface* debugInterface) {
    (void)debugInterface;
    return brain.act(playerView);
}

void MyStrategy::debugUpdate(const PlayerView& playerView, DebugInterface& debugInterface) {
    (void)playerView;
    debugInterface.send(DebugCommand::Clear());
    debugInterface.getState();
}
