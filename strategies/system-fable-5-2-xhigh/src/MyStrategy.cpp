// system-fable-5-2-xhigh: a CodeCraft 2020 strategy in one file (the design is in the README beside it).
//
// The shape of a tick:
//   parse -> memory (fog) -> assess (threat maps, lists) -> fire (who shoots whom, exactly enough to kill)
//   -> economy (sites and plans, flight, build jobs, the mining auction) -> army (simulated battles, the line,
//   guard, march) -> produce (bases) -> moves (priority inheritance: every unit one step, chains but never
//   swaps) -> orders.
// Every unit gets a fresh order every tick; every move is one cell on fields computed here.
// Every tunable is KN("NAME", default), read once from the environment as CC_NAME; CC_DEBUG=1..3 talks on stderr.
#include "MyStrategy.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <queue>
#include <string>
#include <unordered_map>
#include <vector>

namespace {

constexpr int N = 80;
constexpr int NN = N * N;
constexpr int INF = 1000000000;
const int DX[4] = {1, -1, 0, 0};
const int DY[4] = {0, 0, 1, -1};
const int SIZE_OF[10] = {1, 3, 5, 1, 5, 1, 5, 1, 1, 2};
const int SIGHT_OF[10] = {2, 5, 5, 10, 5, 10, 5, 10, 0, 10};
const int MAXHP_OF[10] = {50, 50, 300, 10, 300, 50, 300, 10, 30, 100};

inline int cellOf(int x, int y) { return y * N + x; }
inline bool inMap(int x, int y) { return x >= 0 && y >= 0 && x < N && y < N; }
inline int cx(int c) { return c % N; }
inline int cy(int c) { return c / N; }
inline int mdist(int a, int b) { return std::abs(cx(a) - cx(b)) + std::abs(cy(a) - cy(b)); }
// Manhattan distance from a cell to a square
inline int sqDist(int x, int y, int bx, int by, int s) {
    int dx = std::max({0, bx - x, x - (bx + s - 1)});
    int dy = std::max({0, by - y, y - (by + s - 1)});
    return dx + dy;
}

double knobRaw(const char* name, double def) {
    std::string k = std::string("CC_") + name;
    const char* v = getenv(k.c_str());
    return v ? atof(v) : def;
}
#define KN(name, def) ([]() -> double { static const double v = knobRaw(name, def); return v; }())

struct Ent {
    int id, own, type, x, y, hp, size, cell;
    bool active;
    bool unit() const { return type == BUILDER_UNIT || type == RANGED_UNIT || type == MELEE_UNIT; }
    bool building() const { return !unit() && type != RESOURCE; }
};

enum { ACT_NONE = 0, ACT_ATTACK, ACT_REPAIR, ACT_BUILD };
enum { MODE_NONE = 0, MODE_ATTACK, MODE_HOLD, MODE_RETREAT };

struct Desire {
    int cand[5];
    int val[5];
    int n = 0;
    int pri = 0;
    bool fixed = false;
    bool set = false;
    int act = ACT_NONE, a1 = 0, a2 = 0, a3 = 0;
    int next = -1;
    int cls = 0;          // the priority class it was given (before waiting time is added)
};

struct UnitMem {
    int seen = -1;
    int wait = 0;
    int lastCell = -1;
    int mode = MODE_NONE;
    long long job = 0;
    int joined = 0;
    int born = -1;
    int stuck = 0;
    int walkRun = 0;      // debug: ticks of the current walk to a slot
    int lastRole = 0;     // debug: 1 mining, 2 walking, 3 job
};

struct Plan {
    long long key;
    int type, x, y, size;
    int born;
    bool clearing = false;
};

struct Job {
    long long key;
    int type, x, y, size;
    bool placed;
    int entIdx;
    int need;
    int cost;
    std::vector<int> workers;
};

struct MemBuilding {
    int own, type, x, y, hp, seen;
};

struct MemUnit {
    int own, type, cell, hp, seen;
};

class Brain {
public:
    Action tick(const PlayerView& v);

private:
    // ---------------------------------------------------------------- the game
    bool started = false;
    int myId = 0, tickNo = 0, nPlayers = 2;
    bool fog = false;
    int myRes = 0, myScore = 0;
    int debug = 0;
    double spentMs = 0;                            // wall clock spent so far
    bool lowPower = false;                         // the clock ran away: no simulations, short auctions
    std::vector<int> enemyIds;
    std::unordered_map<int, int> enemyScore;
    std::unordered_map<int, int> enemyCorner;      // player id -> cell of its builder base's corner (my frame)
    std::unordered_map<int, int> enemyDeadTick;    // player id -> tick I found its corner empty

    // ---------------------------------------------------------------- what is remembered between ticks
    int resHp[NN];                                 // believed resource health, 0 = none
    int lastSeen[NN];                              // tick the cell was last in sight, -1 never
    signed char firstRes[NN];                      // what the cell held when first seen
    signed char symRes[NN];                        // what the first seen of its eight images held
    std::unordered_map<int, MemBuilding> memB;     // enemy buildings as last seen
    std::unordered_map<int, MemUnit> memU;         // enemy units as last seen
    std::unordered_map<int, UnitMem> umem;         // my units
    std::unordered_map<int, int> yieldUntil;       // builder id -> tick until which it does not hold the cell it works from
    std::vector<Plan> plans;                       // buildings decided on and not yet placed
    long long planSerial = 0;
    std::unordered_map<long long, int> planFail;   // plan key -> ticks nobody could come
    std::unordered_map<int, int> siteBan;          // site cell and size -> tick until which it is banned
    int rbResX = -1, rbResY = -1;                  // the land kept for the ranged base
    unsigned char road[NN];                        // the army's way out and its guard ground: no houses here
    bool roadInit = false;
    int rallyCell = -1, rallyTick = -1000;         // the guard ground
    int threatCell = -1, threatTick = -1000;       // where the enemy's fighters were last seen coming from
    int lastHomeThreat = -1000;
    int targetPlayer = 0;
    bool attacking = false;
    int minedLast = 0;

    // ---------------------------------------------------------------- the tick: the world
    std::vector<Ent> es;
    int occ[NN];                                   // entity index on a cell (in sight), -1 none
    unsigned char vis[NN];
    unsigned char wall[NN];                        // 1 resource (seen or believed), 2 building (seen or remembered)
    unsigned char hardBlk[NN];                     // walls and enemy units
    unsigned char blkW[NN];                        // + planned sites: what no path of mine crosses
    unsigned char blkWF[NN];                       // + my units that work this tick
    unsigned char fixedAt[NN];                     // a unit of mine that will not move this tick
    unsigned char keepOut[NN];                     // planned sites: nobody stays here
    unsigned char planArea[NN];                    // the cells of every plan and of the kept land
    unsigned char noIdle[NN];                      // the doorsteps of my bases: nobody idles here
    unsigned char prioRes[NN];                     // resources to clear first (a site)
    unsigned char homeZone[NN];                    // within a short walk of my buildings
    unsigned char scare[NN];                       // an enemy fighter can shoot here after a step: builders leave
    int thr5[NN], thr6[NN], thrM[NN];              // enemy shooters in range now, after their step; melee near
    std::vector<int> myBuilders, myRanged, myMelee, myTurrets, myHouses, myBuildings;
    std::vector<int> enRanged, enMelee, enTurrets, enBuilders, enBuildings, enCombat;
    int builderBase = -1, rangedBase = -1, meleeBase = -1;
    int popCap = 0, popUsed = 0, popSoon = 0;
    int income = 0;
    int baseCell = cellOf(7, 7);

    // ---------------------------------------------------------------- the tick: decisions
    std::vector<Desire> des;
    std::vector<Job> jobs;
    std::vector<char> evicted;                     // stands on a planned site and is leaving it
    bool allowKeepOut = false;                     // setMove for those: sites are not walls
    mutable bool siteOnRoad = false;               // pickSite for the ranged base: the road is not forbidden
    int reserve = 0;
    double prodRate = 0;
    bool rich = false;

    // ---------------------------------------------------------------- fields (walking distances)
    int fieldBase[NN];                             // from the builder base
    int fieldTerr[NN];                             // from any building of mine
    int fieldNearB[NN];                            // from the nearest builder
    int fieldA[NN];                                // to the nearest open mining cell (where new builders appear)
    int fieldB[NN];                                // scratch: explorers' field, then the way to a shot at an intruder
    int fieldSlot[NN], labelSlot[NN];              // the auction's
    int fieldAtk[NN];                              // to a shot at anything seen
    int fieldHome[NN];                             // to the guard ground
    int fieldMarch[NN];                            // to the target, digging priced in
    int jobField[8][NN];                           // to each building site's ring
    int exitD[NN];                                 // out of a planned site
    int qbuf[NN * 2];

    // ---------------------------------------------------------------- debug statistics
    int statMine = 0, statWalk = 0, statJob = 0, statFlee = 0, statIdle = 0;
    long long cumMine = 0, cumWalk = 0, cumJob = 0, cumFlee = 0, cumIdle = 0, cumB = 0;
    long long walkHist[6] = {0, 0, 0, 0, 0, 0}, walkHistT[6] = {0, 0, 0, 0, 0, 0}, walkAfterJob = 0, walkAfterMine = 0, walkNew = 0;

    // ---------------------------------------------------------------- steps
    void parse(const PlayerView& v);
    void memory();
    void assess();
    void fire();
    void economy();
    void produce();
    void army();
    void moves();
    Action orders();
    void report();

    // helpers
    void bfs(const std::vector<int>& src, const unsigned char* blk, int* dist) const;
    void bfsOff(const std::vector<std::pair<int, int>>& src, const unsigned char* blk, int* dist, int* label) const;
    bool cellFreeForSite(int c) const;
    bool siteValid(int x, int y, int s, int gap) const;
    int ringArcs(int x, int y, int s) const;
    bool pickSite(int type, int& bx, int& by);
    bool pickClearSite(int& bx, int& by, int hpLimit);
    void setStay(int i, int pri);
    template <class F> void setMove(int i, int pri, F f);
    void setFixed(int i, int act, int a1 = 0, int a2 = 0, int a3 = 0);
    bool pibt(int i, int pusherCell, std::vector<int>& unitAt, std::vector<int>& resv, std::vector<unsigned char>& chain, int depth);
    int unitCost(int type) const;
    int spawnCell(int baseIdx, const int* field) const;
    void planning();
    void buildJobs();
    void mining(std::vector<int>& free);
    int enemyPower(int pid) const;
    void chooseTarget();
    int houseK() const { return (int)KN("HOUSE_K", 3); }
    struct SimUnit {
        int cell, hp, side, type;      // type: 0 ranged, 1 melee, 2 turret
    };
    void simulate(const std::vector<SimUnit>& init, bool advMe, bool advEn, int T, const int* toEn, const int* toMe, double& myLoss, double& enLoss);
};

// ------------------------------------------------------------------------------------------------ geometry and searches
// the cells of the map within Manhattan distance r of a square
template <class F>
inline void around(int x, int y, int s, int r, F f) {
    int x0 = std::max(x - r, 0), x1 = std::min(x + s + r, N);
    for (int ax = x0; ax < x1; ax++) {
        int gap = std::max({0, x - ax, ax - (x + s - 1)});
        int rest = r - gap;
        int y0 = std::max(y - rest, 0), y1 = std::min(y + s + rest, N);
        for (int ay = y0; ay < y1; ay++) f(cellOf(ax, ay));
    }
}

template <class F>
inline void edgeRing(int x, int y, int s, F f) {     // the cells that touch a square by an edge
    for (int k = 0; k < s; k++) {
        if (inMap(x + k, y - 1)) f(cellOf(x + k, y - 1));
        if (inMap(x + k, y + s)) f(cellOf(x + k, y + s));
        if (inMap(x - 1, y + k)) f(cellOf(x - 1, y + k));
        if (inMap(x + s, y + k)) f(cellOf(x + s, y + k));
    }
}

void Brain::bfs(const std::vector<int>& src, const unsigned char* blk, int* dist) const {
    std::fill(dist, dist + NN, INF);
    int* q = const_cast<int*>(qbuf);
    int h = 0, t = 0;
    for (int s : src)
        if (dist[s] == INF) {
            dist[s] = 0;
            q[t++] = s;
        }
    while (h < t) {
        int c = q[h++];
        int x = cx(c), y = cy(c), d = dist[c] + 1;
        for (int k = 0; k < 4; k++) {
            int nx = x + DX[k], ny = y + DY[k];
            if (!inMap(nx, ny)) continue;
            int nc = cellOf(nx, ny);
            if (dist[nc] != INF || blk[nc]) continue;
            dist[nc] = d;
            q[t++] = nc;
        }
    }
}

// sources with start offsets; label = the source cell a cell belongs to
void Brain::bfsOff(const std::vector<std::pair<int, int>>& srcIn, const unsigned char* blk, int* dist, int* label) const {
    std::fill(dist, dist + NN, INF);
    std::vector<std::pair<int, int>> src = srcIn;   // (offset, cell)
    std::sort(src.begin(), src.end());
    static std::vector<int> q;
    q.clear();
    size_t h = 0, si = 0;
    auto expand = [&](int c) {
        int x = cx(c), y = cy(c), d = dist[c] + 1;
        for (int k = 0; k < 4; k++) {
            int nx = x + DX[k], ny = y + DY[k];
            if (!inMap(nx, ny)) continue;
            int nc = cellOf(nx, ny);
            if (blk[nc] || dist[nc] <= d) continue;
            dist[nc] = d;
            if (label) label[nc] = label[c];
            q.push_back(nc);
        }
    };
    while (h < q.size() || si < src.size()) {
        if (si < src.size() && (h == q.size() || src[si].first <= dist[q[h]])) {
            int off = src[si].first, c = src[si].second;
            si++;
            if (dist[c] <= off) continue;
            dist[c] = off;
            if (label) label[c] = c;
            expand(c);
        } else {
            expand(q[h++]);
        }
    }
}

// ------------------------------------------------------------------------------------------------ the world
void Brain::parse(const PlayerView& v) {
    tickNo = v.currentTick;
    myId = v.myId;
    fog = v.fogOfWar;
    nPlayers = (int)v.players.size();
    if (!started) {
        started = true;
        debug = (int)knobRaw("DEBUG", 0);
        std::fill(resHp, resHp + NN, 0);
        std::fill(lastSeen, lastSeen + NN, -1);
        std::fill(firstRes, firstRes + NN, (signed char)-1);
        std::fill(symRes, symRes + NN, (signed char)-1);
        int mySeat = myId - 1;
        for (const Player& p : v.players) {
            if (p.id == myId) continue;
            enemyIds.push_back(p.id);
            int seat = p.id - 1, x = 5, y = 5, s = 5;
            int rx = x, ry = y;                       // the seat's builder base on the real map
            if (seat == 1) rx = N - x - s, ry = N - y - s;
            else if (seat == 2) rx = N - y - s, ry = x;
            else if (seat == 3) rx = y, ry = N - x - s;
            int vx = rx, vy = ry;                     // and in my frame
            if (mySeat == 1) vx = N - rx - s, vy = N - ry - s;
            else if (mySeat == 2) vx = ry, vy = N - rx - s;
            else if (mySeat == 3) vx = N - ry - s, vy = rx;
            enemyCorner[p.id] = cellOf(vx, vy);
        }
    }
    for (const Player& p : v.players) {
        if (p.id == myId) {
            myRes = p.resource;
            myScore = p.score;
        } else {
            enemyScore[p.id] = p.score;
        }
    }
    es.clear();
    es.reserve(v.entities.size());
    for (const Entity& e : v.entities) {
        Ent x;
        x.id = e.id;
        x.type = (int)e.entityType;
        x.own = e.playerId ? (*e.playerId == myId ? 0 : *e.playerId) : -1;
        x.x = e.position.x;
        x.y = e.position.y;
        x.hp = e.health;
        x.active = e.active;
        x.size = SIZE_OF[x.type];
        x.cell = cellOf(x.x, x.y);
        es.push_back(x);
    }
    std::fill(occ, occ + NN, -1);
    for (int i = 0; i < (int)es.size(); i++) {
        const Ent& e = es[i];
        for (int dy = 0; dy < e.size; dy++)
            for (int dx = 0; dx < e.size; dx++)
                if (inMap(e.x + dx, e.y + dy)) occ[cellOf(e.x + dx, e.y + dy)] = i;
    }
    des.assign(es.size(), Desire());
}

void Brain::memory() {
    if (!fog) {
        memset(vis, 1, sizeof vis);
    } else {
        memset(vis, 0, sizeof vis);
        for (const Ent& e : es)
            if (e.own == 0) around(e.x, e.y, e.size, SIGHT_OF[e.type], [&](int c) { vis[c] = 1; });
    }
    for (int c = 0; c < NN; c++) {
        if (!vis[c]) continue;
        int o = occ[c];
        bool r = o >= 0 && es[o].type == RESOURCE;
        if (firstRes[c] < 0) {
            firstRes[c] = r ? 1 : 0;
            int x = cx(c), y = cy(c);
            int ab[2][2] = {{x, y}, {y, x}};
            for (auto& p : ab) {
                int a = p[0], b = p[1];
                int im[4] = {cellOf(a, b), cellOf(N - 1 - a, b), cellOf(a, N - 1 - b), cellOf(N - 1 - a, N - 1 - b)};
                for (int k : im)
                    if (symRes[k] < 0) symRes[k] = r ? 1 : 0;
            }
        }
        resHp[c] = r ? es[o].hp : 0;
        lastSeen[c] = tickNo;
    }
    // enemy buildings and units as last seen
    for (const Ent& e : es) {
        if (e.own <= 0) continue;
        if (e.building()) memB[e.id] = MemBuilding{e.own, e.type, e.x, e.y, e.hp, tickNo};
        else memU[e.id] = MemUnit{e.own, e.type, e.cell, e.hp, tickNo};
    }
    for (auto it = memB.begin(); it != memB.end();) {
        const MemBuilding& b = it->second;
        bool gone = false;
        if (b.seen != tickNo) {
            int s = SIZE_OF[b.type];
            for (int dy = 0; dy < s && !gone; dy++)
                for (int dx = 0; dx < s; dx++)
                    if (vis[cellOf(b.x + dx, b.y + dy)]) {
                        gone = true;
                        break;
                    }
        }
        it = gone ? memB.erase(it) : std::next(it);
    }
    for (auto it = memU.begin(); it != memU.end();) {
        const MemUnit& u = it->second;
        bool drop = u.seen != tickNo && (tickNo - u.seen > 120);
        it = drop ? memU.erase(it) : std::next(it);
    }
    for (int c = 0; c < NN; c++) {
        unsigned char w = 0;
        if (vis[c]) {
            int o = occ[c];
            if (o >= 0) {
                if (es[o].type == RESOURCE) w = 1;
                else if (es[o].building()) w = 2;
            }
        } else if (resHp[c] > 0) {
            w = 1;
        } else if (lastSeen[c] < 0 && symRes[c] == 1) {
            w = 1;
        }
        wall[c] = w;
    }
    for (const auto& kv : memB) {
        const MemBuilding& b = kv.second;
        int s = SIZE_OF[b.type];
        for (int dy = 0; dy < s; dy++)
            for (int dx = 0; dx < s; dx++) {
                int c = cellOf(b.x + dx, b.y + dy);
                if (!vis[c]) wall[c] = 2;
            }
    }
}

void Brain::assess() {
    myBuilders.clear(); myRanged.clear(); myMelee.clear(); myTurrets.clear(); myHouses.clear(); myBuildings.clear();
    enRanged.clear(); enMelee.clear(); enTurrets.clear(); enBuilders.clear(); enBuildings.clear(); enCombat.clear();
    builderBase = rangedBase = meleeBase = -1;
    popCap = popUsed = popSoon = 0;
    for (int i = 0; i < (int)es.size(); i++) {
        const Ent& e = es[i];
        if (e.own == 0) {
            switch (e.type) {
                case BUILDER_UNIT: myBuilders.push_back(i); popUsed++; break;
                case RANGED_UNIT: myRanged.push_back(i); popUsed++; break;
                case MELEE_UNIT: myMelee.push_back(i); popUsed++; break;
                case TURRET: myTurrets.push_back(i); myBuildings.push_back(i); break;
                case HOUSE: myHouses.push_back(i); myBuildings.push_back(i); break;
                case BUILDER_BASE: if (builderBase < 0 || e.active) builderBase = i; myBuildings.push_back(i); break;
                case RANGED_BASE: if (rangedBase < 0 || e.active) rangedBase = i; myBuildings.push_back(i); break;
                case MELEE_BASE: if (meleeBase < 0 || e.active) meleeBase = i; myBuildings.push_back(i); break;
                default: if (e.building()) myBuildings.push_back(i); break;
            }
            if (e.type == HOUSE || e.type == BUILDER_BASE || e.type == RANGED_BASE || e.type == MELEE_BASE) {
                if (e.active) popCap += 5;
                popSoon += 5;
            }
        } else if (e.own > 0) {
            switch (e.type) {
                case BUILDER_UNIT: enBuilders.push_back(i); break;
                case RANGED_UNIT: enRanged.push_back(i); enCombat.push_back(i); break;
                case MELEE_UNIT: enMelee.push_back(i); enCombat.push_back(i); break;
                case TURRET: if (e.active) { enTurrets.push_back(i); enCombat.push_back(i); } enBuildings.push_back(i); break;
                default: enBuildings.push_back(i); break;
            }
        }
    }
    memset(thr5, 0, sizeof thr5);
    memset(thr6, 0, sizeof thr6);
    memset(thrM, 0, sizeof thrM);
    for (int i : enRanged) {
        const Ent& e = es[i];
        around(e.x, e.y, 1, 6, [&](int c) { thr6[c]++; });
        around(e.x, e.y, 1, 5, [&](int c) { thr5[c]++; });
    }
    for (int i : enTurrets) {
        const Ent& e = es[i];
        around(e.x, e.y, 2, 5, [&](int c) { thr6[c]++; thr5[c]++; });
    }
    for (const auto& kv : memB) {
        const MemBuilding& b = kv.second;
        if (b.type != TURRET || b.seen == tickNo) continue;
        around(b.x, b.y, 2, 5, [&](int c) { thr6[c]++; thr5[c]++; });
    }
    for (int i : enMelee) {
        const Ent& e = es[i];
        around(e.x, e.y, 1, 2, [&](int c) { thrM[c]++; });
    }
    for (int c = 0; c < NN; c++) {
        int o = occ[c];
        blkW[c] = wall[c] || (o >= 0 && es[o].own > 0);
        hardBlk[c] = blkW[c];
    }
    if (!roadInit) memset(road, 0, sizeof road), roadInit = true;
    {
        std::vector<int> src;
        for (int i : myBuildings) edgeRing(es[i].x, es[i].y, es[i].size, [&](int c) { if (!wall[c]) src.push_back(c); });
        if (src.empty()) src.push_back(baseCell);
        bfs(src, wall, fieldTerr);
        int terr = (int)KN("TERR", 12);
        for (int c = 0; c < NN; c++) homeZone[c] = fieldTerr[c] <= terr;
    }
    memset(scare, 0, sizeof scare);
    {
        int sr = (int)KN("SCARE_R", 6);
        for (int i : enRanged) around(es[i].x, es[i].y, 1, sr, [&](int c) { scare[c] = 1; });
        for (int i : enMelee) around(es[i].x, es[i].y, 1, 3, [&](int c) { scare[c] = 1; });
        for (int i : enTurrets) around(es[i].x, es[i].y, 2, 6, [&](int c) { scare[c] = 1; });
    }
    evicted.assign(es.size(), 0);
    if (builderBase >= 0) baseCell = cellOf(es[builderBase].x + 2, es[builderBase].y + 2);
    else if (!myBuildings.empty()) baseCell = es[myBuildings[0]].cell;
    else if (!myBuilders.empty()) baseCell = es[myBuilders[0]].cell;
    income = std::max(minedLast, (int)(myBuilders.size() * KN("INC_FLOOR", 0.6)));
    memset(fixedAt, 0, sizeof fixedAt);
    memset(keepOut, 0, sizeof keepOut);
    memset(planArea, 0, sizeof planArea);
    memset(prioRes, 0, sizeof prioRes);
    memset(noIdle, 0, sizeof noIdle);
    memcpy(blkWF, blkW, sizeof blkWF);
    for (int i : myBuildings) {
        const Ent& e = es[i];
        if (e.type == BUILDER_BASE || e.type == RANGED_BASE || e.type == MELEE_BASE)
            around(e.x, e.y, e.size, 1, [&](int c) { noIdle[c] = 1; });
    }
    // unit memory
    for (int i = 0; i < (int)es.size(); i++) {
        const Ent& e = es[i];
        if (e.own != 0 || !e.unit()) continue;
        UnitMem& m = umem[e.id];
        if (m.born < 0) m.born = tickNo;
        if (m.lastCell == e.cell) m.stuck++;
        else m.stuck = 0;
        m.seen = tickNo;
    }
    if (tickNo % 50 == 0)
        for (auto it = umem.begin(); it != umem.end();) it = it->second.seen != tickNo ? umem.erase(it) : std::next(it);
}

// ------------------------------------------------------------------------------------------------ desires
void Brain::setFixed(int i, int act, int a1, int a2, int a3) {
    Desire& d = des[i];
    d.set = true;
    d.fixed = true;
    d.act = act;
    d.a1 = a1;
    d.a2 = a2;
    d.a3 = a3;
    d.n = 1;
    d.cand[0] = es[i].cell;
    d.val[0] = 0;
    fixedAt[es[i].cell] = 1;
    blkWF[es[i].cell] = 1;
}

void Brain::setStay(int i, int pri) {
    setMove(i, pri, [&](int c) {
        bool bad = keepOut[c] || noIdle[c];
        return c == es[i].cell ? (bad ? 60 : 0) : (bad ? 50 : 10);
    });
}

// f(cell) -> value, lower is better; the unit takes the best cell it can get, staying wins ties
template <class F>
void Brain::setMove(int i, int pri, F f) {
    Desire& d = des[i];
    d.set = true;
    d.fixed = false;
    d.act = ACT_NONE;
    d.pri = pri;
    d.cls = pri;
    int c0 = es[i].cell;
    if (fixedAt[c0]) fixedAt[c0] = 0, blkWF[c0] = blkW[c0];
    int x = cx(c0), y = cy(c0);
    d.n = 0;
    d.cand[d.n] = c0;
    d.val[d.n++] = f(c0);
    // a per-unit, per-tick rotation of the neighbour order: equal cells are not always tried in the same order
    int rot = (es[i].id * 7 + tickNo) & 3;
    for (int kk = 0; kk < 4; kk++) {
        int k = (kk + rot) & 3;
        int nx = x + DX[k], ny = y + DY[k];
        if (!inMap(nx, ny)) continue;
        int nc = cellOf(nx, ny);
        if (allowKeepOut ? hardBlk[nc] : blkW[nc]) continue;
        // a cell held by a unit of mine at work stays a candidate: the resolver skips it, and a builder that
        // keeps somebody waiting there is asked to give way
        int v = f(nc);
        if (v >= INF) continue;
        d.cand[d.n] = nc;
        d.val[d.n++] = v;
    }
    for (int a = 1; a < d.n; a++)
        for (int b = a; b > 0 && d.val[b] < d.val[b - 1]; b--) {
            std::swap(d.val[b], d.val[b - 1]);
            std::swap(d.cand[b], d.cand[b - 1]);
        }
}

bool Brain::pibt(int i, int pusherCell, std::vector<int>& unitAt, std::vector<int>& resv, std::vector<unsigned char>& chain, int depth) {
    Desire& d = des[i];
    int c0 = es[i].cell;
    chain[c0] = 1;
    for (int k = 0; k < d.n; k++) {
        int c = d.cand[k];
        if (resv[c] != -1) continue;
        int j = -1;
        if (c != c0) {
            if (c == pusherCell || chain[c]) continue;
            j = unitAt[c];
            if (j >= 0 && des[j].fixed) {
                // pushed against a builder at work: the next in the corridor gives way too (a tick later), and so on down the line
                if (pusherCell >= 0 && es[j].type == BUILDER_UNIT && des[j].act != ACT_BUILD) yieldUntil[es[j].id] = tickNo + (int)KN("YIELD_T", 8);
                continue;
            }
        }
        resv[c] = i;
        d.next = c;
        if (j >= 0 && des[j].next == -1) {
            if (depth > 60 || !pibt(j, c0, unitAt, resv, chain, depth + 1)) {
                // it cannot give way: it stays, for good (asked once, never again: the search stays linear)
                resv[c] = j;
                des[j].next = c;
                d.next = -1;
                continue;
            }
        }
        chain[c0] = 0;
        return true;
    }
    chain[c0] = 0;
    return false;
}

void Brain::moves() {
    std::vector<int> unitAt(NN, -1), resv(NN, -1);
    std::vector<unsigned char> chain(NN, 0);
    std::vector<int> order;
    for (int i = 0; i < (int)es.size(); i++) {
        const Ent& e = es[i];
        if (e.own != 0 || !e.unit()) continue;
        if (!des[i].set) setStay(i, 0);
        unitAt[e.cell] = i;
        if (des[i].fixed) {
            des[i].next = e.cell;
            resv[e.cell] = i;
        } else {
            order.push_back(i);
        }
    }
    for (int i : order) {
        UnitMem& m = umem[es[i].id];
        des[i].pri = des[i].pri * 64 + std::min(m.wait, 60);
    }
    std::sort(order.begin(), order.end(), [&](int a, int b) {
        if (des[a].pri != des[b].pri) return des[a].pri > des[b].pri;
        return es[a].id < es[b].id;
    });
    for (int i : order) {
        if (des[i].next != -1) continue;
        if (!pibt(i, -1, unitAt, resv, chain, 0)) {
            // every candidate is taken: stay (the cell is its own unless a pusher holds it, which cannot be here)
            des[i].next = es[i].cell;
            if (resv[es[i].cell] == -1) resv[es[i].cell] = i;
        }
    }
    for (int i : order) {
        UnitMem& m = umem[es[i].id];
        const Desire& d = des[i];
        bool wanted = d.n > 0 && d.cand[0] != es[i].cell;
        if (wanted && d.next == es[i].cell) m.wait++;
        else m.wait = 0;
        // stuck behind a builder at work (a corridor one cell wide): it gives way, a repairer moves on along its building
        if (m.wait >= (int)KN("YIELD_WAIT", 2) && d.cls >= 3) {
            int j = unitAt[d.cand[0]];
            if (j >= 0 && des[j].fixed && es[j].type == BUILDER_UNIT && des[j].act != ACT_BUILD) yieldUntil[es[j].id] = tickNo + (int)KN("YIELD_T", 8);
        }
    }
    if (tickNo % 64 == 0)
        for (auto it = yieldUntil.begin(); it != yieldUntil.end();) it = it->second < tickNo ? yieldUntil.erase(it) : std::next(it);
    for (int i = 0; i < (int)es.size(); i++)
        if (es[i].own == 0 && es[i].unit()) umem[es[i].id].lastCell = es[i].cell;
}

Action Brain::orders() {
    std::unordered_map<int, EntityAction> out;
    for (int i = 0; i < (int)es.size(); i++) {
        const Ent& e = es[i];
        if (e.own != 0) continue;
        const Desire& d = des[i];
        EntityAction a;
        if (d.act == ACT_ATTACK) {
            a.attackAction = std::make_shared<AttackAction>(std::make_shared<int>(d.a1), nullptr);
        } else if (d.act == ACT_REPAIR) {
            a.repairAction = std::make_shared<RepairAction>(d.a1);
        } else if (d.act == ACT_BUILD) {
            a.buildAction = std::make_shared<BuildAction>((EntityType)d.a1, Vec2Int(d.a2, d.a3));
        } else if (e.unit() && d.next >= 0 && d.next != e.cell) {
            a.moveAction = std::make_shared<MoveAction>(Vec2Int(cx(d.next), cy(d.next)), false, false);
        }
        out[e.id] = a;
    }
    return Action(out);
}

// ------------------------------------------------------------------------------------------------ fire
// Shots are simultaneous and exact: what I see is what the engine shoots at. So every target gets just
// the shots that kill it, the scarce targets first, and the rest of the guns add damage where it counts.
void Brain::fire() {
    struct Tg {
        int idx, rem, cls, need;
        std::vector<int> sh;
    };
    std::vector<Tg> tg;
    std::unordered_map<int, int> tgOf;
    std::vector<int> shooters;
    std::vector<std::vector<int>> opts;
    auto classOf = [&](const Ent& e) {
        switch (e.type) {
            case RANGED_UNIT: return 0;
            case MELEE_UNIT: return 1;
            case TURRET: return e.active ? 2 : 5;
            case BUILDER_UNIT: return 3;
            case RANGED_BASE: return 4;
            case BUILDER_BASE: return 6;
            case MELEE_BASE: return 7;
            case HOUSE: return 6;
            default: return 9;
        }
    };
    auto addShooter = [&](int i, int range) {
        const Ent& s = es[i];
        std::vector<int> found;
        around(s.x, s.y, s.size, range, [&](int c) {
            int o = occ[c];
            if (o >= 0 && es[o].own > 0) found.push_back(o);
        });
        if (found.empty()) return;
        std::sort(found.begin(), found.end());
        found.erase(std::unique(found.begin(), found.end()), found.end());
        int si = (int)shooters.size();
        shooters.push_back(i);
        opts.emplace_back();
        for (int o : found) {
            auto it = tgOf.find(o);
            int t;
            if (it == tgOf.end()) {
                t = (int)tg.size();
                tgOf[o] = t;
                tg.push_back(Tg{o, es[o].hp, classOf(es[o]), (es[o].hp + 4) / 5, {}});
            } else {
                t = it->second;
            }
            tg[t].sh.push_back(si);
            opts[si].push_back(t);
        }
    };
    for (int i : myRanged) addShooter(i, 5);
    for (int i : myTurrets) if (es[i].active) addShooter(i, 5);
    for (int i : myMelee) addShooter(i, 1);
    if (shooters.empty()) return;
    std::vector<int> assigned(shooters.size(), -1);
    std::vector<int> ord(tg.size());
    for (size_t k = 0; k < ord.size(); k++) ord[k] = (int)k;
    std::sort(ord.begin(), ord.end(), [&](int a, int b) {
        if (tg[a].cls != tg[b].cls) return tg[a].cls < tg[b].cls;
        if (tg[a].need != tg[b].need) return tg[a].need < tg[b].need;
        if (tg[a].sh.size() != tg[b].sh.size()) return tg[a].sh.size() < tg[b].sh.size();
        return es[tg[a].idx].id < es[tg[b].idx].id;
    });
    // kills
    for (int t : ord) {
        Tg& g = tg[t];
        std::vector<int> avail;
        for (int si : g.sh)
            if (assigned[si] < 0) avail.push_back(si);
        if ((int)avail.size() < g.need) continue;
        std::sort(avail.begin(), avail.end(), [&](int a, int b) {
            if (opts[a].size() != opts[b].size()) return opts[a].size() < opts[b].size();
            return a < b;
        });
        for (int k = 0; k < g.need; k++) {
            assigned[avail[k]] = t;
            g.rem -= 5;
        }
    }
    // damage
    for (size_t si = 0; si < shooters.size(); si++) {
        if (assigned[si] >= 0) continue;
        int best = -1;
        for (int t : opts[si]) {
            if (tg[t].rem <= 0) continue;
            if (best < 0 || tg[t].cls < tg[best].cls || (tg[t].cls == tg[best].cls && tg[t].rem < tg[best].rem)) best = t;
        }
        if (best >= 0) {
            assigned[si] = best;
            tg[best].rem -= 5;
        }
    }
    for (size_t si = 0; si < shooters.size(); si++) {
        if (assigned[si] < 0) continue;
        int i = shooters[si];
        if (es[i].unit()) setFixed(i, ACT_ATTACK, es[tg[assigned[si]].idx].id);
        else {
            des[i].set = true;
            des[i].act = ACT_ATTACK;
            des[i].a1 = es[tg[assigned[si]].idx].id;
        }
    }
    for (const Tg& g : tg)
        if (g.rem <= 0) memU.erase(es[g.idx].id);
}

// ------------------------------------------------------------------------------------------------ sites

bool Brain::cellFreeForSite(int c) const {
    if (wall[c] || planArea[c] || (road[c] && !siteOnRoad)) return false;
    if (fog && lastSeen[c] < 0) return false;
    int o = occ[c];
    if (o >= 0 && es[o].own > 0) return false;
    return true;
}

// the free arcs of the ring around a square: one arc = building here cuts nothing apart
int Brain::ringArcs(int x, int y, int s) const {
    int ring[4 * 5 + 4];
    int n = 0;
    auto put = [&](int ax, int ay) { ring[n++] = (inMap(ax, ay) && !wall[cellOf(ax, ay)] && !planArea[cellOf(ax, ay)]) ? 1 : 0; };
    for (int i = -1; i <= s; i++) put(x + i, y - 1);
    for (int j = 0; j <= s; j++) put(x + s, y + j);
    for (int i = s - 1; i >= -1; i--) put(x + i, y + s);
    for (int j = s - 1; j >= 0; j--) put(x - 1, y + j);
    int arcs = 0, freeN = 0;
    for (int k = 0; k < n; k++) {
        freeN += ring[k];
        if (ring[k] && !ring[(k + n - 1) % n]) arcs++;
    }
    if (freeN == n) return 1;
    return arcs;
}

bool Brain::siteValid(int x, int y, int s, int gap) const {
    if (x < 0 || y < 0 || x + s > N || y + s > N) return false;
    for (int dy = 0; dy < s; dy++)
        for (int dx = 0; dx < s; dx++)
            if (!cellFreeForSite(cellOf(x + dx, y + dy))) return false;
    auto ban = siteBan.find(cellOf(x, y) * 8 + s);
    if (ban != siteBan.end() && ban->second > tickNo) return false;
    if (ringArcs(x, y, s) != 1) return false;
    int freeEdge = 0;
    bool touch = false;
    edgeRing(x, y, s, [&](int c) {
        if (!wall[c] && !planArea[c]) freeEdge++;
        if (wall[c] == 2 || planArea[c]) touch = true;
    });
    if (freeEdge < s) return false;
    if (gap >= 1) {
        if (touch) return false;
        const int cxs[4] = {x - 1, x + s, x - 1, x + s}, cys[4] = {y - 1, y - 1, y + s, y + s};
        for (int k = 0; k < 4; k++)
            if (inMap(cxs[k], cys[k]) && (wall[cellOf(cxs[k], cys[k])] == 2 || planArea[cellOf(cxs[k], cys[k])])) return false;
    }
    if (gap >= 2) {
        for (int dy = -2; dy < s + 2; dy++)
            for (int dx = -2; dx < s + 2; dx++) {
                if (!inMap(x + dx, y + dy)) continue;
                int c = cellOf(x + dx, y + dy);
                if (wall[c] == 2 || planArea[c]) return false;
            }
    }
    return true;
}

bool Brain::pickSite(int type, int& outX, int& outY) {
    siteOnRoad = type == RANGED_BASE;       // the road is the army's: its base may stand on it, houses may not
    int s = SIZE_OF[type];
    int bx = cx(baseCell), by = cy(baseCell);
    int K = type == HOUSE ? (int)KN("HOUSE_K", 3) : 8;
    std::vector<int> bpos;
    for (int i : myBuilders) bpos.push_back(es[i].cell);
    double best = 1e18;
    bool found = false;
    int R = type == HOUSE ? (int)KN("HOUSE_R", 26) : 24;
    int gap0 = type == HOUSE ? (int)KN("HOUSE_GAP", 1) : 1;
    for (int gap = gap0; gap >= 0 && !found; gap--) {
        for (int y = std::max(0, by - R); y <= std::min(N - s, by + R); y++)
            for (int x = std::max(0, bx - R); x <= std::min(N - s, bx + R); x++) {
                if (!siteValid(x, y, s, gap)) continue;
                int reach = INF, resAdj = 0;
                edgeRing(x, y, s, [&](int c) {
                    reach = std::min(reach, fieldBase[c]);
                    if (wall[c] == 1) resAdj++;
                });
                if (reach >= INF) continue;
                int nearB = INF;
                edgeRing(x, y, s, [&](int c) { nearB = std::min(nearB, fieldNearB[c]); });
                if (type != RANGED_BASE && nearB > (int)KN("SITE_NEARB", 14)) continue;      // nobody near enough by foot
                // the walk of the K nearest builders
                int near[10];
                int nn = 0;
                for (int c : bpos) {
                    int d = sqDist(cx(c), cy(c), x, y, s);
                    int k = nn < K ? nn++ : (d < near[K - 1] ? K - 1 : -1);
                    if (k < 0) continue;
                    near[k] = d;
                    for (; k > 0 && near[k] < near[k - 1]; k--) std::swap(near[k], near[k - 1]);
                }
                double walk = 0;
                for (int k = 0; k < nn; k++) walk += near[k];
                double sc;
                int mx = x + s / 2, my = y + s / 2;
                if (type == HOUSE) {
                    int dBase = std::max(0, sqDist(bx, by, x, y, s) - (int)KN("HS_CORE", 6));
                    int front = std::max(0, (mx + my) - (bx + by));
                    int thrPen = thr6[cellOf(mx, my)] ? 200 : 0;
                    sc = walk * KN("HS_WALK", 1.0) + dBase * KN("HS_BASE", 1.0) + resAdj * KN("HS_RES", 1.5) + nearB * KN("HS_NEARB", 1.0) + front * KN("HS_FRONT", 0.1) + reach * 0.3 + thrPen;
                } else {
                    int ix = bx + (int)KN("RB_DX", 8), iy = by + (int)KN("RB_DX", 8);
                    sc = (std::abs(mx - ix) + std::abs(my - iy)) * 2.0 + walk * 0.3 + reach * 0.5;
                }
                if (sc < best) {
                    best = sc;
                    outX = x;
                    outY = y;
                    found = true;
                }
            }
    }
    return found;
}

// no free land for a base: the 5x5 with the least to mine away
bool Brain::pickClearSite(int& outX, int& outY, int hpLimit) {
    int s = 5;
    int bx = cx(baseCell), by = cy(baseCell);
    int ix = bx + (int)KN("RB_DX", 8), iy = by + (int)KN("RB_DX", 8);
    double best = 1e18;
    bool found = false;
    for (int y = std::max(0, by - 20); y <= std::min(N - s, by + 20); y++)
        for (int x = std::max(0, bx - 20); x <= std::min(N - s, bx + 20); x++) {
            int hp = 0;
            bool ok = true;
            for (int dy = 0; dy < s && ok; dy++)
                for (int dx = 0; dx < s; dx++) {
                    int c = cellOf(x + dx, y + dy);
                    if (wall[c] == 2 || planArea[c] || (fog && lastSeen[c] < 0)) {
                        ok = false;
                        break;
                    }
                    if (wall[c] == 1) hp += std::max(resHp[c], 1);
                }
            if (!ok) continue;
            bool touch = false;
            int reach = INF;
            edgeRing(x, y, s, [&](int c) {
                if (wall[c] == 2 || planArea[c]) touch = true;
                reach = std::min(reach, fieldBase[c]);
            });
            if (touch || reach >= INF) continue;
            if (hp > hpLimit) continue;
            double sc = hp / 2.0 + (std::abs(x + 2 - ix) + std::abs(y + 2 - iy)) * 1.5;
            if (sc < best) {
                best = sc;
                outX = x;
                outY = y;
                found = true;
            }
        }
    return found;
}

int Brain::unitCost(int type) const {
    if (type == BUILDER_UNIT) return 10 + (int)myBuilders.size();
    if (type == RANGED_UNIT) return 30 + (int)myRanged.size();
    if (type == MELEE_UNIT) return 20 + (int)myMelee.size();
    if (type == HOUSE || type == TURRET) return 50;
    if (type == WALL) return 10;
    return 500;
}

// ------------------------------------------------------------------------------------------------ planning
void Brain::planning() {
    // plans that became buildings, or went stale
    for (size_t k = 0; k < plans.size();) {
        Plan& p = plans[k];
        int o = occ[cellOf(p.x, p.y)];
        bool placed = o >= 0 && es[o].own == 0 && es[o].type == p.type && es[o].x == p.x && es[o].y == p.y;
        bool drop = false;
        if (placed) {
            for (int i : myBuilders) {
                UnitMem& m = umem[es[i].id];
                if (m.job == p.key) m.job = es[o].id;
            }
            drop = true;
        } else {
            int limit = p.type == HOUSE ? (int)KN("PLAN_TTL", 30) : 400;
            bool bad = false;
            int resIn = 0;
            for (int dy = 0; dy < p.size; dy++)
                for (int dx = 0; dx < p.size; dx++) {
                    int c = cellOf(p.x + dx, p.y + dy);
                    if (wall[c] == 2) bad = true;
                    if (wall[c] == 1) resIn++;
                    int oo = occ[c];
                    if (oo >= 0 && es[oo].own > 0 && es[oo].unit() && es[oo].type != BUILDER_UNIT) bad = true;
                }
            if (p.clearing && resIn == 0) p.clearing = false, p.born = tickNo;
            if (myRes < unitCost(p.type)) p.born = std::max(p.born, tickNo - 5);
            if (!p.clearing && resIn > 0) bad = true;
            auto pf = planFail.find(p.key);
            if (pf != planFail.end() && pf->second >= (int)KN("PLAN_FAIL", 4)) bad = true;
            if (bad || tickNo - p.born > limit) {
                planFail.erase(p.key);
                siteBan[cellOf(p.x, p.y) * 8 + p.size] = tickNo + 150;
                drop = true;
            }
        }
        if (drop) plans.erase(plans.begin() + k);
        else k++;
    }
    int housePlans = 0;
    bool rbPlan = false;
    for (const Plan& p : plans) {
        for (int dy = 0; dy < p.size; dy++)
            for (int dx = 0; dx < p.size; dx++) {
                int c = cellOf(p.x + dx, p.y + dy);
                planArea[c] = 1;
                if (!p.clearing) keepOut[c] = 1;
                if (wall[c] == 1) prioRes[c] = 1;
            }
        if (p.type == HOUSE) housePlans++;
        if (p.type == RANGED_BASE) rbPlan = true;
    }
    int nB = (int)myBuilders.size();
    if (nB == 0) return;
    // ---- the ranged base
    bool haveRB = rangedBase >= 0;
    // its land is kept from the start: on a cramped map my own houses would take the last free 5x5
    if (!haveRB && !rbPlan && builderBase >= 0) {
        bool ok = rbResX >= 0, clean = true;
        for (int dy = 0; dy < 5 && ok; dy++)
            for (int dx = 0; dx < 5; dx++) {
                int c = cellOf(rbResX + dx, rbResY + dy);
                if (wall[c] == 2 || planArea[c]) {
                    ok = false;
                    break;
                }
                if (wall[c] == 1) clean = false;
            }
        // free land if there is any; else the square near the base with the least to mine away is kept out of the
        // houses' hands (the miners work there anyway), and swapped for free land when some appears
        if ((!ok || !clean) && tickNo % 10 == 3) {
            int x, y;
            if (pickSite(RANGED_BASE, x, y)) rbResX = x, rbResY = y;
            else if (!ok && KN("RES_CLEAR", 1) > 0.5 && tickNo >= (int)KN("RES_CLEAR_T", 80) && pickClearSite(x, y, (int)KN("RES_CLEAR_HP", 450))) rbResX = x, rbResY = y;
            else if (!ok) rbResX = rbResY = -1;
        } else if (!ok) {
            rbResX = rbResY = -1;
        }
        if (rbResX >= 0)
            for (int dy = 0; dy < 5; dy++)
                for (int dx = 0; dx < 5; dx++) planArea[cellOf(rbResX + dx, rbResY + dy)] = 1;
    } else {
        rbResX = rbResY = -1;
    }
    if (rbPlan && tickNo % 8 == 0) {
        for (size_t k = 0; k < plans.size(); k++) {
            Plan& p = plans[k];
            if (p.type != RANGED_BASE || !p.clearing) continue;
            // free land may have appeared (mined out, or seen for the first time): take it instead
            for (int dy = 0; dy < 5; dy++)
                for (int dx = 0; dx < 5; dx++) planArea[cellOf(p.x + dx, p.y + dy)] = 0;
            int x, y;
            if (pickSite(RANGED_BASE, x, y)) {
                for (int dy = 0; dy < 5; dy++)
                    for (int dx = 0; dx < 5; dx++) prioRes[cellOf(p.x + dx, p.y + dy)] = 0;
                p.x = x;
                p.y = y;
                p.clearing = false;
                p.born = tickNo;
                for (int dy = 0; dy < 5; dy++)
                    for (int dx = 0; dx < 5; dx++) keepOut[cellOf(x + dx, y + dy)] = planArea[cellOf(x + dx, y + dy)] = 1;
            } else {
                for (int dy = 0; dy < 5; dy++)
                    for (int dx = 0; dx < 5; dx++) planArea[cellOf(p.x + dx, p.y + dy)] = 1;
            }
        }
    }
    if (!haveRB && !rbPlan && builderBase >= 0 && rbResX < 0 && nB >= (int)KN("RB_AT", 50) - (int)KN("RB_CLEAR_LEAD", 14)) {
        int x, y;
        if (pickClearSite(x, y, 100000)) {
            plans.push_back(Plan{-(++planSerial), RANGED_BASE, x, y, 5, tickNo, true});
            rbPlan = true;
            for (int dy = 0; dy < 5; dy++)
                for (int dx = 0; dx < 5; dx++) {
                    int c = cellOf(x + dx, y + dy);
                    planArea[c] = 1;
                    if (wall[c] == 1) prioRes[c] = 1;
                }
        }
    }
    if (!haveRB && !rbPlan && builderBase >= 0) {
        bool want = nB >= (int)KN("RB_AT", 50) || tickNo >= (int)KN("RB_TICK", 230) || (tickNo - lastHomeThreat < 100 && nB >= 8);
        if (want && myRes + income * 30 >= 500) {
            int x, y;
            if (rbResX >= 0) {
                bool res = false;
                for (int dy = 0; dy < 5; dy++)
                    for (int dx = 0; dx < 5; dx++)
                        if (wall[cellOf(rbResX + dx, rbResY + dy)] == 1) res = true;
                plans.push_back(Plan{-(++planSerial), RANGED_BASE, rbResX, rbResY, 5, tickNo, res});
            } else if (pickSite(RANGED_BASE, x, y)) {
                plans.push_back(Plan{-(++planSerial), RANGED_BASE, x, y, 5, tickNo, false});
            } else if (pickClearSite(x, y, (int)KN("CLEAR_HP", 200))) {
                plans.push_back(Plan{-(++planSerial), RANGED_BASE, x, y, 5, tickNo, true});
            }
            if (!plans.empty() && plans.back().type == RANGED_BASE) {
                const Plan& p = plans.back();
                for (int dy = 0; dy < 5; dy++)
                    for (int dx = 0; dx < 5; dx++) {
                        int c = cellOf(p.x + dx, p.y + dy);
                        planArea[c] = 1;
                        if (!p.clearing) keepOut[c] = 1;
                        if (wall[c] == 1) prioRes[c] = 1;
                    }
            }
        }
    }
    // ---- houses
    int pendingPop = (popSoon - popCap) + 5 * housePlans;
    int producers = 0;
    double costSum = 0;
    if (builderBase >= 0 && es[builderBase].active && nB < (int)KN("B_MAX", 64)) producers++, costSum += unitCost(BUILDER_UNIT);
    if (rangedBase >= 0 || rbPlan) producers++, costSum += unitCost(RANGED_UNIT);
    double rate = producers ? std::min((double)producers, income / (costSum / producers + KN("HOUSE_SHARE", 20))) : 0;
    // money in the bank: the bases can turn out a unit a tick each, the houses must be there for them
    {
        int spare = myRes;
        for (const Plan& p : plans)
            if (p.type != HOUSE) spare -= unitCost(p.type);       // the base's money is not spare
        if (!haveRB && nB >= (int)KN("RB_AT", 50) - 6) spare -= 500;
        rich = spare >= (int)KN("HOUSE_RICH", 150);
    }
    if (rich && producers) rate = std::max(rate, producers * KN("RICH_RATE", 0.8));
    prodRate = rate;
    int need = popUsed + (int)std::ceil(rate * KN("HOUSE_LEAD", 18)) + (int)KN("HOUSE_SLACK", 1);
    int maxPar = std::max(1, (int)(nB * KN("HOUSE_WORK", 0.36) / houseK()));
    if (nB < 5 && popCap <= 5) maxPar = 0;          // the opening: five builders first
    int pendingHouses = pendingPop / 5;
    int popMax = (int)KN("POP_MAX", 260);
    int guard = 0;
    int unplaced = housePlans;
    while (need > popCap + pendingPop && pendingHouses < maxPar && popCap + pendingPop < popMax && guard++ < 3) {
        // only what can be paid within a few ticks is worth a plan
        if (myRes + income * (int)KN("HOUSE_FUND_T", 12) < 50 * (unplaced + 1)) break;
        unplaced++;
        int x, y;
        if (!pickSite(HOUSE, x, y)) break;
        plans.push_back(Plan{-(++planSerial), HOUSE, x, y, 3, tickNo, false});
        for (int dy = 0; dy < 3; dy++)
            for (int dx = 0; dx < 3; dx++) keepOut[cellOf(x + dx, y + dy)] = planArea[cellOf(x + dx, y + dy)] = 1;
        pendingPop += 5;
        pendingHouses++;
    }
}

// ------------------------------------------------------------------------------------------------ jobs
void Brain::buildJobs() {
    jobs.clear();
    for (const Plan& p : plans) {
        if (p.clearing) {
            if (debug >= 2 && tickNo % 5 == 0) fprintf(stderr, "  plan base (%d,%d) clearing, age %d\n", p.x, p.y, tickNo - p.born);
            continue;
        }
        int need = p.type == HOUSE ? houseK() + (rich ? (int)KN("RICH_K", 1) : 0) : (int)KN("BASE_K", 10);
        jobs.push_back(Job{p.key, p.type, p.x, p.y, p.size, false, -1, need, unitCost(p.type), {}});
    }
    bool calm = enCombat.empty() || tickNo - lastHomeThreat > 30;
    for (int i : myBuildings) {
        const Ent& e = es[i];
        if (!e.active) {
            int need = e.type == HOUSE ? houseK() + (rich ? (int)KN("RICH_K", 1) : 0) : e.type == TURRET ? 4 : (int)KN("BASE_K", 10);
            jobs.push_back(Job{e.id, e.type, e.x, e.y, e.size, true, i, need, 0, {}});
        } else if (e.hp < MAXHP_OF[e.type] && calm) {
            int need = e.type == HOUSE ? 1 : 3;
            jobs.push_back(Job{e.id, e.type, e.x, e.y, e.size, true, i, need, 0, {}});
        }
    }
    if (jobs.size() > 8) jobs.resize(8);
}

// ------------------------------------------------------------------------------------------------ economy
void Brain::economy() {
    statMine = statWalk = statJob = statFlee = statIdle = 0;
    memcpy(blkWF, blkW, sizeof blkWF);
    for (int c = 0; c < NN; c++)
        if (fixedAt[c]) blkWF[c] = 1;
    std::vector<int> src;
    if (builderBase >= 0)
        edgeRing(es[builderBase].x, es[builderBase].y, 5, [&](int c) { if (!blkW[c]) src.push_back(c); });
    if (src.empty()) src.push_back(baseCell);
    bfs(src, blkW, fieldBase);
    for (int i : enCombat)
        if (homeZone[es[i].cell]) lastHomeThreat = tickNo;
    {
        std::vector<int> bsrc;
        for (int i : myBuilders) bsrc.push_back(es[i].cell);
        if (bsrc.empty()) bsrc.push_back(baseCell);
        bfs(bsrc, blkW, fieldNearB);
    }
    planning();
    {
        std::vector<int> q;
        std::fill(exitD, exitD + NN, INF);
        for (int c = 0; c < NN; c++) {
            if (!keepOut[c]) continue;
            blkW[c] = blkWF[c] = 1;
            int x = cx(c), y = cy(c);
            for (int k = 0; k < 4; k++) {
                int nx = x + DX[k], ny = y + DY[k];
                if (inMap(nx, ny) && !keepOut[cellOf(nx, ny)] && !hardBlk[cellOf(nx, ny)]) exitD[c] = 1;
            }
            if (exitD[c] == 1) q.push_back(c);
        }
        for (size_t h = 0; h < q.size(); h++) {
            int c = q[h], x = cx(c), y = cy(c);
            for (int k = 0; k < 4; k++) {
                int nx = x + DX[k], ny = y + DY[k];
                if (!inMap(nx, ny)) continue;
                int nc = cellOf(nx, ny);
                if (keepOut[nc] && !hardBlk[nc] && exitD[nc] == INF) exitD[nc] = exitD[c] + 1, q.push_back(nc);
            }
        }
        allowKeepOut = true;
        for (int i = 0; i < (int)es.size(); i++) {
            const Ent& e = es[i];
            if (e.own != 0 || !e.unit() || !keepOut[e.cell] || des[i].fixed) continue;
            evicted[i] = 1;
            int restless = umem[e.id].wait >= (int)KN("EVICT_PATIENCE", 4) ? 15 : 0;
            setMove(i, 7, [&](int c) { return keepOut[c] ? 100 + std::min(exitD[c], 50) * 10 + (c == e.cell ? restless : 0) : std::min(fieldBase[c], 90); });
        }
        allowKeepOut = false;
    }
    buildJobs();

    // ---- builders under fire step away
    std::vector<int> avail;
    for (int i : myBuilders) {
        int c = es[i].cell;
        if (evicted[i]) {
            umem[es[i].id].job = 0;
            continue;
        }
        if (thr6[c] > 0 || thrM[c] > 0 || scare[c]) {
            umem[es[i].id].job = 0;
            statFlee++;
            setMove(i, 6, [&](int nc) {
                int near = 99;
                for (int j : enCombat) near = std::min(near, sqDist(cx(nc), cy(nc), es[j].x, es[j].y, es[j].size));
                return thr5[nc] * 300 + thr6[nc] * 100 + thrM[nc] * 200 + (scare[nc] ? 40 : 0) - std::min(near, 12) * 10 + std::min(fieldBase[nc], 60) + (keepOut[nc] ? 5 : 0);
            });
        } else {
            avail.push_back(i);
        }
    }

    // ---- build and repair jobs
    std::unordered_map<long long, int> jobIdx;
    for (size_t j = 0; j < jobs.size(); j++) jobIdx[jobs[j].key] = (int)j;
    std::vector<char> taken(es.size(), 0);
    int waitMined = 0;
    for (int i : avail) {
        UnitMem& m = umem[es[i].id];
        if (m.job == 0) continue;
        auto it = jobIdx.find(m.job);
        if (it != jobIdx.end() && (int)jobs[it->second].workers.size() < jobs[it->second].need) {
            jobs[it->second].workers.push_back(i);
            taken[i] = 1;
        } else {
            m.job = 0;
        }
    }
    for (size_t jj = 0; jj < jobs.size(); jj++) {
        Job& jb = jobs[jj];
        int* field = jobField[jj];
        int shortfall = jb.placed ? 0 : std::max(0, jb.cost - myRes);
        bool funded = true;
        bool siteClear = true;
        if (!jb.placed)
            for (int dy = 0; dy < jb.size && siteClear; dy++)
                for (int dx = 0; dx < jb.size; dx++)
                    if (occ[cellOf(jb.x + dx, jb.y + dy)] >= 0) {
                        siteClear = false;
                        break;
                    }
        bool placedNow = false;
        std::vector<int> walking;
        std::vector<int> movingOn;
        for (int w : jb.workers) {
            const Ent& e = es[w];
            auto yl = yieldUntil.find(e.id);
            if (yl != yieldUntil.end() && yl->second >= tickNo && sqDist(e.x, e.y, jb.x, jb.y, jb.size) == 1) {
                movingOn.push_back(w);
                continue;
            }
            if (sqDist(e.x, e.y, jb.x, jb.y, jb.size) == 1) {
                if (jb.placed) {
                    setFixed(w, ACT_REPAIR, es[jb.entIdx].id);
                } else if (!placedNow && myRes >= jb.cost && siteClear) {
                    setFixed(w, ACT_BUILD, jb.type, jb.x, jb.y);
                    myRes -= jb.cost;
                    placedNow = true;
                } else if (!siteClear) {
                    setMove(w, 1, [&](int c) { return c == e.cell ? 0 : (keepOut[c] ? 60 : 10 + (sqDist(cx(c), cy(c), jb.x, jb.y, jb.size) == 1 ? 0 : 5)); });
                    continue;
                } else {
                    int rid = -1;
                    for (int k = 0; k < 4; k++) {
                        int nx = e.x + DX[k], ny = e.y + DY[k];
                        if (!inMap(nx, ny)) continue;
                        int o = occ[cellOf(nx, ny)];
                        if (o >= 0 && es[o].type == RESOURCE) rid = es[o].id;
                    }
                    if (rid >= 0) setFixed(w, ACT_ATTACK, rid), waitMined++;
                    else setFixed(w, ACT_NONE);
                }
                blkWF[e.cell] = 1;
            } else {
                walking.push_back(w);
            }
        }
        std::vector<int> ring;
        edgeRing(jb.x, jb.y, jb.size, [&](int c) { if (!blkWF[c] && !keepOut[c]) ring.push_back(c); });
        if (ring.empty()) {
            for (int w : walking) umem[es[w].id].job = 0, taken[w] = 0;
            continue;
        }
        bfs(ring, blkWF, field);
        if (funded && (int)jb.workers.size() < jb.need) {
            std::vector<std::pair<int, int>> cand;
            int maxD = jb.type == HOUSE ? (int)KN("HOUSE_MAXD", 30) : 45;
            for (int i : avail)
                if (!taken[i] && field[es[i].cell] < maxD) cand.push_back({field[es[i].cell], i});
            std::sort(cand.begin(), cand.end());
            for (size_t k = 0; k < cand.size() && (int)jb.workers.size() < jb.need; k++) {
                int i = cand[k].second;
                if (shortfall > 0) {       // it would only stand and wait there: the miners left behind must make the money by then
                    int inc = std::max(0, minedLast - (int)jb.workers.size() - 1);
                    if (shortfall > inc * cand[k].first) break;
                }
                // helpers come only as far as the building is worth it: the nearest places it, the rest save their walk
                if (!jb.workers.empty() && jb.type == HOUSE && cand[k].first > (int)KN("HOUSE_HELP_D", 30)) break;
                taken[i] = 1;
                umem[es[i].id].job = jb.key;
                jb.workers.push_back(i);
                walking.push_back(i);
            }
        }
        if (!jb.placed) {
            if (jb.workers.empty() && shortfall == 0) planFail[jb.key]++;
            else planFail.erase(jb.key);
        }
        if (debug >= 2 && !jb.placed && tickNo % 5 == 0)
            fprintf(stderr, "  plan %s (%d,%d) res=%d cost=%d workers=%d walking=%d clear=%d ring=%d\n", jb.type == HOUSE ? "house" : "base", jb.x, jb.y, myRes, jb.cost,
                    (int)jb.workers.size(), (int)walking.size(), (int)siteClear, (int)ring.size());
        static int fieldThrough[NN];
        bool haveThrough = false;
        for (int w : walking) {
            if (field[es[w].cell] >= INF) {
                // no way round the builders at work: the way through them (they give way when it waits behind them)
                if (!haveThrough) bfs(ring, blkW, fieldThrough), haveThrough = true;
                if (fieldThrough[es[w].cell] >= INF) {
                    umem[es[w].id].job = 0;
                    taken[w] = 0;
                    continue;
                }
                setMove(w, 4, [&](int c) { return fieldThrough[c] >= INF ? INF : fieldThrough[c] * 4 + (keepOut[c] ? 3 : 0); });
                continue;
            }
            setMove(w, 4, [&](int c) { return field[c] >= INF ? INF : field[c] * 4 + (keepOut[c] ? 3 : 0); });
        }
        // who was asked to give way: to another place on the ring if one is next to it, else a step off
        for (int w : movingOn)
            setMove(w, 4, [&](int c) { return (field[c] >= INF ? 40 : field[c] * 4) + (c == es[w].cell ? 8 : 0) + (keepOut[c] ? 100 : 0); });
        if (!jb.placed && jb.workers.empty() && funded && tickNo - 0 > 0) {
            // nobody can come: the plan dies in planning() by its age
        }
    }
    std::vector<int> rest;
    for (int i : avail) {
        if (taken[i]) {
            statJob++;
            umem[es[i].id].lastRole = 3;
        } else {
            rest.push_back(i);
        }
    }
    mining(rest);
    minedLast += waitMined;
    cumMine += statMine; cumWalk += statWalk; cumJob += statJob; cumFlee += statFlee; cumIdle += statIdle;
    cumB += (long long)myBuilders.size();
}

void Brain::mining(std::vector<int>& freeB) {
    std::vector<int> walkers;
    int mined = 0;
    for (int i : freeB) {
        const Ent& e = es[i];
        int best = -1, bestKey = INF;
        auto yl = yieldUntil.find(e.id);
        bool yielding = yl != yieldUntil.end() && yl->second >= tickNo;
        if (!keepOut[e.cell] && !yielding) {
            for (int k = 0; k < 4; k++) {
                int nx = e.x + DX[k], ny = e.y + DY[k];
                if (!inMap(nx, ny)) continue;
                int nc = cellOf(nx, ny), o = occ[nc];
                if (o < 0 || es[o].type != RESOURCE) continue;
                int key = (prioRes[nc] ? -1000 : 0) + es[o].hp;
                if (key < bestKey) bestKey = key, best = o;
            }
        }
        if (best >= 0) {
            setFixed(i, ACT_ATTACK, es[best].id);
            blkWF[e.cell] = 1;
            mined++;
            {
                UnitMem& m = umem[e.id];
                if (debug && m.walkRun > 0) {
                    int b = m.walkRun <= 1 ? 0 : m.walkRun <= 3 ? 1 : m.walkRun <= 7 ? 2 : m.walkRun <= 15 ? 3 : m.walkRun <= 30 ? 4 : 5;
                    walkHist[b]++;
                    walkHistT[b] += m.walkRun;
                }
                m.walkRun = 0;
                m.lastRole = 1;
            }
        } else {
            walkers.push_back(i);
            {
                UnitMem& m = umem[e.id];
                if (m.walkRun == 0) (m.lastRole == 1 ? walkAfterMine : m.lastRole == 3 ? walkAfterJob : walkNew)++;
                m.walkRun++;
                m.lastRole = 2;
            }
        }
    }
    minedLast = mined;
    statMine = mined;

    // the open slots: free cells beside a resource
    static std::vector<int> stamp(NN, -1);
    std::vector<std::pair<int, int>> open;
    int prioOff = 3;
    for (const Plan& p : plans)
        if (p.clearing) prioOff = (int)KN("CLEAR_PULL", 10);
    for (int c = 0; c < NN; c++) {
        if (wall[c] != 1) continue;
        int x = cx(c), y = cy(c);
        for (int k = 0; k < 4; k++) {
            int nx = x + DX[k], ny = y + DY[k];
            if (!inMap(nx, ny)) continue;
            int nc = cellOf(nx, ny);
            if (blkWF[nc] || keepOut[nc]) continue;
            int off = prioRes[c] ? 0 : prioOff;
            if (thr6[nc] > 0 || thrM[nc] > 0 || scare[nc]) off += 40;
            if (!vis[nc]) off += 3;
            if (lastSeen[nc] < 0 && fog) off += 5;
            if (stamp[nc] == tickNo) {
                // keep the smaller offset
                for (auto& s : open)
                    if (s.second == nc) {
                        s.first = std::min(s.first, off);
                        break;
                    }
                continue;
            }
            stamp[nc] = tickNo;
            open.push_back({off, nc});
        }
    }
    static std::vector<int> bw(NN, -1), slotTaken(NN, -1);
    std::vector<int> un = walkers;
    int rounds = 0;
    bool first = true;
    std::fill(fieldA, fieldA + NN, INF);
    static unsigned char blkMine[NN];
    for (int c = 0; c < NN; c++) blkMine[c] = blkWF[c] || thr6[c] > 0 || thrM[c] > 0;
    {
        std::vector<std::pair<int, int>> safe;
        for (auto& sl : open)
            if (!blkMine[sl.second]) safe.push_back(sl);
        open.swap(safe);
    }
    int maxRounds = lowPower ? 3 : (int)KN("MINE_ROUNDS", 16);
    while (!open.empty() && (first || !un.empty()) && rounds < maxRounds) {
        bfsOff(open, blkMine, fieldSlot, labelSlot);
        if (first) memcpy(fieldA, fieldSlot, sizeof fieldA);   // the spawn field: the way to the nearest open slot
        first = false;
        if (un.empty()) break;
        std::vector<int> labels;
        for (int w : un) {
            int c = es[w].cell, d = fieldSlot[c];
            if (d >= INF) continue;
            int l = labelSlot[c];
            if (bw[l] < 0) {
                bw[l] = w;
                labels.push_back(l);
            } else if (d < fieldSlot[es[bw[l]].cell]) {
                bw[l] = w;
            }
        }
        if (labels.empty()) break;
        for (int l : labels) {
            int w = bw[l];
            bw[l] = -1;
            setMove(w, 2, [&](int c) { return fieldSlot[c] >= INF ? INF : fieldSlot[c] * 4 + (keepOut[c] ? 3 : 0); });
            slotTaken[l] = tickNo;
            statWalk++;
            un.erase(std::find(un.begin(), un.end(), w));
        }
        std::vector<std::pair<int, int>> next;
        for (auto& s : open)
            if (slotTaken[s.second] != tickNo) next.push_back(s);
        open.swap(next);
        rounds++;
    }
    // nobody's slot: look for new land under fog, else wait
    if (!un.empty()) {
        statIdle += (int)un.size();
        bool haveField = false;
        if (fog) {
            std::vector<int> src;
            for (int c = 0; c < NN; c++) {
                if (lastSeen[c] < 0 || blkWF[c]) continue;
                int x = cx(c), y = cy(c);
                for (int k = 0; k < 4; k++) {
                    int nx = x + DX[k], ny = y + DY[k];
                    if (inMap(nx, ny) && lastSeen[cellOf(nx, ny)] < 0) {
                        src.push_back(c);
                        break;
                    }
                }
            }
            if (!src.empty()) {
                bfs(src, blkWF, fieldB);
                haveField = true;
            }
        }
        for (int w : un) {
            if (haveField && fieldB[es[w].cell] < INF)
                setMove(w, 1, [&](int c) { return fieldB[c] >= INF ? INF : fieldB[c] * 4 + (keepOut[c] ? 3 : 0); });
            else
                setStay(w, 0);
        }
    }
}

int Brain::spawnCell(int baseIdx, const int* field) const {
    const Ent& b = es[baseIdx];
    int best = -1, bv = INF + 1;
    edgeRing(b.x, b.y, b.size, [&](int c) {
        if (occ[c] >= 0 || keepOut[c]) return;
        int v = field ? field[c] : 0;
        if (v < bv) bv = v, best = c;
    });
    return best;
}

// ------------------------------------------------------------------------------------------------ production
void Brain::produce() {
    int nB = (int)myBuilders.size(), nR = (int)myRanged.size();
    int freePop = popCap - popUsed;
    reserve = 0;
    for (const Plan& p : plans) {
        if (p.type == HOUSE) {
            if (freePop <= (int)(prodRate * KN("RES_LEAD", 0))) reserve += 50;
        } else if (!p.clearing) {
            reserve += unitCost(p.type);
        }
    }
    int budget = myRes - reserve;
    bool threatened = tickNo - lastHomeThreat < 80;
    int bMax = (int)KN("B_MAX", 64);
    if (nR >= (int)KN("B_MAX2_R", 30)) bMax = (int)KN("B_MAX2", 72);      // with an army standing, a bigger workforce
    if (tickNo > (int)KN("B_STOP", 820)) bMax = 0;
    if (statIdle >= (int)KN("B_IDLE", 3)) bMax = std::min(bMax, nB);
    bool canB = builderBase >= 0 && es[builderBase].active && nB < bMax;
    bool canR = rangedBase >= 0 && es[rangedBase].active;
    // how many ranged the moment asks for
    int sched = (int)std::max(0.0, (tickNo - KN("R_T0", 170)) * KN("R_SLOPE", 0.33));
    std::unordered_map<int, int> seenArmy;
    for (const auto& kv : memU)
        if (kv.second.type == RANGED_UNIT) seenArmy[kv.second.own] += 2;
        else if (kv.second.type == MELEE_UNIT) seenArmy[kv.second.own] += 1;
    int seenMax = 0;
    for (const auto& kv : seenArmy) seenMax = std::max(seenMax, kv.second / 2);
    int rWant = std::max(sched, (int)(seenMax * KN("R_MATCH", 1.2)) + 2);
    // the economy comes first: while it is small, only an army that is on its way here is answered
    if (nB < (int)KN("ECO_MIN", 30)) {
        int near2 = 0;
        for (const auto& kv : memU) {
            const MemUnit& g = kv.second;
            if (tickNo - g.seen > 60 || fieldTerr[g.cell] > (int)KN("ECO_ALERT", 45)) continue;
            near2 += g.type == RANGED_UNIT ? 2 : g.type == MELEE_UNIT ? 1 : 0;
        }
        rWant = near2 ? near2 / 2 + 2 : 0;
    }
    bool rFirst = canR && (nR < rWant || threatened || !canB);
    // the workforce is kept up: lost builders come back before more soldiers, unless the enemy is at the door
    if (canB && tickNo - lastHomeThreat > (int)KN("B_FLOOR_CALM", 0) && nB < (int)(bMax * KN("B_FLOOR", 1.0)) && nR >= (int)KN("B_FLOOR_R", 20)) rFirst = false;
    auto tryBuild = [&](int baseIdx, int type, const int* field) -> int {
        int cost = unitCost(type);
        if (freePop <= 0) return 0;
        if (budget < cost) return -1;
        int c = spawnCell(baseIdx, field);
        if (c < 0) return 0;
        Desire& d = des[baseIdx];
        d.set = true;
        d.act = ACT_BUILD;
        d.a1 = type;
        d.a2 = cx(c);
        d.a3 = cy(c);
        budget -= cost;
        myRes -= cost;
        freePop--;
        return 1;
    };
    for (int pass = 0; pass < 2; pass++) {
        bool ranged = (pass == 0) == rFirst;
        if (ranged && canR) {
            if (tryBuild(rangedBase, RANGED_UNIT, fieldHome) < 0 && rFirst) budget -= unitCost(RANGED_UNIT);
        } else if (!ranged && canB) {
            if (tryBuild(builderBase, BUILDER_UNIT, fieldA) < 0 && !rFirst) budget -= unitCost(BUILDER_UNIT);
        }
    }
}

// ------------------------------------------------------------------------------------------------ battle simulator
// A few ticks of a local fight on the real grid: everybody shoots the weakest enemy in range (shots land together),
// who has no shot walks towards the enemy when its side advances. Tells what a push or a stand is likely to cost.
void Brain::simulate(const std::vector<SimUnit>& init, bool advMe, bool advEn, int T, const int* toEn, const int* toMe, double& myLoss, double& enLoss) {
    static std::vector<int> socc(NN, 0);
    static std::vector<SimUnit> u;
    static std::vector<int> pend, order;
    static std::vector<char> shot;
    u = init;
    int n = (int)u.size();
    auto place = [&](int i, int v) {
        if (u[i].type == 2) {
            int x = cx(u[i].cell), y = cy(u[i].cell);
            for (int d = 0; d < 4; d++)
                if (inMap(x + (d & 1), y + (d >> 1))) socc[cellOf(x + (d & 1), y + (d >> 1))] = v;
        } else {
            socc[u[i].cell] = v;
        }
    };
    for (int i = 0; i < n; i++) place(i, i + 1);
    order.resize(n);
    for (int i = 0; i < n; i++) order[i] = i;
    std::sort(order.begin(), order.end(), [&](int a, int b) {
        int fa = (u[a].side == 0 ? toEn : toMe)[u[a].cell], fb = (u[b].side == 0 ? toEn : toMe)[u[b].cell];
        return fa != fb ? fa < fb : a < b;
    });
    pend.resize(n);
    shot.resize(n);
    myLoss = enLoss = 0;
    const double val[3] = {1.0, 0.5, 2.0};
    for (int t = 0; t < T; t++) {
        for (int i = 0; i < n; i++) pend[i] = u[i].hp, shot[i] = 0;
        bool any = false;
        for (int i : order) {
            if (u[i].hp <= 0) continue;
            int x = cx(u[i].cell), y = cy(u[i].cell);
            int best = -1;
            int range = u[i].type == 1 ? 1 : 5, size = u[i].type == 2 ? 2 : 1;
            around(x, y, size, range, [&](int c) {
                int o = socc[c] - 1;
                if (o < 0 || u[o].side == u[i].side || u[o].hp <= 0) return;
                if (best < 0) best = o;
                else if (pend[o] > 0 && (pend[best] <= 0 || pend[o] < pend[best] || (pend[o] == pend[best] && u[o].type < u[best].type))) best = o;
            });
            if (best >= 0) {
                pend[best] -= 5;
                shot[i] = 1;
                any = true;
            }
        }
        if (any)
            for (int i = 0; i < n; i++) {
                if (u[i].hp <= 0 || pend[i] == u[i].hp) continue;
                u[i].hp = pend[i];
                if (u[i].hp <= 0) {
                    place(i, 0);
                    (u[i].side == 0 ? myLoss : enLoss) += val[u[i].type];
                }
            }
        for (int i : order) {
            if (u[i].hp <= 0 || shot[i] || u[i].type == 2) continue;
            if (!(u[i].side == 0 ? advMe : advEn)) continue;
            const int* f = u[i].side == 0 ? toEn : toMe;
            int c = u[i].cell, x = cx(c), y = cy(c), bestC = -1, bv = f[c];
            for (int k = 0; k < 4; k++) {
                int nx = x + DX[k], ny = y + DY[k];
                if (!inMap(nx, ny)) continue;
                int nc = cellOf(nx, ny);
                if (wall[nc] || socc[nc] || f[nc] >= bv) continue;
                bv = f[nc];
                bestC = nc;
            }
            if (bestC >= 0) {
                socc[c] = 0;
                u[i].cell = bestC;
                socc[bestC] = i + 1;
            }
        }
    }
    for (int i = 0; i < n; i++)
        if (u[i].hp > 0) place(i, 0);
}

// ------------------------------------------------------------------------------------------------ army
int Brain::enemyPower(int pid) const {
    int p2 = 0;
    for (const auto& kv : memU) {
        if (kv.second.own != pid) continue;
        if (kv.second.type == RANGED_UNIT) p2 += 2;
        else if (kv.second.type == MELEE_UNIT) p2 += 1;
    }
    for (const auto& kv : memB)
        if (kv.second.own == pid && kv.second.type == TURRET) p2 += 4;
    return p2 / 2;
}

void Brain::chooseTarget() {
    for (const Ent& e : es)
        if (e.own > 0) enemyDeadTick.erase(e.own);
    for (int p : enemyIds) {
        if (enemyDeadTick.count(p)) continue;
        bool any = false;
        for (const Ent& e : es)
            if (e.own == p) {
                any = true;
                break;
            }
        if (!any)
            for (const auto& kv : memB)
                if (kv.second.own == p) {
                    any = true;
                    break;
                }
        if (any) continue;
        int c = enemyCorner[p];
        bool seen = true;
        for (int dy = 0; dy < 5 && seen; dy++)
            for (int dx = 0; dx < 5; dx++)
                if (lastSeen[cellOf(cx(c) + dx, cy(c) + dy)] < tickNo - 3) {
                    seen = false;
                    break;
                }
        if (seen) enemyDeadTick[p] = tickNo;
    }
    int best = 0;
    double bestSc = 1e18;
    for (int p : enemyIds) {
        if (enemyDeadTick.count(p)) continue;
        int c = enemyCorner[p];
        int d = std::abs(cx(c) + 2 - cx(baseCell)) + std::abs(cy(c) + 2 - cy(baseCell));
        double sc = d * KN("TGT_DIST", 1.0) + enemyPower(p) * KN("TGT_PW", 5.0) + enemyScore[p] / KN("TGT_SCORE", 150.0) + (p == targetPlayer ? -30 : 0);
        if (sc < bestSc) bestSc = sc, best = p;
    }
    targetPlayer = best;
}

void Brain::army() {
    chooseTarget();
    // ---- the march field: to what is known of the target, digging where it is shorter
    {
        std::vector<int> src;
        auto addAssets = [&](int p) {
            for (const auto& kv : memB) {
                const MemBuilding& b = kv.second;
                if (p && b.own != p) continue;
                around(b.x, b.y, SIZE_OF[b.type], 1, [&](int c) { if (wall[c] != 2) src.push_back(c); });
            }
            for (const auto& kv : memU) {
                const MemUnit& u = kv.second;
                if ((p && u.own != p) || tickNo - u.seen > 40) continue;
                src.push_back(u.cell);
            }
        };
        if (targetPlayer) {
            addAssets(targetPlayer);
            if (src.empty()) {
                int c = enemyCorner[targetPlayer];
                around(cx(c), cy(c), 5, 1, [&](int k) { if (wall[k] != 2) src.push_back(k); });
            }
        } else {
            addAssets(0);
            if (src.empty()) {
                int old = tickNo - (int)KN("EXPLORE_AGE", 150);
                for (int c = 0; c < NN; c++)
                    if (lastSeen[c] < old && wall[c] != 2) src.push_back(c);
            }
        }
        std::fill(fieldMarch, fieldMarch + NN, INF);
        typedef std::pair<int, int> PI;
        std::priority_queue<PI, std::vector<PI>, std::greater<PI>> pq;
        for (int c : src)
            if (fieldMarch[c] != 0) {
                fieldMarch[c] = 0;
                pq.push({0, c});
            }
        int digBase = (int)KN("DIG_COST", 2);
        while (!pq.empty()) {
            PI t = pq.top();
            pq.pop();
            int c = t.second, d = t.first;
            if (d > fieldMarch[c]) continue;
            int x = cx(c), y = cy(c);
            for (int k = 0; k < 4; k++) {
                int nx = x + DX[k], ny = y + DY[k];
                if (!inMap(nx, ny)) continue;
                int nc = cellOf(nx, ny);
                if (wall[nc] == 2) continue;
                // the cost of leaving nc towards c is the cost of entering c
                int w = 1;
                if (wall[c] == 1) w += digBase + ((resHp[c] > 0 ? resHp[c] : 30) + 4) / 5;
                if (d + w < fieldMarch[nc]) {
                    fieldMarch[nc] = d + w;
                    pq.push({d + w, nc});
                }
            }
        }
    }
    // ---- the guard ground: on the way to the enemy, a walk out from the base; the way there is a road (no houses)
    {
        // the nearest enemy fighter to my land, seen now or lately
        int best = -1, bd = (int)KN("THREAT_R", 60);
        for (int i : enCombat)
            if (es[i].unit() && fieldTerr[es[i].cell] < bd) bd = fieldTerr[es[i].cell], best = es[i].cell;
        if (best >= 0) {
            if (threatCell < 0 || tickNo - threatTick > 30 || mdist(best, threatCell) > 12) rallyTick = -1000;
            threatCell = best;
            threatTick = tickNo;
        } else if (threatCell >= 0 && tickNo - threatTick > (int)KN("THREAT_TTL", 200)) {
            threatCell = -1;
            rallyTick = -1000;
        }
    }
    if (rallyCell < 0 || tickNo - rallyTick >= 40 || wall[rallyCell] || keepOut[rallyCell]) {
        const int* gf = fieldMarch;
        static int fieldThreat[NN];
        if (threatCell >= 0 && !wall[threatCell]) {
            bfs(std::vector<int>{threatCell}, wall, fieldThreat);
            gf = fieldThreat;
        }
        int start = baseCell;
        if (rangedBase >= 0 || builderBase >= 0) {
            const Ent& b = es[rangedBase >= 0 ? rangedBase : builderBase];
            int bv = INF;
            edgeRing(b.x, b.y, b.size, [&](int c) { if (!wall[c] && gf[c] < bv) bv = gf[c], start = c; });
        }
        std::vector<int> path;
        int c = start, L = (int)KN("GUARD_D", 14);
        if (gf != fieldMarch) L = std::min(L, std::max(2, gf[start] - 9));
        memset(road, 0, sizeof road);
        for (int step = 0; step <= L && gf[c] < INF; step++) {
            path.push_back(c);
            int x = cx(c), y = cy(c), bestC = -1, bv = gf[c];
            for (int k = 0; k < 4; k++) {
                int nx = x + DX[k], ny = y + DY[k];
                if (!inMap(nx, ny)) continue;
                int nc = cellOf(nx, ny);
                if (wall[nc] || gf[nc] >= bv) continue;
                bv = gf[nc];
                bestC = nc;
            }
            if (bestC < 0) break;
            c = bestC;
        }
        int best = path.empty() ? baseCell : path.back();
        int bestOpen = -1;
        for (size_t k = path.size() > 5 ? path.size() - 5 : 0; k < path.size(); k++) {
            int open = 0;
            around(cx(path[k]), cy(path[k]), 1, 3, [&](int q) { if (!wall[q]) open++; });
            if (open > bestOpen) bestOpen = open, best = path[k];
        }
        rallyCell = best;
        rallyTick = tickNo;
        int rw = (int)KN("ROAD_W", 1), gr = (int)KN("GUARD_R", 4);
        for (int q : path) {
            around(cx(q), cy(q), 1, rw, [&](int k) { road[k] = 1; });
            if (q == best) break;
        }
        around(cx(best), cy(best), 1, gr, [&](int k) { road[k] = 1; });
    }
    bfs(std::vector<int>{rallyCell}, blkW, fieldHome);

    std::vector<int> units = myRanged;
    if (units.empty() && myMelee.empty()) return;

    // ---- firing positions: against everything seen, and against what is at home
    bool homeThreat = false;
    {
        std::vector<std::pair<int, int>> src, srcHome;
        static std::vector<int> off(NN), offH(NN);
        std::fill(off.begin(), off.end(), INF);
        std::fill(offH.begin(), offH.end(), INF);
        for (const Ent& e : es) {
            if (e.own <= 0) continue;
            int o = (e.type == RANGED_UNIT || e.type == MELEE_UNIT || (e.type == TURRET && e.active)) ? 0 : e.type == BUILDER_UNIT ? 2 : 5;
            bool home = false;
            for (int dy = 0; dy < e.size && !home; dy++)
                for (int dx = 0; dx < e.size; dx++)
                    if (homeZone[cellOf(e.x + dx, e.y + dy)]) {
                        home = true;
                        break;
                    }
            if (!home && e.unit() && e.type != BUILDER_UNIT)
                for (int b : myBuilders)
                    if (std::abs(es[b].x - e.x) + std::abs(es[b].y - e.y) <= (int)KN("RAID_R", 9)) {
                        home = true;
                        break;
                    }
            if (!home && e.unit() && fieldTerr[e.cell] <= (int)KN("TERR_NEAR", 22)) home = true;
            if (home) homeThreat = true;
            around(e.x, e.y, e.size, 5, [&](int c) {
                if (blkWF[c]) return;
                if (o < off[c]) off[c] = o;
                if (home && o < offH[c]) offH[c] = o;
            });
        }
        for (int c = 0; c < NN; c++) {
            if (off[c] < INF) src.push_back({off[c], c});
            if (offH[c] < INF) srcHome.push_back({offH[c], c});
        }
        bfsOff(src, blkWF, fieldAtk, nullptr);
        bfsOff(srcHome, blkWF, fieldB, nullptr);     // fieldB: the way to a shot at an intruder
    }

    // ---- battles: clusters of enemy fighters against my units near them
    int nE = (int)enCombat.size();
    std::vector<int> par(nE);
    for (int k = 0; k < nE; k++) par[k] = k;
    std::function<int(int)> find = [&](int a) { return par[a] == a ? a : par[a] = find(par[a]); };
    for (int a = 0; a < nE; a++)
        for (int b = a + 1; b < nE; b++) {
            const Ent& A = es[enCombat[a]];
            const Ent& B = es[enCombat[b]];
            if (std::abs(A.x - B.x) + std::abs(A.y - B.y) <= (int)KN("CLUSTER_LINK", 7)) par[find(a)] = find(b);
        }
    std::vector<int> prevAtk(nE, 0), cnt(nE, 0);
    std::vector<char> homeB(nE, 0);
    for (int k = 0; k < nE; k++)
        if (homeZone[es[enCombat[k]].cell]) homeB[find(k)] = 1;
    int contactR = (int)KN("CONTACT", 12);
    std::vector<int> nearK(es.size(), -1), nearD(es.size(), 99);
    for (int i : units) {
        const Ent& u = es[i];
        for (int k = 0; k < nE; k++) {
            const Ent& e = es[enCombat[k]];
            int d = sqDist(u.x, u.y, e.x, e.y, e.size);
            if (d < nearD[i]) nearD[i] = d, nearK[i] = k;
        }
        if (nearK[i] >= 0 && nearD[i] <= contactR) {
            int r = find(nearK[i]);
            cnt[r]++;
            if (umem[u.id].mode == MODE_ATTACK) prevAtk[r]++;
        }
    }
    std::vector<int> modeOf(nE, MODE_NONE);
    for (int k = 0; k < nE; k++) {
        if (find(k) != k || cnt[k] == 0) continue;
        if (lowPower) {
            modeOf[k] = homeB[k] ? MODE_ATTACK : MODE_HOLD;
            continue;
        }
        std::vector<SimUnit> su;
        std::vector<int> srcE, srcM;
        static std::vector<int> used(NN, -1);
        for (int j = 0; j < nE; j++) {
            if (find(j) != k) continue;
            const Ent& e = es[enCombat[j]];
            su.push_back(SimUnit{e.cell, e.hp, 1, e.type == RANGED_UNIT ? 0 : e.type == MELEE_UNIT ? 1 : 2});
            srcE.push_back(e.cell);
            used[e.cell] = tickNo;
        }
        for (const auto& kv : memU) {
            const MemUnit& g = kv.second;
            if (g.seen == tickNo || tickNo - g.seen > (int)KN("GHOST_AGE", 10) || g.type != RANGED_UNIT) continue;
            if (wall[g.cell] || used[g.cell] == tickNo || (vis[g.cell] && occ[g.cell] >= 0)) continue;
            int bd = 99, bk = -1;
            for (int j = 0; j < nE; j++) {
                int d = mdist(g.cell, es[enCombat[j]].cell);
                if (d < bd) bd = d, bk = j;
            }
            if (bk < 0 || bd > 8 || find(bk) != k) continue;
            su.push_back(SimUnit{g.cell, g.hp, 1, 0});
            srcE.push_back(g.cell);
            used[g.cell] = tickNo;
        }
        for (int i : units) {
            if (nearK[i] < 0 || nearD[i] > contactR || find(nearK[i]) != k) continue;
            su.push_back(SimUnit{es[i].cell, es[i].hp, 0, 0});
            srcM.push_back(es[i].cell);
        }
        for (int i : myTurrets) {
            const Ent& t = es[i];
            if (!t.active) continue;
            bool near = false;
            for (int c : srcE)
                if (sqDist(cx(c), cy(c), t.x, t.y, 2) <= 9) near = true;
            if (near) su.push_back(SimUnit{t.cell, t.hp, 0, 2}), srcM.push_back(t.cell);
        }
        static int toEn[NN], toMe[NN];
        bfs(srcE, wall, toEn);
        bfs(srcM, wall, toMe);
        int T = (int)KN("SIM_T", 10);
        double m1, e1, m2, e2, m3, e3;
        simulate(su, true, false, T, toEn, toMe, m1, e1);
        simulate(su, true, true, T, toEn, toMe, m2, e2);
        simulate(su, false, true, T, toEn, toMe, m3, e3);
        double lam = nPlayers > 2 ? KN("LAMBDA4", 0.8) : KN("LAMBDA", 1.0);
        double push = std::min(e1 - lam * m1, e2 - lam * m2), stand = e3 - lam * m3;
        bool wasAtk = prevAtk[k] * 2 > cnt[k];
        double thr = wasAtk ? KN("PUSH_STAY", -0.5) : KN("PUSH_IN", 1.0);
        if (homeB[k]) thr -= KN("PUSH_HOME", 1.5);
        int m;
        if (push >= thr) m = MODE_ATTACK;
        else if (homeB[k]) m = push > stand ? MODE_ATTACK : MODE_HOLD;
        else if (stand >= -KN("STAND_TOL", 1.0)) m = MODE_HOLD;
        else m = MODE_RETREAT;
        modeOf[k] = m;
        if (debug >= 2)
            fprintf(stderr, "  battle@(%d,%d) me=%d en=%d home=%d push %.1f/%.1f | %.1f/%.1f stand %.1f/%.1f -> %s\n", es[enCombat[k]].x, es[enCombat[k]].y, cnt[k], (int)srcE.size(), (int)homeB[k],
                    e1, m1, e2, m2, e3, m3, m == MODE_ATTACK ? "ATTACK" : m == MODE_HOLD ? "HOLD" : "RETREAT");
    }

    // ---- who is with the army, who waits at the rally
    int nArmy = (int)units.size();
    int joinedN = 0, reserveN = 0;
    for (int i : units) (umem[es[i].id].joined ? joinedN : reserveN)++;
    double homePower = 0;
    for (int k = 0; k < nE; k++)
        if (fieldTerr[es[enCombat[k]].cell] <= (int)KN("TERR_NEAR", 22)) homePower += es[enCombat[k]].type == MELEE_UNIT ? 0.5 : 1.0;
    int attackN = (int)(nPlayers > 2 ? KN("ATTACK_N4", 12) : KN("ATTACK_N", 16));
    if (!attacking && nArmy >= attackN && homePower < KN("ATTACK_CALM", 3)) attacking = true;
    if (attacking && joinedN + reserveN < (int)KN("ATTACK_MIN", 4)) attacking = false;
    if (attacking && homePower >= std::max(3.0, reserveN * KN("RECALL", 1.3))) {
        attacking = false;              // the home is being overrun: the wave ends, everybody defends
        for (int i : units) umem[es[i].id].joined = 0;
        joinedN = 0;
    }
    if (attacking && (joinedN == 0 || reserveN >= (int)KN("BATCH", 6))) {
        for (int i : units) umem[es[i].id].joined = 1;
        joinedN = nArmy;
    }
    if (!attacking && joinedN > 0) {
        for (int i : units) umem[es[i].id].joined = 0;
        joinedN = 0;
    }
    // the median of the army on the march
    int med = INF;
    {
        std::vector<int> ds;
        for (int i : units)
            if (umem[es[i].id].joined && fieldMarch[es[i].cell] < INF) ds.push_back(fieldMarch[es[i].cell]);
        if (!ds.empty()) {
            std::nth_element(ds.begin(), ds.begin() + ds.size() / 2, ds.end());
            med = ds[ds.size() / 2];
        }
    }
    static std::vector<int> ally(NN);
    std::fill(ally.begin(), ally.end(), 0);
    int allyR = (int)KN("ALLY_R", 3);
    for (int i : units) around(es[i].x, es[i].y, 1, allyR, [&](int c) { ally[c]++; });
    double sup = KN("SUPPORT", 0.9);
    int rallyRad = 1 + (int)(std::sqrt((double)reserveN) * KN("GUARD_SPREAD", 0.9));

    // who answers a threat at home: the nearest, as many as it takes
    std::vector<char> responder(es.size(), 0);
    if (homeThreat) {
        double tp = 0;
        for (int k = 0; k < nE; k++) {
            const Ent& e = es[enCombat[k]];
            if (fieldTerr[e.cell] <= (int)KN("TERR_NEAR", 22) || homeZone[e.cell]) tp += e.type == MELEE_UNIT ? 0.5 : e.type == TURRET ? 2.0 : 1.0;
        }
        int need = (int)(tp * KN("DEF_OVER", 1.6)) + (int)KN("DEF_MIN", 3);
        std::vector<std::pair<int, int>> byD;
        for (int i : units)
            if (fieldB[es[i].cell] < INF) byD.push_back({fieldB[es[i].cell], i});
        std::sort(byD.begin(), byD.end());
        for (size_t k = 0; k < byD.size() && (int)k < need; k++) responder[byD[k].second] = 1;
        if (!attacking)
            for (auto& d : byD) responder[d.second] = 1;      // nothing else to do: all of them
    }
    // ---- the line: who holds takes a place of its own on the edge of the enemy's fire (a step from a shot),
    //      the nearest places first, so the army stands wide and not deep
    std::vector<char> onLine(es.size(), 0);
    {
        std::vector<int> holders;
        for (int i : units) {
            if (evicted[i] || des[i].fixed) continue;
            if (nearK[i] >= 0 && nearD[i] <= contactR && modeOf[find(nearK[i])] == MODE_HOLD) holders.push_back(i);
        }
        if (!holders.empty()) {
            std::vector<std::pair<int, int>> open;
            for (int c = 0; c < NN; c++) {
                if (thr5[c] > 0 || thrM[c] > 0 || blkWF[c] || keepOut[c]) continue;
                int x = cx(c), y = cy(c);
                bool edge = false;
                for (int k = 0; k < 4 && !edge; k++) {
                    int nx = x + DX[k], ny = y + DY[k];
                    if (inMap(nx, ny) && thr5[cellOf(nx, ny)] > 0) edge = true;
                }
                if (edge) open.push_back({0, c});
            }
            static std::vector<int> bw(NN, -1), taken(NN, -1);
            static unsigned char blkLine[NN];
            for (int c = 0; c < NN; c++) blkLine[c] = blkWF[c] || thr5[c] > 0;
            std::vector<int> un = holders;
            int maxWalk = (int)KN("LINE_WALK", 14);
            for (int round = 0; round < 24 && !un.empty() && !open.empty(); round++) {
                bfsOff(open, blkLine, fieldSlot, labelSlot);
                std::vector<int> labels;
                for (int w : un) {
                    int c = es[w].cell, d = fieldSlot[c];
                    if (d >= maxWalk) continue;
                    int l = labelSlot[c];
                    if (bw[l] < 0) bw[l] = w, labels.push_back(l);
                    else if (d < fieldSlot[es[bw[l]].cell]) bw[l] = w;
                }
                if (labels.empty()) break;
                for (int l : labels) {
                    int w = bw[l];
                    bw[l] = -1;
                    taken[l] = tickNo;
                    onLine[w] = 1;
                    umem[es[w].id].mode = MODE_HOLD;
                    setMove(w, 5, [&](int c) { return fieldSlot[c] >= INF ? INF : fieldSlot[c] * 10 + (thr5[c] > 0 ? 2000 : 0) + (keepOut[c] ? 3 : 0); });
                    un.erase(std::find(un.begin(), un.end(), w));
                }
                std::vector<std::pair<int, int>> next;
                for (auto& sl : open)
                    if (taken[sl.second] != tickNo) next.push_back(sl);
                open.swap(next);
            }
        }
    }
    for (int i : units) {
        const Ent& u = es[i];
        UnitMem& m = umem[u.id];
        int mode = MODE_NONE;
        if (nearK[i] >= 0 && nearD[i] <= contactR) mode = modeOf[find(nearK[i])];
        m.mode = mode;
        if (evicted[i] || onLine[i]) continue;
        if (des[i].fixed) {
            // it has a shot. A retreat still shoots at fighters, but leaves buildings alone
            if (mode != MODE_RETREAT) continue;
            int tgt = -1;
            for (int j = 0; j < (int)es.size(); j++)
                if (es[j].id == des[i].a1) {
                    tgt = j;
                    break;
                }
            if (tgt >= 0 && (es[tgt].type == RANGED_UNIT || es[tgt].type == MELEE_UNIT || es[tgt].type == TURRET)) continue;
        }
        int c0 = u.cell;
        if (mode == MODE_ATTACK) {
            setMove(i, 5, [&](int c) { return fieldAtk[c] >= INF ? INF : fieldAtk[c] * 10 + thrM[c] * 40 + (keepOut[c] ? 3 : 0); });
        } else if (mode == MODE_HOLD) {
            setMove(i, 5, [&](int c) {
                int v = (thr6[c] > 0 ? 1000 + thr6[c] * 50 : 0) + thrM[c] * 200 + std::min(fieldAtk[c], 100) * 10 + (keepOut[c] ? 3 : 0);
                return v;
            });
        } else if (mode == MODE_RETREAT) {
            setMove(i, 5, [&](int c) { return std::min(fieldHome[c], 500) * 10 + thr6[c] * 30 + thr5[c] * 60 + thrM[c] * 100; });
        } else if (homeThreat && responder[i]) {
            // an intruder at home and nobody near me: go and meet it, to the edge of its reach
            setMove(i, 4, [&](int c) {
                if (fieldB[c] >= INF) return INF;
                return fieldB[c] * 10 + (thr6[c] > 0 && ally[c] < thr6[c] * sup ? 300 : 0) + (keepOut[c] ? 3 : 0);
            });
        } else if (fieldAtk[c0] < (m.joined ? (int)KN("HUNT_R", 14) : 8)) {
            // something to shoot within a few steps (a builder, a house): take it
            setMove(i, 4, [&](int c) { return fieldAtk[c] >= INF ? INF : fieldAtk[c] * 10 + (keepOut[c] ? 3 : 0); });
        } else if (m.joined) {
            int d0 = fieldMarch[c0];
            if (d0 >= INF) {
                setStay(i, 1);
                continue;
            }
            // dig when the shortest way leads through a resource
            int bestFree = INF, digId = -1;
            for (int k = 0; k < 4; k++) {
                int nx = u.x + DX[k], ny = u.y + DY[k];
                if (!inMap(nx, ny)) continue;
                int nc = cellOf(nx, ny);
                if (wall[nc] == 1) {
                    int o = occ[nc];
                    if (o >= 0 && es[o].type == RESOURCE && fieldMarch[nc] < d0 && digId < 0) digId = es[o].id;
                } else if (!blkW[nc]) {
                    bestFree = std::min(bestFree, fieldMarch[nc]);
                }
            }
            if (digId >= 0 && bestFree >= d0) {
                setFixed(i, ACT_ATTACK, digId);
                continue;
            }
            // the army walks as one body: who is ahead of its middle waits (and is pushed on by those who catch up)
            int lead = (int)KN("COH_AHEAD", 4) + joinedN / (int)KN("COH_DIV", 12);
            if (d0 < med - lead) {
                setStay(i, 1);
                continue;
            }
            setMove(i, 3, [&](int c) { return fieldMarch[c] >= INF ? INF : fieldMarch[c] * 10 + (keepOut[c] ? 3 : 0); });
        } else {
            // the reserve gathers at the rally point, off the bases' doorsteps
            if (fieldHome[c0] <= rallyRad && !keepOut[c0] && !noIdle[c0]) setStay(i, 0);
            else setMove(i, 2, [&](int c) { return fieldHome[c] >= INF ? INF : fieldHome[c] * 10 + (keepOut[c] || noIdle[c] ? 30 : 0); });
        }
    }
    // melee (the one Round 1 gives): it strikes what comes next to it, and otherwise keeps out of the way
    for (int i : myMelee)
        if (!des[i].set && !evicted[i]) setStay(i, 0);
}

// ------------------------------------------------------------------------------------------------ the tick
void Brain::report() {
    if (!debug) return;
    if (debug == 1 && tickNo % 10 != 0 && tickNo != 999) return;
    int nB = (int)myBuilders.size();
    fprintf(stderr, "[t%4d] res=%5d score=%6d pop=%d/%d B=%d(mine %d walk %d job %d flee %d idle %d) R=%d H=%d plans=%d inc=%d atk=%d tgt=%d\n", tickNo, myRes, myScore, popUsed, popCap, nB,
            statMine, statWalk, statJob, statFlee, statIdle, (int)myRanged.size(), (int)myHouses.size(), (int)plans.size(), income, (int)attacking, targetPlayer);
    if (tickNo == 999 || (debug >= 2 && tickNo % 100 == 0)) {
        double t = std::max(1LL, cumB);
        fprintf(stderr, "[use] mine %.1f%% walk %.1f%% job %.1f%% flee %.1f%% idle %.1f%%\n", 100.0 * cumMine / t, 100.0 * cumWalk / t, 100.0 * cumJob / t, 100.0 * cumFlee / t, 100.0 * cumIdle / t);
        fprintf(stderr, "[walks] 1:%lld(%lld) 2-3:%lld(%lld) 4-7:%lld(%lld) 8-15:%lld(%lld) 16-30:%lld(%lld) 31+:%lld(%lld) | after mining %lld, after a job %lld, new %lld\n", walkHist[0], walkHistT[0], walkHist[1],
                walkHistT[1], walkHist[2], walkHistT[2], walkHist[3], walkHistT[3], walkHist[4], walkHistT[4], walkHist[5], walkHistT[5], walkAfterMine, walkAfterJob, walkNew);
    }
    if (debug >= 3 && tickNo % 50 == 0) {
        for (int y = 39; y >= 0; y--) {
            char line[64];
            for (int x = 0; x < 60; x++) {
                int c = cellOf(x, y), o = occ[c];
                char ch = '.';
                if (keepOut[c]) ch = '_';
                if (o >= 0) {
                    const Ent& e = es[o];
                    if (e.type == RESOURCE) ch = '#';
                    else if (e.own == 0) ch = e.type == BUILDER_UNIT ? (des[o].fixed ? 'b' : 'w') : e.type == RANGED_UNIT ? 'r' : e.type == HOUSE ? 'H' : e.type == MELEE_UNIT ? 'm' : 'B';
                    else ch = e.type == BUILDER_UNIT ? 'x' : e.unit() ? 'X' : 'E';
                } else if (wall[c] == 1) ch = '%';
                else if (fog && lastSeen[c] < 0) ch = ' ';
                line[x] = ch;
            }
            line[60] = 0;
            fprintf(stderr, "%s\n", line);
        }
    }
}

Action Brain::tick(const PlayerView& v) {
    auto t0 = std::chrono::steady_clock::now();
    // the server gives 40 s of CPU a game: far more than this needs, but should the clock ever run away, the costly
    // parts (battle simulations, the slot auction's later rounds) are dropped rather than the game
    lowPower = spentMs > KN("CPU_SOFT_MS", 24000);
    parse(v);
    memory();
    assess();
    fire();
    economy();
    army();
    produce();
    moves();
    Action a = orders();
    report();
    spentMs += std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
    return a;
}

Brain* brain = nullptr;

}  // namespace

MyStrategy::MyStrategy() {}

Action MyStrategy::getAction(const PlayerView& playerView, DebugInterface* debugInterface) {
    if (!brain) brain = new Brain();
    try {
        return brain->tick(playerView);
    } catch (...) {
        return Action(std::unordered_map<int, EntityAction>());
    }
}

void MyStrategy::debugUpdate(const PlayerView& playerView, DebugInterface& debugInterface) {
    debugInterface.send(DebugCommand::Clear());
    debugInterface.getState();
}
