// system-opus-5-5-max: a CodeCraft 2020 strategy in one file (C++).
// Written from the rules alone. README.md next to src/ describes the design.
#include "MyStrategy.hpp"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <exception>
#include <map>
#include <memory>
#include <queue>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace {

using std::max;
using std::min;
using std::vector;

enum { T_WALL = 0, T_HOUSE, T_BBASE, T_BUILDER, T_MBASE, T_MELEE, T_RBASE, T_RANGED, T_RES, T_TURRET, T_N };
enum { F_ROUND1, F_ROUND2, F_FINALS };
enum { M_GATHER, M_DEFEND, M_ATTACK };
const int INF = 1 << 28;

struct TypeProps {
    int size = 1, maxHp = 1, cost = 0, popProv = 0, popUse = 0, sight = 0;
    int range = -1, dmg = 0, buildScore = 0, destroyScore = 0, initHp = 0;
    bool canMove = false;
};
TypeProps P[T_N];

int N = 80, NN = 6400;
inline int cid(int x, int y) { return y * N + x; }
inline int cxo(int c) { return c % N; }
inline int cyo(int c) { return c / N; }
inline bool inMap(int x, int y) { return x >= 0 && y >= 0 && x < N && y < N; }
const int DX[4] = {1, 0, -1, 0};
const int DY[4] = {0, 1, 0, -1};

inline int sqDist(int ax, int ay, int as, int bx, int by, int bs) {
    int dx = max(0, max(bx - (ax + as - 1), ax - (bx + bs - 1)));
    int dy = max(0, max(by - (ay + as - 1), ay - (by + bs - 1)));
    return dx + dy;
}
inline int mdist(int c1, int c2) { return std::abs(cxo(c1) - cxo(c2)) + std::abs(cyo(c1) - cyo(c2)); }
inline bool isUnitType(int t) { return t == T_BUILDER || t == T_MELEE || t == T_RANGED; }
inline bool isBuildingType(int t) { return t != T_RES && !isUnitType(t); }
inline bool isArmyType(int t) { return t == T_MELEE || t == T_RANGED; }

struct Ent {
    int id = 0, type = 0, owner = -1, x = 0, y = 0, hp = 0, size = 1;
    bool active = true, visible = true;
    int c() const { return cid(x, y); }
};

struct Task {
    int type = 0, x = 0, y = 0;
    int eid = -1;          // the building once placed
    int placer = -1;       // builder id that places it
    vector<int> helpers;   // builder ids that repair it
    int created = 0;
    int invalid = 0;
    bool builtNow = false, waiting = false;
    int noPlacer = 0;
};

struct MoveReq {
    int ei = 0, prio = 0, n = 0;
    int opt[4] = {0, 0, 0, 0};
};

// ------------------------------------------------------------------ persistent state
bool started = false;
int fmt = F_ROUND1, me = 0, tick = 0, nPlayers = 4;
bool fog = false;
vector<uint8_t> seenEver;
vector<int> lastSeen;
vector<int16_t> memRes;
std::map<int, Ent> memBld;
vector<Task> tasks;
int armyMode = M_GATHER;
int attackTarget = -1;      // player id (Round1) or corner index (fog)
int lastMining = 0;
double cpuStart = 0;
vector<int> lastMinerCells;  // where builders mined last tick
std::unordered_map<int, int> mineTarget;  // builder id -> the slot it walks to
int resvX = -1, resvY = -1;   // a site kept free for the ranged base until it is built
struct BadSite { int x, y, s, until; };
vector<BadSite> badSites;     // sites that failed lately: not chosen again for a while
int scoutId = -1;             // a builder that looks around home under fog of war
bool scoutDone = false;
int dbgWalk[6];
int dbgPush = 0;
std::map<int, int> dbgMoveBy;  // successful builder moves by request priority
int dbgNewT = 0, dbgNewTLen = 0, dbgKeptT = 0;
int dbgStallRes = 0, dbgStallPop = 0, dbgStallCell = 0, dbgStallReserve = 0, dbgTrained = 0;   // walking to a slot by distance: 1-3, 4-8, 9-15, 16-30, 31+; explore
std::map<int, std::pair<int, int>> seenArmy;   // enemy army unit id -> (owner, last tick seen)
int rallyC = -1, rallyTick = -1000;
int lastArmySeenC = -1, lastArmySeenTick = -1000;

// ------------------------------------------------------------------ per tick
int myRes = 0, myScore = 0, budget = 0;
vector<Ent> E;
std::unordered_map<int, int> byId;
vector<int> cellE;
vector<uint8_t> vis, blk, site, stayC, nearB;
vector<int> dNow, dNext, homeD, slotD;
vector<int> myOf[T_N];
vector<int> enemies;
int popProv = 0, popUsed = 0;
std::unordered_map<int, EntityAction> acts;
vector<uint8_t> decided;
vector<MoveReq> reqs;
vector<int> claimC;
vector<int> fieldDef, fieldAtk, fieldRally;
vector<uint8_t> pushTaken;
int miningNow = 0;
int idleBuilders = 0;     // builders that found nothing to mine this tick
vector<uint8_t> baseAdj;  // cells next to my bases: no idling there
int dbgLevel = -1;
double maxTickCpu = 0;

// BFS scratch
vector<int> bfsStamp, bfsPar, bfsQ;
int bfsCur = 0;

EntityAction actNone() { return EntityAction(nullptr, nullptr, nullptr, nullptr); }
EntityAction actMove(int c) {
    return EntityAction(std::make_shared<MoveAction>(Vec2Int(cxo(c), cyo(c)), false, false), nullptr, nullptr, nullptr);
}
EntityAction actAttack(int target) {
    return EntityAction(nullptr, nullptr, std::make_shared<AttackAction>(std::make_shared<int>(target), nullptr), nullptr);
}
EntityAction actBuild(int type, int x, int y) {
    return EntityAction(nullptr, std::make_shared<BuildAction>((EntityType)type, Vec2Int(x, y)), nullptr, nullptr);
}
EntityAction actRepair(int target) {
    return EntityAction(nullptr, nullptr, nullptr, std::make_shared<RepairAction>(target));
}

double cpuNow() { return (double)std::clock() / CLOCKS_PER_SEC; }

// tuning knobs: CC_<NAME> in the environment overrides a default (local experiments only)
std::map<std::string, double> knobCache;
double knob(const char* name, double def) {
    auto it = knobCache.find(name);
    if (it != knobCache.end()) return it->second;
    std::string env = std::string("CC_") + name;
    const char* v = std::getenv(env.c_str());
    double r = v ? std::atof(v) : def;
    knobCache[name] = r;
    return r;
}

void readProps(const PlayerView& pv) {
    for (const auto& kv : pv.entityProperties) {
        int t = (int)kv.first;
        if (t < 0 || t >= T_N) continue;
        const EntityProperties& p = kv.second;
        TypeProps& q = P[t];
        q.size = p.size;
        q.maxHp = p.maxHealth;
        q.cost = p.initialCost;
        q.popProv = p.populationProvide;
        q.popUse = p.populationUse;
        q.sight = p.sightRange;
        q.canMove = p.canMove;
        q.buildScore = p.buildScore;
        q.destroyScore = p.destroyScore;
        q.range = p.attack ? p.attack->attackRange : -1;
        q.dmg = p.attack ? p.attack->damage : 0;
        q.initHp = (p.build && p.build->initHealth) ? *p.build->initHealth : 0;
    }
}

template <class G>
void forDiamond(int x, int y, int s, int r, G g) {
    for (int yy = max(0, y - r); yy <= min(N - 1, y + s - 1 + r); ++yy) {
        int dy = yy < y ? y - yy : (yy > y + s - 1 ? yy - (y + s - 1) : 0);
        int rem = r - dy;
        if (rem < 0) continue;
        int x0 = max(0, x - rem), x1 = min(N - 1, x + s - 1 + rem);
        for (int xx = x0; xx <= x1; ++xx) g(yy * N + xx, dy + (xx < x ? x - xx : (xx > x + s - 1 ? xx - (x + s - 1) : 0)));
    }
}

int unitCost(int type) { return P[type].cost + (int)myOf[type].size(); }

// ------------------------------------------------------------------ world
void buildWorld(const PlayerView& pv) {
    N = pv.mapSize;
    NN = N * N;
    me = pv.myId;
    tick = pv.currentTick;
    if (!started) {
        started = true;
        fog = pv.fogOfWar;
        nPlayers = (int)pv.players.size();
        fmt = !fog ? F_ROUND1 : (nPlayers <= 2 ? F_FINALS : F_ROUND2);
        seenEver.assign(NN, 0);
        lastSeen.assign(NN, -1000);
        memRes.assign(NN, 0);
        bfsStamp.assign(NN, 0);
        bfsPar.assign(NN, -1);
        bfsQ.assign(NN, 0);
        cpuStart = cpuNow();
    }
    for (const auto& p : pv.players)
        if (p.id == me) {
            myRes = p.resource;
            myScore = p.score;
        }
    E.clear();
    byId.clear();
    for (auto& v : myOf) v.clear();
    enemies.clear();
    E.reserve(pv.entities.size() + memBld.size());
    for (const Entity& e : pv.entities) {
        Ent x;
        x.id = e.id;
        x.type = (int)e.entityType;
        if (x.type < 0 || x.type >= T_N) continue;
        x.owner = e.playerId ? *e.playerId : -1;
        x.x = e.position.x;
        x.y = e.position.y;
        x.hp = e.health;
        x.active = e.active;
        x.size = P[x.type].size;
        x.visible = true;
        byId[x.id] = (int)E.size();
        E.push_back(x);
    }
    vis.assign(NN, fog ? 0 : 1);
    if (fog)
        for (const Ent& x : E)
            if (x.owner == me) forDiamond(x.x, x.y, x.size, P[x.type].sight, [&](int c, int) { vis[c] = 1; });
    for (int c = 0; c < NN; ++c)
        if (vis[c]) {
            seenEver[c] = 1;
            lastSeen[c] = tick;
            memRes[c] = 0;
        }
    for (const Ent& x : E)
        if (x.type == T_RES) memRes[x.c()] = (int16_t)x.hp;
    if (!fog) memBld.clear();
    for (const Ent& x : E)
        if (x.owner >= 0 && x.owner != me && isBuildingType(x.type)) memBld[x.id] = x;
    for (auto it = memBld.begin(); it != memBld.end();) {
        if (byId.count(it->first)) {
            ++it;
            continue;
        }
        const Ent& b = it->second;
        bool seen = false;
        for (int yy = b.y; yy < b.y + b.size && !seen; ++yy)
            for (int xx = b.x; xx < b.x + b.size; ++xx)
                if (inMap(xx, yy) && vis[cid(xx, yy)]) {
                    seen = true;
                    break;
                }
        if (seen)
            it = memBld.erase(it);
        else
            ++it;
    }
    for (const auto& kv : memBld)
        if (!byId.count(kv.first)) {
            Ent b = kv.second;
            b.visible = false;
            byId[b.id] = (int)E.size();
            E.push_back(b);
        }
    cellE.assign(NN, -1);
    blk.assign(NN, 0);
    for (int i = 0; i < (int)E.size(); ++i) {
        const Ent& x = E[i];
        for (int yy = x.y; yy < x.y + x.size; ++yy)
            for (int xx = x.x; xx < x.x + x.size; ++xx) {
                if (!inMap(xx, yy)) continue;
                int c = cid(xx, yy);
                cellE[c] = i;
                if (!isUnitType(x.type)) blk[c] = 1;
            }
        if (x.owner == me)
            myOf[x.type].push_back(i);
        else if (x.owner >= 0)
            enemies.push_back(i);
    }
    for (int c = 0; c < NN; ++c)
        if (!vis[c] && memRes[c] > 0) blk[c] = 1;
    for (int j : enemies)
        if (isArmyType(E[j].type) && E[j].visible) seenArmy[E[j].id] = {E[j].owner, tick};
    for (auto it = seenArmy.begin(); it != seenArmy.end();) {
        if (tick - it->second.second > 90)
            it = seenArmy.erase(it);
        else
            ++it;
    }
    popProv = popUsed = 0;
    for (int t = 0; t < T_N; ++t)
        for (int i : myOf[t]) {
            popUsed += P[t].popUse;
            if (E[i].active) popProv += P[t].popProv;
        }
}

void computeDanger() {
    dNow.assign(NN, 0);
    dNext.assign(NN, 0);
    for (int i : enemies) {
        const Ent& e = E[i];
        if (!e.active) continue;
        if (e.type == T_RANGED || e.type == T_MELEE) {
            int r = P[e.type].range, d = P[e.type].dmg;
            forDiamond(e.x, e.y, 1, r + 1, [&](int c, int dist) {
                dNext[c] += d;
                if (dist <= r) dNow[c] += d;
            });
        } else if (e.type == T_TURRET) {
            int r = P[e.type].range, d = P[e.type].dmg;
            forDiamond(e.x, e.y, e.size, r, [&](int c, int) {
                dNext[c] += d;
                dNow[c] += d;
            });
        }
    }
}

void computeHome() {
    homeD.assign(NN, INF);
    int qh = 0, qt = 0;
    for (int t : {T_BBASE, T_MBASE, T_RBASE, T_HOUSE, T_TURRET, T_WALL})
        for (int i : myOf[t]) {
            const Ent& b = E[i];
            for (int yy = b.y; yy < b.y + b.size; ++yy)
                for (int xx = b.x; xx < b.x + b.size; ++xx) {
                    int c = cid(xx, yy);
                    if (homeD[c] != 0) {
                        homeD[c] = 0;
                        bfsQ[qt++] = c;
                    }
                }
        }
    if (qt == 0)
        for (int t : {T_BUILDER, T_RANGED, T_MELEE})
            for (int i : myOf[t]) {
                int c = E[i].c();
                if (homeD[c] != 0) {
                    homeD[c] = 0;
                    bfsQ[qt++] = c;
                }
            }
    while (qh < qt) {
        int c = bfsQ[qh++];
        int x = cxo(c), y = cyo(c);
        for (int d = 0; d < 4; ++d) {
            int nx = x + DX[d], ny = y + DY[d];
            if (!inMap(nx, ny)) continue;
            int n = cid(nx, ny);
            if (homeD[n] > homeD[c] + 1) {
                homeD[n] = homeD[c] + 1;
                bfsQ[qt++] = n;
            }
        }
    }
    nearB.assign(NN, 0);
    for (int i : myOf[T_BUILDER]) forDiamond(E[i].x, E[i].y, 1, 7, [&](int c, int) { nearB[c] = 1; });
}

// generic BFS from one start cell; returns goal cell or -1; fills bfsPar for the path
template <class Pass, class Goal>
int bfsFind(int start, Pass passable, Goal goal, int maxNodes = 1 << 30) {
    ++bfsCur;
    if (bfsCur == INT32_MAX) {
        std::fill(bfsStamp.begin(), bfsStamp.end(), 0);
        bfsCur = 1;
    }
    int qh = 0, qt = 0;
    bfsQ[qt++] = start;
    bfsStamp[start] = bfsCur;
    bfsPar[start] = -1;
    while (qh < qt) {
        int c = bfsQ[qh++];
        if (c != start && goal(c)) return c;
        if (qh > maxNodes) break;
        int x = cxo(c), y = cyo(c);
        for (int d = 0; d < 4; ++d) {
            int nx = x + DX[d], ny = y + DY[d];
            if (!inMap(nx, ny)) continue;
            int n = cid(nx, ny);
            if (bfsStamp[n] == bfsCur) continue;
            if (!passable(n) && !goal(n)) continue;
            if (!passable(n)) {
                // a goal cell that is not passable is still reachable as a goal
            }
            bfsStamp[n] = bfsCur;
            bfsPar[n] = c;
            bfsQ[qt++] = n;
        }
    }
    return -1;
}

vector<int> bfsDistV;
template <class Pass>
void bfsAll(int start, Pass passable, int maxDist, vector<int>& visited) {
    ++bfsCur;
    if ((int)bfsDistV.size() != NN) bfsDistV.assign(NN, 0);
    visited.clear();
    int qh = 0, qt = 0;
    bfsQ[qt++] = start;
    bfsStamp[start] = bfsCur;
    bfsPar[start] = -1;
    bfsDistV[start] = 0;
    while (qh < qt) {
        int c = bfsQ[qh++];
        visited.push_back(c);
        if (bfsDistV[c] >= maxDist) continue;
        int x = cxo(c), y = cyo(c);
        for (int d = 0; d < 4; ++d) {
            int nx = x + DX[d], ny = y + DY[d];
            if (!inMap(nx, ny)) continue;
            int n = cid(nx, ny);
            if (bfsStamp[n] == bfsCur || !passable(n)) continue;
            bfsStamp[n] = bfsCur;
            bfsPar[n] = c;
            bfsDistV[n] = bfsDistV[c] + 1;
            bfsQ[qt++] = n;
        }
    }
}

int firstStep(int start, int goal) {
    int c = goal;
    while (c >= 0 && bfsPar[c] != start) {
        c = bfsPar[c];
        if (c == start) return -1;
    }
    return c;
}

// multi-source BFS distance field with unit step costs
template <class Pass>
void bfsField(vector<int>& D, const vector<int>& src, Pass passable) {
    D.assign(NN, INF);
    int qh = 0, qt = 0;
    for (int c : src)
        if (D[c] != 0) {
            D[c] = 0;
            bfsQ[qt++] = c;
        }
    while (qh < qt) {
        int c = bfsQ[qh++];
        int x = cxo(c), y = cyo(c);
        for (int d = 0; d < 4; ++d) {
            int nx = x + DX[d], ny = y + DY[d];
            if (!inMap(nx, ny)) continue;
            int n = cid(nx, ny);
            if (D[n] <= D[c] + 1 || !passable(n)) continue;
            D[n] = D[c] + 1;
            bfsQ[qt++] = n;
        }
    }
}

bool enemyUnitAt(int c) {
    int o = cellE[c];
    return o >= 0 && E[o].owner != me && E[o].owner >= 0 && isUnitType(E[o].type);
}

void addReq(int ei, int prio, const int* opts, int n) {
    if (n <= 0) return;
    MoveReq r;
    r.ei = ei;
    r.prio = prio;
    r.n = min(n, 4);
    for (int k = 0; k < r.n; ++k) r.opt[k] = opts[k];
    reqs.push_back(r);
    decided[ei] = 1;
}

// a move along the last BFS path to `goal`: its first step, then the sidesteps (never straight back)
template <class Pass>
bool addPathReq(int ei, int prio, int start, int goal, Pass passable) {
    int st = firstStep(start, goal);
    if (st < 0) return false;
    int opts[4], n = 0;
    opts[n++] = st;
    int x = cxo(start), y = cyo(start);
    int back = cid(x, y) * 2 - st;  // the cell opposite to the step (same row or column)
    std::pair<int, int> alt[4];
    int m = 0;
    for (int d = 0; d < 4; ++d) {
        int nx = x + DX[d], ny = y + DY[d];
        if (!inMap(nx, ny)) continue;
        int nc = cid(nx, ny);
        if (nc == st || nc == back || !passable(nc)) continue;
        alt[m++] = {mdist(nc, goal), nc};
    }
    std::sort(alt, alt + m);
    for (int k = 0; k < m && n < 3; ++k) opts[n++] = alt[k].second;
    addReq(ei, prio, opts, n);
    return true;
}

void setAct(int ei, const EntityAction& a, bool stays) {
    acts[E[ei].id] = a;
    decided[ei] = 1;
    if (stays) stayC[E[ei].c()] = 1;
}

// ------------------------------------------------------------------ building sites
bool siteCellsOk(int x, int y, int s, bool allowMyUnits) {
    if (x < 0 || y < 0 || x + s > N || y + s > N) return false;
    for (int yy = y; yy < y + s; ++yy)
        for (int xx = x; xx < x + s; ++xx) {
            int c = cid(xx, yy);
            if (blk[c] || site[c]) return false;
            if (fog && !seenEver[c]) return false;
            int o = cellE[c];
            if (o >= 0) {
                if (!allowMyUnits) return false;
                if (E[o].owner != me) return false;
            }
        }
    return true;
}

// cells around my bases stay free for spawns and traffic: one cell all round, two on the outward sides
bool keepFree(int c) {
    int x = cxo(c), y = cyo(c);
    bool tight = knob("TIGHT", 1) > 0;
    auto near = [&](int bx, int by, int s) {
        if (tight) {
            // exits: the middle of the right and bottom sides, two cells out, and the outer corner
            int m = s / 2;
            if ((y == by + m || y == by + m - 1) && (x == bx + s || x == bx + s + 1)) return true;
            if ((x == bx + m || x == bx + m - 1) && (y == by + s || y == by + s + 1)) return true;
            return x == bx + s && y == by + s;
        }
        int d = sqDist(bx, by, s, x, y, 1);
        if (d == 1) return true;
        if (d != 2) return false;
        return (x >= bx + s || y >= by + s) && x >= bx && y >= by;  // the right and bottom bands
    };
    for (int t : {T_BBASE, T_MBASE, T_RBASE})
        for (int i : myOf[t]) {
            const Ent& b = E[i];
            if (near(b.x, b.y, b.size)) return true;
        }
    for (const Task& t : tasks)
        if (t.type == T_RBASE || t.type == T_BBASE || t.type == T_MBASE)
            if (near(t.x, t.y, P[t.type].size)) return true;
    return false;
}

bool isBuildingCell(int c) { return (blk[c] && memRes[c] <= 0) || site[c]; }

int bldTypeAt(int c) {
    if (site[c]) {
        int x = cxo(c), y = cyo(c);
        for (const Task& t : tasks) {
            int s = P[t.type].size;
            if (t.eid < 0 && x >= t.x && x < t.x + s && y >= t.y && y < t.y + s) return t.type;
        }
        return -1;
    }
    int o = cellE[c];
    if (o >= 0) return E[o].type;
    return T_WALL;  // a remembered building out of sight
}

// a free cell beside the square with a building (or the map edge) right behind it would make a 1-wide corridor
bool makesNarrow(int x, int y, int s, bool resIsWall) {
    for (int k = 0; k < s; ++k) {
        const int pts[4][4] = {{x + k, y - 1, x + k, y - 2},
                               {x + k, y + s, x + k, y + s + 1},
                               {x - 1, y + k, x - 2, y + k},
                               {x + s, y + k, x + s + 1, y + k}};
        for (const auto& p : pts) {
            if (!inMap(p[0], p[1])) continue;
            int c1 = cid(p[0], p[1]);
            if ((resIsWall ? blk[c1] : isBuildingCell(c1)) || site[c1]) continue;
            if (!inMap(p[2], p[3])) return true;
            int c2 = cid(p[2], p[3]);
            if ((resIsWall ? blk[c2] : isBuildingCell(c2)) || site[c2]) return true;
        }
    }
    return false;
}

vector<uint8_t> keepFreeC;
vector<int> apCut;  // free slots cut off from home (away from the DFS root) if this cell were blocked

// articulation points of the free cells (buildings and resources block), iterative Tarjan from home
void computeAP(const vector<uint8_t>& slotFree) {
    static vector<int> disc, low, par, sub, it, stk;
    disc.assign(NN, -1);
    low.assign(NN, 0);
    par.assign(NN, -1);
    sub.assign(NN, 0);
    it.assign(NN, 0);
    apCut.assign(NN, 0);
    int timer = 0;
    auto run = [&](int root) {
        stk.clear();
        stk.push_back(root);
        disc[root] = low[root] = timer++;
        sub[root] = slotFree[root];
        int rootKids = 0, maxKid = 0, sumKids = 0;
        while (!stk.empty()) {
            int v = stk.back();
            if (it[v] < 4) {
                int d = it[v]++;
                int nx = cxo(v) + DX[d], ny = cyo(v) + DY[d];
                if (!inMap(nx, ny)) continue;
                int u = cid(nx, ny);
                if (blk[u]) continue;
                if (disc[u] == -1) {
                    par[u] = v;
                    disc[u] = low[u] = timer++;
                    sub[u] = slotFree[u];
                    stk.push_back(u);
                } else if (u != par[v]) {
                    low[v] = min(low[v], disc[u]);
                }
            } else {
                stk.pop_back();
                int p = par[v];
                if (p < 0) continue;
                low[p] = min(low[p], low[v]);
                sub[p] += sub[v];
                if (p == root) {
                    rootKids++;
                    maxKid = max(maxKid, sub[v]);
                    sumKids += sub[v];
                } else if (low[v] >= disc[p]) {
                    apCut[p] += sub[v];
                }
            }
        }
        apCut[root] = rootKids >= 2 ? sumKids - maxKid : 0;
    };
    int root = -1;
    for (int i : myOf[T_BBASE]) {
        const Ent& b = E[i];
        for (int yy = b.y - 1; yy <= b.y + b.size && root < 0; ++yy)
            for (int xx = b.x - 1; xx <= b.x + b.size; ++xx)
                if (inMap(xx, yy) && !blk[cid(xx, yy)] && sqDist(b.x, b.y, b.size, xx, yy, 1) == 1) {
                    root = cid(xx, yy);
                    break;
                }
    }
    if (root < 0)
        for (int i : myOf[T_BUILDER]) {
            root = E[i].c();
            break;
        }
    if (root >= 0 && !blk[root]) run(root);
    for (int c = 0; c < NN; ++c)
        if (!blk[c] && disc[c] == -1) run(c);
}
void computeKeepFree() {
    keepFreeC.assign(NN, 0);
    for (int c = 0; c < NN; ++c) keepFreeC[c] = keepFree(c) ? 1 : 0;
    baseAdj.assign(NN, 0);
    for (int t : {T_BBASE, T_RBASE, T_MBASE})
        for (int i : myOf[t]) {
            const Ent& b = E[i];
            for (int yy = b.y - 1; yy <= b.y + b.size; ++yy)
                for (int xx = b.x - 1; xx <= b.x + b.size; ++xx)
                    if (inMap(xx, yy) && sqDist(b.x, b.y, b.size, xx, yy, 1) == 1) baseAdj[cid(xx, yy)] = 1;
        }
}
// number of separate blocked runs on the ring around a square (out of map counts as blocked)
int ringRuns(int x, int y, int s, int& ringRes) {
    std::vector<std::pair<int, int>> cells;
    cells.reserve(4 * s + 4);
    for (int xx = x - 1; xx <= x + s; ++xx) cells.push_back({xx, y - 1});
    for (int yy = y; yy <= y + s; ++yy) cells.push_back({x + s, yy});
    for (int xx = x + s - 1; xx >= x - 1; --xx) cells.push_back({xx, y + s});
    for (int yy = y + s - 1; yy >= y; --yy) cells.push_back({x - 1, yy});
    ringRes = 0;
    int m = (int)cells.size();
    vector<uint8_t> b(m);
    for (int k = 0; k < m; ++k) {
        int xx = cells[k].first, yy = cells[k].second;
        if (!inMap(xx, yy)) {
            b[k] = 1;
            continue;
        }
        int c = cid(xx, yy);
        bool res = memRes[c] > 0;
        if (res) ringRes++;
        bool building = (blk[c] && !res) || site[c];
        b[k] = building ? 1 : 0;
    }
    int runs = 0;
    for (int k = 0; k < m; ++k)
        if (b[k] && !b[(k + m - 1) % m]) runs++;
    if (runs == 0) {
        bool all = true;
        for (int k = 0; k < m; ++k)
            if (!b[k]) all = false;
        if (all) runs = 1;
    }
    return runs;
}

vector<int> builderDist;
bool relaxSites = false;
bool findSiteOnce(int type, int& bx, int& by);
bool findSite(int type, int& bx, int& by) {
    relaxSites = false;
    if (findSiteOnce(type, bx, by)) return true;
    relaxSites = true;
    bool ok = findSiteOnce(type, bx, by);
    relaxSites = false;
    return ok;
}
bool findSiteOnce(int type, int& bx, int& by) {
    int s = P[type].size;
    int best = INF;
    int rej[8] = {0, 0, 0, 0, 0, 0, 0, 0};
    {
        vector<int> src;
        for (int c : lastMinerCells)
            if (c >= 0 && c < NN && !blk[c]) src.push_back(c);
        if (src.empty())
            for (int i : myOf[T_BUILDER]) src.push_back(E[i].c());
        bfsField(builderDist, src, [&](int c) { return !blk[c]; });
    }
    int baseX = 7, baseY = 7;
    for (int i : myOf[T_BBASE]) {
        baseX = E[i].x + 2;
        baseY = E[i].y + 2;
    }
    bx = by = -1;
    int lim = min(N - s, relaxSites ? 50 : (type == T_HOUSE ? 32 : 40));
    for (int y = 0; y <= lim; ++y)
        for (int x = 0; x <= lim; ++x) {
            if (!siteCellsOk(x, y, s, true)) { rej[0]++; continue; }
            {
                bool badSite = false;
                for (const BadSite& b : badSites)
                    if (b.until > tick && x < b.x + b.s && b.x < x + s && y < b.y + b.s && b.y < y + s) badSite = true;
                if (badSite) continue;
            }
            if (resvX >= 0 && type != T_RBASE) {
                int rs = P[T_RBASE].size;
                if (x <= resvX + rs + 1 && resvX <= x + s + 1 && y <= resvY + rs + 1 && resvY <= y + s + 1) continue;
            }
            bool bad = false;
            int units = 0;
            for (int yy = y; yy < y + s && !bad; ++yy)
                for (int xx = x; xx < x + s; ++xx) {
                    int c = cid(xx, yy);
                    if (keepFree(c) || dNext[c] > 0) {
                        bad = true;
                        break;
                    }
                    if (cellE[c] >= 0) units++;
                }
            if (bad) { rej[1]++; continue; }
            // touching: houses may touch houses and the map edge, nothing else touches anything
            int touch = 0;
            for (int k = 0; k < s && !bad; ++k) {
                const int pts[4][2] = {{x + k, y - 1}, {x + k, y + s}, {x - 1, y + k}, {x + s, y + k}};
                for (const auto& p : pts) {
                    if (!inMap(p[0], p[1])) {
                        touch++;
                        continue;
                    }
                    int c1 = cid(p[0], p[1]);
                    if (!isBuildingCell(c1)) continue;
                    int bt = bldTypeAt(c1);
                    bool okTouch = type == T_HOUSE && (bt == T_HOUSE || (knob("TIGHT", 1) > 0 && (bt == T_BBASE || bt == T_RBASE || bt == T_MBASE)));
                    if (!okTouch) {
                        bad = true;
                        break;
                    }
                    touch++;
                }
            }
            if (bad) { rej[2]++; continue; }
            if (!relaxSites && makesNarrow(x, y, s, type == T_HOUSE && knob("RESWALL", 1) > 0)) { rej[3]++; continue; }
            if (type == T_HOUSE && knob("TIGHT", 1) > 0) {
                // every base keeps room to spawn: four free cells around it besides this house
                bool cramped = false;
                for (int bt : {T_BBASE, T_RBASE, T_MBASE})
                    for (int bi : myOf[bt]) {
                        const Ent& b = E[bi];
                        if (sqDist(b.x, b.y, b.size, x, y, s) > 1) continue;
                        int freeAdj = 0;
                        for (int yy = b.y - 1; yy <= b.y + b.size; ++yy)
                            for (int xx = b.x - 1; xx <= b.x + b.size; ++xx) {
                                if (!inMap(xx, yy) || sqDist(b.x, b.y, b.size, xx, yy, 1) != 1) continue;
                                if (xx >= x && xx < x + s && yy >= y && yy < y + s) continue;
                                int c3 = cid(xx, yy);
                                if (!blk[c3] && !site[c3]) freeAdj++;
                            }
                        if (freeAdj < 4) cramped = true;
                    }
                if (cramped) continue;
            }
            if (type != T_HOUSE) {
                // a base needs open exits on its outward sides
                int open = 0, all = 0;
                for (int k = 0; k < s; ++k)
                    for (int dd = 1; dd <= 2; ++dd) {
                        const int pts[2][2] = {{x + s - 1 + dd, y + k}, {x + k, y + s - 1 + dd}};
                        for (const auto& p : pts) {
                            all++;
                            if (inMap(p[0], p[1]) && !blk[cid(p[0], p[1])]) open++;
                        }
                    }
                if (open * 10 < all * 6) { rej[4]++; continue; }
            }
            int ringRes = 0;
            int runs = ringRuns(x, y, s, ringRes);
            if (runs > 1) { rej[5]++; continue; }
            int score;
            if (type == T_HOUSE) {
                int bd = INF;
                for (int k = 0; k < s; ++k) {
                    const int pts[4][2] = {{x + k, y - 1}, {x + k, y + s}, {x - 1, y + k}, {x + s, y + k}};
                    for (const auto& p : pts)
                        if (inMap(p[0], p[1])) bd = min(bd, builderDist[cid(p[0], p[1])]);
                }
                if (bd >= INF) continue;  // nobody can get there
                int slotsCovered = 0;
                for (int yy = y; yy < y + s; ++yy)
                    for (int xx = x; xx < x + s; ++xx) {
                        int c2 = cid(xx, yy);
                        for (int d = 0; d < 4; ++d) {
                            int ax = xx + DX[d], ay = yy + DY[d];
                            if (inMap(ax, ay) && memRes[cid(ax, ay)] > 0) {
                                slotsCovered++;
                                break;
                            }
                        }
                        (void)c2;
                    }
                int db = std::abs(x + 1 - baseX) + std::abs(y + 1 - baseY);
                // miners right beside the site repair it without walking
                int minersNear = 0;
                for (int c3 : lastMinerCells)
                    if (sqDist(x, y, s, cxo(c3), cyo(c3), 1) <= 1) minersNear++;
                score = bd * 6 + db * 2 + units * 8 - touch * 3 + slotsCovered * 3 - min(minersNear, 4) * (int)knob("MINERS_NEAR", 15);
            } else if (type == T_TURRET) {
                bool reach = false;
                for (int k = 0; k < s && !reach; ++k) {
                    const int pts[4][2] = {{x + k, y - 1}, {x + k, y + s}, {x - 1, y + k}, {x + s, y + k}};
                    for (const auto& p : pts)
                        if (inMap(p[0], p[1]) && builderDist[cid(p[0], p[1])] < INF) reach = true;
                }
                if (!reach) continue;
                int d0 = std::abs(x - baseX) + std::abs(y - baseY);
                if (d0 < (int)knob("TUR_DMIN", 7) || d0 > (int)knob("TUR_DMAX", 18)) continue;
                int spacing = INF;
                for (int ti : myOf[T_TURRET]) spacing = min(spacing, std::abs(E[ti].x - x) + std::abs(E[ti].y - y));
                for (const Task& tt : tasks)
                    if (tt.type == T_TURRET) spacing = min(spacing, std::abs(tt.x - x) + std::abs(tt.y - y));
                if (spacing < (int)knob("TUR_SPACE", 7)) continue;
                int rc = rallyC >= 0 ? rallyC : cid(min(N - 1, 19), min(N - 1, 19));
                score = (std::abs(x - cxo(rc)) + std::abs(y - cyo(rc))) * 10 + units * 8;
            } else {
                bool reach = false;
                for (int k = 0; k < s && !reach; ++k) {
                    const int pts[4][2] = {{x + k, y - 1}, {x + k, y + s}, {x - 1, y + k}, {x + s, y + k}};
                    for (const auto& p : pts)
                        if (inMap(p[0], p[1]) && builderDist[cid(p[0], p[1])] < INF) reach = true;
                }
                if (!reach) { rej[6]++; continue; }
                int ccx = x + s / 2, ccy = y + s / 2;
                score = (std::abs(ccx - baseX) + std::abs(ccy - baseY)) * 10 + ringRes * 2 + units * 8;
            }
            if (score < best) {
                best = score;
                bx = x;
                by = y;
            }
        }
    if (dbgLevel >= 3 && bx < 0 && type != T_HOUSE && tick == 200) {
        for (int y = 0; y < 45; ++y) {
            std::string row;
            for (int x = 0; x < 60; ++x) {
                int c = cid(x, y);
                char ch = !seenEver[c] ? ' ' : blk[c] ? (memRes[c] > 0 ? '#' : 'H') : site[c] ? '+' : '.';
                int o = cellE[c];
                if (o >= 0 && isUnitType(E[o].type)) ch = E[o].owner == me ? 'u' : 'E';
                row += ch;
            }
            std::fprintf(stderr, "Z %s\n", row.c_str());
        }
    }
    if (dbgLevel >= 1 && bx < 0 && type != T_HOUSE && tick % 10 == 0)
        std::fprintf(stderr, "t%d site type %d relax %d: cells %d keepFree/danger %d touch %d narrow %d exits %d runs %d reach %d\n", tick, type,
                     (int)relaxSites, rej[0], rej[1], rej[2], rej[3], rej[4], rej[5], rej[6]);
    return bx >= 0;
}

bool siteStillOk(const Task& t) {
    int s = P[t.type].size;
    if (t.x < 0 || t.y < 0 || t.x + s > N || t.y + s > N) return false;
    for (int yy = t.y; yy < t.y + s; ++yy)
        for (int xx = t.x; xx < t.x + s; ++xx) {
            int c = cid(xx, yy);
            if (blk[c]) return false;
            if (enemyUnitAt(c)) return false;
        }
    return true;
}

bool siteEmpty(const Task& t) {
    int s = P[t.type].size;
    for (int yy = t.y; yy < t.y + s; ++yy)
        for (int xx = t.x; xx < t.x + s; ++xx)
            if (cellE[cid(xx, yy)] >= 0) return false;
    return true;
}

// ------------------------------------------------------------------ macro
int nB = 0, nR = 0, nM = 0;
int targetBuilders = 60;
int reserveRes = 0;
bool threatened = false;
double threatPow = 0;
bool wantRangedFirst = false;
int armyWanted = 0;

double unitPow(const Ent& e) {
    if (!e.active) return 0;
    if (e.type == T_RANGED) return 0.55 + 0.45 * e.hp / max(1, P[T_RANGED].maxHp);
    if (e.type == T_MELEE) return 0.5 + 0.5 * e.hp / max(1, P[T_MELEE].maxHp);
    if (e.type == T_TURRET) return 1.0 + 2.0 * e.hp / max(1, P[T_TURRET].maxHp);
    return 0;
}

void maintainTasks() {
    for (size_t k = 0; k < tasks.size();) {
        Task& t = tasks[k];
        bool remove = false;
        if (t.eid >= 0) {
            auto f = byId.find(t.eid);
            if (f == byId.end() || E[f->second].owner != me)
                remove = true;
            else if (E[f->second].active)
                remove = true;
        } else {
            int o = (t.x >= 0 && t.y >= 0 && t.x < N && t.y < N) ? cellE[cid(t.x, t.y)] : -1;
            if (o >= 0 && E[o].owner == me && E[o].type == t.type && E[o].x == t.x && E[o].y == t.y) {
                t.eid = E[o].id;
                if (E[o].active) remove = true;
            } else if (!siteStillOk(t)) {
                if (++t.invalid > 2) remove = true;
            } else {
                t.invalid = 0;
            }
            if (tick - t.created > 250) remove = true;
            if (t.placer < 0 && t.type == T_HOUSE) {
                if (++t.noPlacer > 15) remove = true;
            } else
                t.noPlacer = 0;
            if (t.type == T_HOUSE && tick - t.created > 60) remove = true;
            if (remove) badSites.push_back({t.x, t.y, P[t.type].size, tick + 100});
        }
        if (remove)
            tasks.erase(tasks.begin() + k);
        else
            ++k;
    }
    site.assign(NN, 0);
    for (const Task& t : tasks)
        if (t.eid < 0) {
            int s = P[t.type].size;
            for (int yy = t.y; yy < t.y + s; ++yy)
                for (int xx = t.x; xx < t.x + s; ++xx)
                    if (inMap(xx, yy)) site[cid(xx, yy)] = 1;
        }
}

int countTasks(int type, bool unplacedOnly) {
    int n = 0;
    for (const Task& t : tasks)
        if (t.type == type && (!unplacedOnly || t.eid < 0)) n++;
    return n;
}

void addTask(int type) {
    int x, y;
    if (type == T_RBASE && resvX >= 0 && siteCellsOk(resvX, resvY, P[T_RBASE].size, true)) {
        x = resvX;
        y = resvY;
        resvX = resvY = -1;
    } else if (!findSite(type, x, y)) {
        if (dbgLevel >= 1 && tick % 10 == 0) std::fprintf(stderr, "t%d no site for type %d\n", tick, type);
        return;
    }
    Task t;
    t.type = type;
    t.x = x;
    t.y = y;
    t.created = tick;
    tasks.push_back(t);
    int s = P[type].size;
    for (int yy = y; yy < y + s; ++yy)
        for (int xx = x; xx < x + s; ++xx) site[cid(xx, yy)] = 1;
}

void planMacro() {
    nB = (int)myOf[T_BUILDER].size();
    nR = (int)myOf[T_RANGED].size();
    nM = (int)myOf[T_MELEE].size();
    maintainTasks();

    // threats near home
    threatPow = 0;
    for (int i : enemies) {
        const Ent& e = E[i];
        if (!isArmyType(e.type) && e.type != T_TURRET) continue;
        int c = e.c();
        if (homeD[c] <= 25 || nearB[c]) threatPow += unitPow(e) + (e.type == T_TURRET ? 0 : 0);
    }
    double myArmy = 0;
    for (int t : {T_RANGED, T_MELEE})
        for (int i : myOf[t]) myArmy += unitPow(E[i]);
    threatened = threatPow > 0.1;

    if (fmt == F_ROUND1) targetBuilders = 70;
    else if (fmt == F_ROUND2) targetBuilders = 70;
    else targetBuilders = 70;
    if (tick > 650) targetBuilders = min(targetBuilders, nB);

    bool haveRBase = !myOf[T_RBASE].empty() || countTasks(T_RBASE, false) > 0;
    bool rbaseActive = false;
    for (int i : myOf[T_RBASE])
        if (E[i].active) rbaseActive = true;
    bool bbaseActive = false;
    for (int i : myOf[T_BBASE])
        if (E[i].active) bbaseActive = true;

    // army wanted
    double ratio = 0;
    if (fmt == F_ROUND1) ratio = tick < 120 ? knob("R1_EARLY", 0.15) : (tick < 250 ? knob("R1_MID", 0.5) : 0.8);
    else ratio = tick < 200 ? 0.3 : (tick < 300 ? 0.6 : 0.9);
    armyWanted = (int)(nB * ratio);
    int threatNeed = (int)std::ceil(threatPow * 1.4) + (threatened ? 2 : 0);
    armyWanted = max(armyWanted, threatNeed);
    if (nB >= targetBuilders || tick > 650) armyWanted = 1000;
    wantRangedFirst = (threatened && myArmy < threatPow * 1.4 + 1) || nR + nM < armyWanted;

    // buildings
    if (resvX >= 0) {
        bool ok = true;
        int rs = P[T_RBASE].size;
        for (int yy = resvY; yy < resvY + rs && ok; ++yy)
            for (int xx = resvX; xx < resvX + rs; ++xx)
                if (!inMap(xx, yy) || blk[cid(xx, yy)]) {
                    ok = false;
                    break;
                }
        if (!ok) resvX = resvY = -1;
    }
    if (fmt != F_ROUND1 && !haveRBase && resvX < 0 && tick >= 40 && tick % 5 == 0) {
        int x, y;
        if (findSite(T_RBASE, x, y)) {
            resvX = x;
            resvY = y;
        }
    }
    if (haveRBase) resvX = resvY = -1;
    if (myOf[T_BBASE].empty() && countTasks(T_BBASE, false) == 0 && nB > 0 && myRes >= 300) addTask(T_BBASE);
    // turrets: a few on the side the enemy comes from, once the army has its base
    {
        int maxTurrets = (int)knob("TURRETS", 0);
        if (maxTurrets > 0 && rbaseActive && nB >= (int)knob("TUR_MINB", 30) && tick >= (int)knob("TUR_T", 220)) {
            int want = min(maxTurrets, (tick - (int)knob("TUR_T", 220)) / (int)knob("TUR_EVERY", 50) + 1);
            int have = (int)myOf[T_TURRET].size() + countTasks(T_TURRET, false);
            if (have < want && countTasks(T_TURRET, true) == 0) addTask(T_TURRET);
        }
    }
    if (fmt != F_ROUND1 && !haveRBase && nB > 0) {
        int at = (int)knob(fmt == F_FINALS ? "RB_AT_F" : "RB_AT_R2", fmt == F_FINALS ? 40 : 50);
        int atTick = (int)knob(fmt == F_FINALS ? "RB_TICK_F" : "RB_TICK_R2", fmt == F_FINALS ? 210 : 240);
        if (nB >= at || tick >= atTick || (threatened && nB >= 12)) addTask(T_RBASE);
    }
    // houses
    int pendingHouses = countTasks(T_HOUSE, true);
    for (int i : myOf[T_HOUSE])
        if (!E[i].active) pendingHouses++;
    int producers = (bbaseActive && nB < targetBuilders ? 1 : 0) + (rbaseActive ? 1 : 0);
    double cost = rbaseActive && nB >= targetBuilders ? 30.0 + nR : 10.0 + nB;
    double rate = min((double)producers, max(0.1, max(1, lastMining) / cost));
    int buffer = max(2, min(28, (int)std::ceil(rate * 30) + (nB >= 12 ? 4 : 1)));
    int popFree = popProv - popUsed;
    int jitUntil = (int)knob("JIT_UNTIL", 12);
    int maxConcurrent = nB < jitUntil ? 1 : (nB < 6 ? 1 : 2 + nB / 12);
    bool want = popFree + 5 * pendingHouses < buffer;
    if (nB < jitUntil) want = popFree + 5 * pendingHouses <= (int)knob("JIT_FREE", 1);  // early: one at a time, just in time
    if (producers > 0 && want && pendingHouses < maxConcurrent && nB > 0) addTask(T_HOUSE);
    // money piles up while the population is (nearly) full: one more house
    else if (producers > 0 && nB >= (int)knob("BANK_MINB", 6) && myRes >= (int)knob("BANK_RES", 120) &&
             popFree + 5 * countTasks(T_HOUSE, true) <= (int)knob("BANK_FREE", 4) && pendingHouses < maxConcurrent + 1)
        addTask(T_HOUSE);

    for (Task& t : tasks) t.builtNow = t.waiting = false;
    budget = myRes;
    computeKeepFree();
}

// money: big buildings are saved for; a house only when its placer waits at the site or the population is full
void computeReserve() {
    reserveRes = 0;
    int popFree = popProv - popUsed;
    bool houseDone = false;
    for (const Task& t : tasks) {
        if (t.eid >= 0 || t.builtNow) continue;
        if (t.type == T_RBASE || t.type == T_BBASE || t.type == T_MBASE)
            reserveRes += P[t.type].cost;
        else if (t.type == T_HOUSE && !houseDone &&
                 (popFree <= 0 || (nB >= 12 && (popFree <= 1 || (t.waiting && popFree <= 3))))) {
            reserveRes += P[t.type].cost;
            houseDone = true;
        }
    }
}

// ------------------------------------------------------------------ combat
double targetPriority(const Ent& t) {
    switch (t.type) {
        case T_RANGED: return 100;
        case T_MELEE: return 75;
        case T_TURRET: return t.active ? 60 : 30;
        case T_BUILDER: return 50;
        case T_HOUSE: return 24;
        case T_RBASE: return 22;
        case T_BBASE: return 21;
        case T_MBASE: return 20;
        case T_WALL: return 5;
    }
    return 1;
}

void planCombat() {
    struct Sh {
        int ei, dmg;
        vector<int> tg;
    };
    vector<Sh> sh;
    for (int t : {T_RANGED, T_MELEE, T_TURRET})
        for (int i : myOf[t]) {
            const Ent& u = E[i];
            if (!u.active) continue;
            Sh s;
            s.ei = i;
            s.dmg = P[t].dmg;
            int r = P[t].range;
            for (int j : enemies) {
                const Ent& e = E[j];
                if (!e.visible) continue;
                if (sqDist(u.x, u.y, u.size, e.x, e.y, e.size) <= r) s.tg.push_back(j);
            }
            if (!s.tg.empty()) sh.push_back(std::move(s));
        }
    if (sh.empty()) return;
    std::unordered_map<int, int> potential, remHp;
    for (const Sh& s : sh)
        for (int j : s.tg) potential[j] += s.dmg;
    vector<int> tl;
    for (const auto& kv : potential) tl.push_back(kv.first);
    std::sort(tl.begin(), tl.end(), [&](int a, int b) {
        const Ent& A = E[a];
        const Ent& B = E[b];
        bool ka = isUnitType(A.type) && potential[a] >= A.hp, kb = isUnitType(B.type) && potential[b] >= B.hp;
        if (ka != kb) return ka;
        double pa = targetPriority(A), pb = targetPriority(B);
        if (pa != pb) return pa > pb;
        if (A.hp != B.hp) return A.hp < B.hp;
        return A.id < B.id;
    });
    for (int j : tl) remHp[j] = E[j].hp;
    vector<int> assigned(sh.size(), -1);
    vector<int> order(sh.size());
    for (size_t k = 0; k < sh.size(); ++k) order[k] = (int)k;
    std::sort(order.begin(), order.end(), [&](int a, int b) { return sh[a].tg.size() < sh[b].tg.size(); });
    for (int j : tl) {
        for (int k : order) {
            if (remHp[j] <= 0) break;
            if (assigned[k] >= 0) continue;
            const Sh& s = sh[k];
            if (std::find(s.tg.begin(), s.tg.end(), j) == s.tg.end()) continue;
            assigned[k] = j;
            remHp[j] -= s.dmg;
        }
    }
    for (size_t k = 0; k < sh.size(); ++k) {
        if (assigned[k] < 0) {
            int best = -1;
            double bp = -1;
            for (int j : sh[k].tg) {
                double p = targetPriority(E[j]) + (remHp[j] > 0 ? 1000 : 0);
                if (p > bp) {
                    bp = p;
                    best = j;
                }
            }
            assigned[k] = best;
            if (best >= 0) remHp[best] -= sh[k].dmg;
        }
        int ei = sh[k].ei;
        if (assigned[k] >= 0) setAct(ei, actAttack(E[assigned[k]].id), true);
    }
    for (int j : tl)
        if (remHp[j] <= 0 && isArmyType(E[j].type)) seenArmy.erase(E[j].id);
}

// one step towards the nearest free cell that touches the square (x, y, s); false when there is none
vector<uint8_t> standMark;
bool walkToStand(int ei, int x, int y, int s, int prio) {
    const Ent& u = E[ei];
    int start = u.c();
    auto isStand = [&](int c) {
        int cx = cxo(c), cy = cyo(c);
        return sqDist(x, y, s, cx, cy, 1) == 1 && !blk[c] && !site[c] && !stayC[c] && !enemyUnitAt(c);
    };
    auto pass = [&](int c) { return !blk[c] && !site[c] && !stayC[c] && !enemyUnitAt(c) && !(u.type == T_BUILDER && dNext[c] > 0); };
    int g = bfsFind(start, pass, isStand, 3000);
    if (g < 0) return false;
    return addPathReq(ei, prio, start, g, pass);
}

int bestAdjRes(int c) {
    int x = cxo(c), y = cyo(c);
    int best = -1, bh = INF;
    for (int d = 0; d < 4; ++d) {
        int nx = x + DX[d], ny = y + DY[d];
        if (!inMap(nx, ny)) continue;
        int o = cellE[cid(nx, ny)];
        if (o >= 0 && E[o].type == T_RES && E[o].hp < bh) {
            bh = E[o].hp;
            best = o;
        }
    }
    return best;
}

void planBuilders() {
    vector<int>& bl = myOf[T_BUILDER];
    // 1. flee
    for (int i : bl) {
        const Ent& b = E[i];
        int c = b.c();
        if (dNext[c] <= 0) continue;
        int best = -1, bk = INF;
        int opts[4], n = 0;
        std::pair<int, int> cand[4];
        int m = 0;
        for (int d = 0; d < 4; ++d) {
            int nx = b.x + DX[d], ny = b.y + DY[d];
            if (!inMap(nx, ny)) continue;
            int nc = cid(nx, ny);
            if (blk[nc] || enemyUnitAt(nc)) continue;
            if (dNext[nc] >= dNext[c]) continue;
            cand[m++] = {dNext[nc] * 64 + min(63, homeD[nc]), nc};
        }
        (void)best;
        (void)bk;
        std::sort(cand, cand + m);
        for (int k = 0; k < m; ++k) opts[n++] = cand[k].second;
        if (n > 0)
            addReq(i, 100, opts, n);
        else {
            int r = bestAdjRes(c);
            if (r >= 0)
                setAct(i, actAttack(E[r].id), true);
            else
                setAct(i, actNone(), true);
        }
    }
    // 2. construction tasks
    std::unordered_map<int, int> taskOf;  // builder id -> task index
    for (size_t k = 0; k < tasks.size(); ++k) {
        Task& t = tasks[k];
        if (t.placer >= 0) taskOf[t.placer] = (int)k;
        for (int h : t.helpers) taskOf[h] = (int)k;
    }
    vector<int> F;
    for (size_t k = 0; k < tasks.size(); ++k) {
        Task& t = tasks[k];
        int s = P[t.type].size;
        vector<int> stand;
        for (int yy = t.y - 1; yy <= t.y + s; ++yy)
            for (int xx = t.x - 1; xx <= t.x + s; ++xx) {
                if (!inMap(xx, yy)) continue;
                if (sqDist(t.x, t.y, s, xx, yy, 1) != 1) continue;
                int c = cid(xx, yy);
                if (blk[c] || site[c]) continue;
                stand.push_back(c);
            }
        if (stand.empty()) continue;
        bfsField(F, stand, [&](int c) { return !blk[c] && !site[c] && !enemyUnitAt(c); });
        auto alive = [&](int id) -> int {
            auto f = byId.find(id);
            if (f == byId.end()) return -1;
            int ei = f->second;
            if (E[ei].owner != me || E[ei].type != T_BUILDER) return -1;
            return ei;
        };
        auto pickNearest = [&](int maxD) -> int {
            int best = -1, bd = maxD + 1;
            for (int i : bl) {
                if (decided[i] || taskOf.count(E[i].id)) continue;
                int d = F[E[i].c()];
                if (d < bd) {
                    bd = d;
                    best = i;
                }
            }
            return best;
        };
        if (t.eid < 0) {
            int pi = t.placer >= 0 ? alive(t.placer) : -1;
            if (pi >= 0 && F[E[pi].c()] >= INF) {
                taskOf.erase(t.placer);
                t.placer = -1;
                pi = -1;
            }
            bool goNow = t.type == T_HOUSE || budget * 10 >= P[t.type].cost * 7;
            if (pi < 0 && goNow) {
                pi = pickNearest(60);
                if (pi >= 0) {
                    t.placer = E[pi].id;
                    taskOf[t.placer] = (int)k;
                } else
                    t.placer = -1;
            }
            if (dbgLevel >= 3 && tick % 10 == 0 && tick - t.created > 20)
                std::fprintf(stderr, "t%d stuck house (%d,%d) placer %d at %d,%d decided %d F %d budget %d empty %d\n", tick, t.x, t.y, t.placer,
                             pi >= 0 ? E[pi].x : -1, pi >= 0 ? E[pi].y : -1, pi >= 0 ? (int)decided[pi] : -1, pi >= 0 ? F[E[pi].c()] : -1, budget,
                             (int)siteEmpty(t));
            if (pi >= 0 && !decided[pi]) {
                int c = E[pi].c();
                if (F[c] == 0) {
                    int cost = P[t.type].cost;
                    if (budget >= cost && siteEmpty(t)) {
                        setAct(pi, actBuild(t.type, t.x, t.y), true);
                        budget -= cost;
                        t.builtNow = true;
                    } else {
                        t.waiting = true;
                        int r = bestAdjRes(c);
                        setAct(pi, r >= 0 ? actAttack(E[r].id) : actNone(), true);
                    }
                } else if (!walkToStand(pi, t.x, t.y, s, 60)) {
                    int r = bestAdjRes(c);
                    setAct(pi, r >= 0 ? actAttack(E[r].id) : actNone(), true);
                }
            }
            // helpers of a house walk to it a few ticks before it can be placed
            if (knob("PREHELP", 0) > 0 && t.type == T_HOUSE && pi >= 0 &&
                (t.waiting || budget + max(1, lastMining) * (int)knob("PREHELP", 0) >= P[t.type].cost)) {
                vector<int> keep;
                for (int h : t.helpers)
                    if (h != t.placer && alive(h) >= 0) keep.push_back(h);
                t.helpers = keep;
                while ((int)t.helpers.size() < 3) {
                    int hi = pickNearest(8);
                    if (hi < 0) break;
                    t.helpers.push_back(E[hi].id);
                    taskOf[E[hi].id] = (int)k;
                }
                for (int h : t.helpers) {
                    int hi = alive(h);
                    if (hi < 0 || decided[hi]) continue;
                    int c = E[hi].c();
                    if (sqDist(t.x, t.y, s, E[hi].x, E[hi].y, 1) == 1 || !walkToStand(hi, t.x, t.y, s, 44)) {
                        int r = bestAdjRes(c);
                        setAct(hi, r >= 0 ? actAttack(E[r].id) : actNone(), true);
                    }
                }
            }
        } else {
            auto f = byId.find(t.eid);
            if (f == byId.end()) continue;
            int bi = f->second;
            int want = t.type == T_HOUSE ? (int)knob("HELP_N", 3) : (t.type == T_TURRET ? 3 : 8);
            vector<int> keep;
            if (t.placer >= 0 && alive(t.placer) >= 0) keep.push_back(t.placer);
            for (int h : t.helpers)
                if (h != t.placer && alive(h) >= 0) keep.push_back(h);
            t.helpers = keep;
            t.placer = -1;
            while ((int)t.helpers.size() < want) {
                int hi = pickNearest(t.type == T_HOUSE ? (int)knob("HELP_R", 8) : 20);
                if (hi < 0) break;
                t.helpers.push_back(E[hi].id);
                taskOf[E[hi].id] = (int)k;
            }
            for (int h : t.helpers) {
                int hi = alive(h);
                if (hi < 0 || decided[hi]) continue;
                int c = E[hi].c();
                const Ent& b = E[bi];
                if (sqDist(b.x, b.y, b.size, E[hi].x, E[hi].y, 1) == 1) {
                    setAct(hi, actRepair(b.id), true);
                } else if (!walkToStand(hi, b.x, b.y, b.size, 45)) {
                    int r = bestAdjRes(c);
                    setAct(hi, r >= 0 ? actAttack(E[r].id) : actNone(), true);
                }
            }
        }
    }
    // 3. repair damaged active buildings near home (bases, turrets)
    // (left out in v1)

    // 3b. a scout: under fog, one builder looks at the ground around home that nobody has seen
    bool needLook = resvX < 0 && myOf[T_RBASE].empty() && countTasks(T_RBASE, false) == 0;
    if (!needLook && scoutId >= 0) scoutId = -1;
    if (fog && !scoutDone && needLook && tick >= (int)knob("SCOUT_T", 50) && (int)bl.size() >= 5) {
        int si = -1;
        if (scoutId >= 0) {
            auto f = byId.find(scoutId);
            if (f != byId.end() && E[f->second].owner == me && E[f->second].type == T_BUILDER) si = f->second;
        }
        int radius = (int)knob("SCOUT_R", 34);
        int bx0 = 7, by0 = 7;
        for (int i : myOf[T_BBASE]) {
            bx0 = E[i].x + 2;
            by0 = E[i].y + 2;
        }
        auto wanted = [&](int c) { return !seenEver[c] && std::abs(cxo(c) - bx0) + std::abs(cyo(c) - by0) <= radius; };
        if (si < 0) {
            // the builder nearest to unseen ground, not on a task
            std::unordered_map<int, int> busyT;
            for (const Task& t : tasks) {
                if (t.placer >= 0) busyT[t.placer] = 1;
                for (int h : t.helpers) busyT[h] = 1;
            }
            int best = -1, bd = INF;
            for (int i : bl) {
                if (decided[i] || busyT.count(E[i].id)) continue;
                int d = std::abs(E[i].x - bx0) + std::abs(E[i].y - by0);
                if (d < bd) {
                    bd = d;
                    best = i;
                }
            }
            si = best;
            scoutId = si >= 0 ? E[si].id : -1;
        }
        if (si >= 0 && !decided[si]) {
            int start = E[si].c();
            auto pass = [&](int c) { return !blk[c] && !site[c] && dNext[c] == 0 && !stayC[c] && !enemyUnitAt(c); };
            int g = bfsFind(start, pass, wanted, 6400);
            if (g < 0 || tick > (int)knob("SCOUT_END", 260)) {
                scoutDone = true;
                scoutId = -1;
            } else
                addPathReq(si, 15, start, g, pass);
        }
    }

    // 4. mining
    vector<uint8_t> slot(NN, 0), taken(NN, 0);
    vector<int> slotCells;
    for (int c = 0; c < NN; ++c) {
        if (blk[c] || site[c] || dNext[c] > 0) continue;
        int x = cxo(c), y = cyo(c);
        for (int d = 0; d < 4; ++d) {
            int nx = x + DX[d], ny = y + DY[d];
            if (!inMap(nx, ny)) continue;
            if (memRes[cid(nx, ny)] > 0) {
                slot[c] = 1;
                break;
            }
        }
    }
    {
        vector<uint8_t> slotFree(NN, 0);
        for (int c = 0; c < NN; ++c) {
            int o = cellE[c];
            slotFree[c] = slot[c] && !(o >= 0 && E[o].owner == me && E[o].type == T_BUILDER);
        }
        computeAP(slotFree);
    }
    if (dbgLevel >= 1 && tick % 50 == 0) {
        int raw = 0, ap = 0, near = 0, nearAp = 0;
        int bx = 7, by = 7;
        for (int i : myOf[T_BBASE]) { bx = E[i].x + 2; by = E[i].y + 2; }
        for (int c = 0; c < NN; ++c) {
            if (!slot[c]) continue;
            raw++;
            bool n = std::abs(cxo(c) - bx) + std::abs(cyo(c) - by) <= 25;
            if (n) near++;
            if (apCut[c] > 0) { ap++; if (n) nearAp++; }
        }
        std::fprintf(stderr, "t%d slots raw %d ap-excluded %d; within 25 of base %d (ap %d)\n", tick, raw, ap, near, nearAp);
    }
    if (dbgLevel >= 3 && tick == 150) {
        for (int y = 0; y < 32; ++y) {
            std::string row;
            for (int x = 0; x < 40; ++x) {
                int c = cid(x, y);
                char ch = blk[c] ? (memRes[c] > 0 ? '#' : 'H') : '.';
                if (slot[c]) ch = apCut[c] > 0 ? 'X' : 's';
                int o = cellE[c];
                if (o >= 0 && E[o].owner == me && E[o].type == T_BUILDER) ch = slot[c] ? (apCut[c] > 0 ? 'Y' : 'b') : 'u';
                row += ch;
            }
            std::fprintf(stderr, "S %s\n", row.c_str());
        }
    }
    for (int c = 0; c < NN; ++c) {
        if (slot[c] && apCut[c] > 0) slot[c] = 0;
        if (slot[c]) slotCells.push_back(c);
    }
    miningNow = 0;
    idleBuilders = 0;
    vector<int> freeB;
    vector<int> minerAt(NN, -1);
    for (int i : bl) {
        if (decided[i]) continue;
        int c = E[i].c();
        if (slot[c] && !taken[c]) {
            int r = bestAdjRes(c);
            if (r >= 0) {
                setAct(i, actAttack(E[r].id), true);
                taken[c] = 1;
                minerAt[c] = i;
                miningNow++;
                continue;
            }
        }
        freeB.push_back(i);
    }
    // what a slot still offers: its resources' health, shared with the miners already at them
    vector<int> minersAtRes(NN, 0);
    for (int c = 0; c < NN; ++c) {
        if (minerAt[c] < 0) continue;
        for (int d = 0; d < 4; ++d) {
            int nx = cxo(c) + DX[d], ny = cyo(c) + DY[d];
            if (inMap(nx, ny) && memRes[cid(nx, ny)] > 0) minersAtRes[cid(nx, ny)]++;
        }
    }
    vector<int> slotVal(NN, 0);
    for (int c : slotCells) {
        int v = 0;
        for (int d = 0; d < 4; ++d) {
            int nx = cxo(c) + DX[d], ny = cyo(c) + DY[d];
            if (!inMap(nx, ny)) continue;
            int r = cid(nx, ny);
            if (memRes[r] > 0) v += memRes[r] / (1 + minersAtRes[r]);
        }
        slotVal[c] = v;
    }
    int minVal = (int)knob("SLOT_MINVAL", 10);
    // what lies around a slot: resource health within 2 cells, shared by the miners within 2 cells
    vector<int> dens(NN, 0), mdens(NN, 0);
    for (int c = 0; c < NN; ++c) {
        int hp = memRes[c] > 0 ? memRes[c] : 0;
        bool mn = minerAt[c] >= 0;
        if (!hp && !mn) continue;
        forDiamond(cxo(c), cyo(c), 1, 2, [&](int c2, int) {
            dens[c2] += hp;
            if (mn) mdens[c2]++;
        });
    }
    double valRef = knob("VAL_REF", 240), valW = knob("VAL_W", 12), band = knob("VAL_BAND", 10);
    vector<int> visitedCells;
    vector<int> freeSlots;
    for (int c : slotCells)
        if (!taken[c] && !stayC[c]) freeSlots.push_back(c);
    bfsField(slotD, freeSlots, [&](int c) { return !blk[c] && !site[c] && dNext[c] == 0; });
    // builders keep the slot they walk to while it stays free: first those, then the rest by distance
    std::unordered_map<int, int> keepT;
    for (int i : freeB) {
        auto f = mineTarget.find(E[i].id);
        if (f == mineTarget.end()) continue;
        int t = f->second;
        if (t < 0 || t >= NN || !slot[t] || taken[t] || stayC[t]) continue;
        int o = cellE[t];
        if (o >= 0 && o != i && E[o].owner == me && E[o].type == T_BUILDER) continue;
        keepT[i] = t;
        taken[t] = 1;
    }
    std::sort(freeB.begin(), freeB.end(), [&](int a, int b) {
        bool ka = keepT.count(a), kb = keepT.count(b);
        if (ka != kb) return ka;
        return slotD[E[a].c()] < slotD[E[b].c()];
    });
    std::unordered_map<int, int> newTargets;
    for (int k = 0; k < 6; ++k) dbgWalk[k] = 0;
    auto walkLen = [&](int start, int g) {
        int n = 0;
        for (int c = g; c >= 0 && c != start && n < 200; c = bfsPar[c]) n++;
        return n;
    };
    auto countWalk = [&](int len) { dbgWalk[len <= 3 ? 0 : len <= 8 ? 1 : len <= 15 ? 2 : len <= 30 ? 3 : 4]++; };
    for (int i : freeB) {
        int start = E[i].c();
        auto pass = [&](int c) { return !blk[c] && !site[c] && dNext[c] == 0 && !stayC[c] && !enemyUnitAt(c); };
        {
            auto kt = keepT.find(i);
            if (kt != keepT.end()) {
                int t = kt->second;
                int g = bfsFind(start, pass, [&](int c) { return c == t; }, 4000);
                bool tooFar = g >= 0 && walkLen(start, g) > slotD[start] + (int)knob("RETARGET", 5);
                if (g >= 0 && !tooFar && addPathReq(i, 10, start, g, pass)) {
                    newTargets[E[i].id] = t;
                    countWalk(walkLen(start, g));
                    dbgKeptT++;
                    continue;
                }
                taken[t] = 0;
            }
        }
        if (site[start]) {
            // off a building site first, through the site if need be
            auto passS = [&](int c) { return !blk[c] && !stayC[c] && !enemyUnitAt(c); };
            int e = bfsFind(start, passS, [&](int c) { return !site[c] && !blk[c] && !stayC[c] && !enemyUnitAt(c); }, 400);
            if (e >= 0 && addPathReq(i, 20, start, e, passS)) continue;
        }
        int g = -1;
        if (slotD[start] < INF && valW > 0) {
            // the nearest slots first, but a rich field a little further beats a crumb next door
            bfsAll(start, pass, min(120, slotD[start] + (int)band + 2), visitedCells);
            int dmin = INF;
            for (int c : visitedCells)
                if (slot[c] && !taken[c] && !stayC[c] && c != start) dmin = min(dmin, bfsDistV[c]);
            double bestS = 1e18;
            for (int c : visitedCells) {
                if (!slot[c] || taken[c] || stayC[c] || c == start) continue;
                if (bfsDistV[c] > dmin + band) continue;
                double v = (double)dens[c] / (1 + mdens[c]);
                double sc = bfsDistV[c] + valW * (1.0 - min(1.0, v / valRef));
                if (sc < bestS) {
                    bestS = sc;
                    g = c;
                }
            }
        } else if (slotD[start] < INF) {
            g = bfsFind(start, pass, [&](int c) { return slot[c] && !taken[c] && !stayC[c] && (slotVal[c] >= minVal || mdist(c, start) <= 2); }, 4000);
            if (g < 0) g = bfsFind(start, pass, [&](int c) { return slot[c] && !taken[c] && !stayC[c]; }, 4000);
        }
        if (g >= 0) {
            taken[g] = 1;
            if (addPathReq(i, 10, start, g, pass)) {
                newTargets[E[i].id] = g;
                countWalk(walkLen(start, g));
                dbgNewT++;
                dbgNewTLen += walkLen(start, g);
                continue;
            }
        }
        if (g < 0 && slotD[start] < INF) {
            // the way to the free slots is barred by miners: the miner nearest to them goes on, this one takes its place
            auto pass2 = [&](int c) {
                return !blk[c] && !site[c] && dNext[c] == 0 && !enemyUnitAt(c) && (!stayC[c] || minerAt[c] >= 0);
            };
            int g2 = bfsFind(start, pass2, [&](int c) { return slot[c] && !taken[c] && !stayC[c]; }, 4000);
            if (g2 >= 0) {
                int prev = g2, c = bfsPar[g2], mcell = -1, mstep = -1;
                while (c >= 0 && c != start) {
                    if (minerAt[c] >= 0) {
                        mcell = c;
                        mstep = prev;
                        break;
                    }
                    prev = c;
                    c = bfsPar[c];
                }
                if (mcell >= 0) {
                    int m = minerAt[mcell];
                    minerAt[mcell] = -1;
                    stayC[mcell] = 0;
                    taken[g2] = 1;
                    int o1[1] = {mstep};
                    addReq(m, 12, o1, 1);
                    miningNow--;
                    int st = firstStep(start, mcell);
                    if (st >= 0 && (st == mcell || !stayC[st])) {
                        int o2[1] = {st};
                        addReq(i, 11, o2, 1);
                    } else
                        setAct(i, actNone(), true);
                    continue;
                }
            }
        }
        if (fog) {
            int e = bfsFind(start, pass, [&](int c) { return !seenEver[c]; }, 6400);
            if (e >= 0 && addPathReq(i, 5, start, e, pass)) {
                dbgWalk[5]++;
                continue;
            }
        }
        if (dbgLevel >= 3 && (tick == 300 || tick == 120))
            std::fprintf(stderr, "t%d idle builder at %d,%d slotD=%d g=%d freeSlots=%zu site=%d ap=%d\n", tick, E[i].x, E[i].y, slotD[start], g,
                         freeSlots.size(), (int)site[start], apCut[start]);
        idleBuilders++;
        if (apCut[start] > 0 || keepFreeC[start] || baseAdj[start]) {
            // nothing to do: at least do not stand in a corridor or next to a base
            int e = bfsFind(start, pass, [&](int c) { return apCut[c] == 0 && !keepFreeC[c] && !baseAdj[c] && !stayC[c]; }, 300);
            if (e >= 0 && addPathReq(i, 4, start, e, pass)) continue;
        }
        setAct(i, actNone(), true);
    }
    if (dbgLevel >= 3 && tick == 300) {
        vector<char> mk(NN, 0);
        for (const auto& kv : newTargets) {
            auto f = byId.find(kv.first);
            if (f == byId.end()) continue;
            int d = mdist(E[f->second].c(), kv.second);
            mk[E[f->second].c()] = d < 10 ? (char)('0' + d) : 'W';
            if (!mk[kv.second]) mk[kv.second] = '*';
        }
        for (int y = 0; y < 50; ++y) {
            std::string row;
            for (int x = 0; x < 60; ++x) {
                int c = cid(x, y);
                char ch = blk[c] ? (memRes[c] > 0 ? '#' : 'H') : (slot[c] ? (taken[c] ? 't' : 's') : '.');
                int o = cellE[c];
                if (o >= 0 && E[o].owner == me && E[o].type == T_BUILDER) {
                    auto f = acts.find(E[o].id);
                    ch = (f != acts.end() && f->second.attackAction) ? 'b' : 'u';
                }
                if (mk[c]) ch = mk[c];
                row += ch;
            }
            std::fprintf(stderr, "W %s\n", row.c_str());
        }
    }
    lastMining = miningNow;
    mineTarget.swap(newTargets);
    lastMinerCells.clear();
    for (int i : bl) {
        auto f = acts.find(E[i].id);
        if (f != acts.end() && f->second.attackAction) lastMinerCells.push_back(E[i].c());
    }
}

// ------------------------------------------------------------------ army
double myPowAt(int c, int r) {
    double p = 0;
    for (int t : {T_RANGED, T_MELEE})
        for (int i : myOf[t])
            if (mdist(E[i].c(), c) <= r) p += unitPow(E[i]);
    for (int i : myOf[T_TURRET]) {
        const Ent& u = E[i];
        if (sqDist(u.x, u.y, u.size, cxo(c), cyo(c), 1) <= r) p += unitPow(u);
    }
    return p;
}

double enPowAt(int c, int r) {
    double p = 0;
    for (int j : enemies) {
        const Ent& e = E[j];
        if (!isArmyType(e.type) && e.type != T_TURRET) continue;
        if (sqDist(e.x, e.y, e.size, cxo(c), cyo(c), 1) <= r) p += unitPow(e);
    }
    return p;
}

int nearestFree(int tx, int ty);
int rallyCell() {
    if (rallyC >= 0 && tick - rallyTick < 20 && !blk[rallyC]) return rallyC;
    rallyTick = tick;
    int home = -1;
    for (int i : myOf[T_BBASE]) home = cid(E[i].x + 2, E[i].y + 2);
    if (home < 0)
        for (int t : {T_RBASE, T_HOUSE, T_BUILDER})
            if (!myOf[t].empty()) {
                home = E[myOf[t][0]].c();
                break;
            }
    int target = -1;
    if (fmt == F_FINALS)
        target = cid(N - 8, N - 8);
    else if (lastArmySeenC >= 0 && tick - lastArmySeenTick < 150)
        target = lastArmySeenC;
    if (home >= 0 && target >= 0) {
        vector<int> D;
        vector<int> src(1, target);
        bfsField(D, src, [&](int c) { return !blk[c]; });
        int c = home;
        // from home, 16 steps down the distance field towards the enemy
        for (int step = 0; step < 40 && D[c] < INF; ++step) {
            int best = -1;
            for (int d = 0; d < 4; ++d) {
                int nx = cxo(c) + DX[d], ny = cyo(c) + DY[d];
                if (!inMap(nx, ny)) continue;
                int n = cid(nx, ny);
                if (D[n] < D[c] && (best < 0 || D[n] < D[best])) best = n;
            }
            if (best < 0) break;
            c = best;
            if (mdist(c, home) >= 17) break;
        }
        if (D[c] < INF && !blk[c]) {
            rallyC = c;
            return c;
        }
    }
    rallyC = nearestFree(19, 19);
    return rallyC;
}

int nearestFree(int tx, int ty) {
    tx = min(tx, N - 1);
    ty = min(ty, N - 1);
    int best = cid(tx, ty), bd = INF;
    for (int y = max(0, ty - 6); y <= min(N - 1, ty + 6); ++y)
        for (int x = max(0, tx - 6); x <= min(N - 1, tx + 6); ++x) {
            int c = cid(x, y);
            if (blk[c] || site[c]) continue;
            int d = std::abs(x - tx) + std::abs(y - ty);
            if (d < bd) {
                bd = d;
                best = c;
            }
        }
    return best;
}

// the army's targets: weighted sources (cell, head start cost)
void armyFieldW(vector<int>& D, const vector<std::pair<int, int>>& src) {
    D.assign(NN, INF);
    typedef std::pair<int, int> PI;
    std::priority_queue<PI, vector<PI>, std::greater<PI>> pq;
    for (const auto& sc : src)
        if (sc.first >= 0 && sc.first < NN && sc.second < D[sc.first]) {
            D[sc.first] = sc.second;
            pq.push(PI(sc.second, sc.first));
        }
    while (!pq.empty()) {
        PI top = pq.top();
        pq.pop();
        int c = top.second;
        if (top.first != D[c]) continue;
        int x = cxo(c), y = cyo(c);
        for (int d = 0; d < 4; ++d) {
            int nx = x + DX[d], ny = y + DY[d];
            if (!inMap(nx, ny)) continue;
            int n = cid(nx, ny);
            if (blk[n]) continue;
            int w = 1;
            int o = cellE[n];
            if (o >= 0 && E[o].owner == me && isUnitType(E[o].type)) w = E[o].type == T_BUILDER ? 3 : 2;
            if (site[n]) w += 3;
            if (D[c] + w < D[n]) {
                D[n] = D[c] + w;
                pq.push(PI(D[n], n));
            }
        }
    }
}

void addEntSrc(vector<std::pair<int, int>>& src, const Ent& e, int cost) {
    for (int yy = e.y; yy < e.y + e.size; ++yy)
        for (int xx = e.x; xx < e.x + e.size; ++xx)
            if (inMap(xx, yy)) src.push_back({cid(xx, yy), cost});
}

int buildingCost(int type) {
    switch (type) {
        case T_BBASE: case T_RBASE: case T_MBASE: return 0;
        case T_TURRET: return 2;
        case T_HOUSE: return 6;
        default: return 14;
    }
}

void planArmy() {
    vector<int> army;
    double myArmy = 0;
    for (int t : {T_RANGED, T_MELEE})
        for (int i : myOf[t])
            if (E[i].active) {
                army.push_back(i);
                myArmy += unitPow(E[i]);
            }
    // enemy army estimate: visible now (Round 1) or seen lately (fog)
    std::map<int, double> enArmyBy;
    double enArmyAll = 0;
    if (!fog) {
        for (int j : enemies)
            if (isArmyType(E[j].type)) enArmyBy[E[j].owner] += unitPow(E[j]);
    } else {
        for (const auto& kv : seenArmy) enArmyBy[kv.second.first] += 1.0;
    }
    for (const auto& kv : enArmyBy) enArmyAll += kv.second;
    {
        int sx = 0, sy = 0, cnt = 0;
        for (int j : enemies)
            if (isArmyType(E[j].type) && E[j].visible && homeD[E[j].c()] <= 45) {
                sx += E[j].x;
                sy += E[j].y;
                cnt++;
            }
        if (cnt > 0) {
            lastArmySeenC = nearestFree(sx / cnt, sy / cnt);
            lastArmySeenTick = tick;
        }
    }
    // threats: enemy fighters and buildings in my zone (builders are shot when in range, never chased)
    vector<std::pair<int, int>> threatSrc;
    double threatP = 0;
    for (int j : enemies) {
        const Ent& e = E[j];
        if (e.type == T_BUILDER) continue;
        if (isUnitType(e.type) && !e.visible) continue;
        int c = e.c();
        if (!(homeD[c] <= 16 || nearB[c])) continue;
        addEntSrc(threatSrc, e, 0);
        threatP += isArmyType(e.type) || e.type == T_TURRET ? unitPow(e) : 0.3;
    }

    // the attack: whom and where
    double attackAt = fmt == F_ROUND1 ? knob("R1_ATK", 18) : (fmt == F_FINALS ? knob("F_ATK", 30) : knob("R2_ATK", 34));
    vector<std::pair<int, int>> atkSrc;
    int owner = -1;
    if (fmt == F_ROUND1) {
        std::map<int, double> distBy, powBy;
        std::map<int, int> bldBy;
        for (int j : enemies) {
            const Ent& e = E[j];
            if (isBuildingType(e.type)) {
                distBy[e.owner] += homeD[e.c()];
                bldBy[e.owner]++;
            }
            if (isArmyType(e.type) || (e.type == T_TURRET && e.active)) powBy[e.owner] += unitPow(e);
        }
        double best = 1e18;
        for (const auto& kv : bldBy) {
            int p = kv.first;
            double avgD = distBy[p] / max(1, kv.second);
            double sc = avgD + powBy[p] * 3.0 - (p == attackTarget ? 15 : 0);
            if (sc < best) {
                best = sc;
                owner = p;
            }
        }
        attackTarget = owner;
        if (owner >= 0) attackAt = max(attackAt, powBy[owner] * knob("R1_ATK_K", 1.3) + 5);
    } else {
        int bd = INF;
        for (int j : enemies) {
            const Ent& e = E[j];
            if (!isBuildingType(e.type) && !(isArmyType(e.type) && e.visible)) continue;
            int d = homeD[e.c()];
            if (d < bd) {
                bd = d;
                owner = e.owner;
            }
        }
        attackAt = max(attackAt, enArmyAll * 1.5 + 6);
    }
    if (tick > 850) attackAt = min(attackAt, 12.0);
    if (owner >= 0) {
        for (int j : enemies) {
            const Ent& e = E[j];
            if (e.owner != owner && fmt != F_FINALS) continue;
            if (isArmyType(e.type) && e.visible)
                addEntSrc(atkSrc, e, 0);
            else if (isBuildingType(e.type))
                addEntSrc(atkSrc, e, buildingCost(e.type));
        }
    }
    if (fog) {
        // the corners where enemies started, as a fallback (unseen corners first)
        vector<std::pair<int, int>> corners;
        if (fmt == F_FINALS)
            corners.push_back({N - 8, N - 8});
        else {
            corners.push_back({N - 8, 7});
            corners.push_back({7, N - 8});
            corners.push_back({N - 8, N - 8});
        }
        for (auto& cc : corners) {
            int c = nearestFree(cc.first, cc.second);
            bool explored = seenEver[c] && tick - lastSeen[c] < 60;
            if (!explored) atkSrc.push_back({c, atkSrc.empty() ? 0 : 30});
        }
    }

    // modes
    bool bigThreat = threatP > 0 && (threatP >= 0.4 * myArmy || threatP >= 6);
    if (armyMode == M_ATTACK && myArmy >= attackAt * 0.6 && !atkSrc.empty())
        armyMode = M_ATTACK;
    else if (myArmy >= attackAt && !atkSrc.empty())
        armyMode = M_ATTACK;
    else
        armyMode = M_GATHER;
    if (bigThreat) armyMode = M_DEFEND;

    if (!threatSrc.empty()) armyFieldW(fieldDef, threatSrc);
    if (armyMode == M_ATTACK) armyFieldW(fieldAtk, atkSrc);
    {
        vector<std::pair<int, int>> src(1, {rallyCell(), 0});
        armyFieldW(fieldRally, src);
    }
    const vector<int>& mainF = armyMode == M_ATTACK ? fieldAtk : (armyMode == M_DEFEND ? fieldDef : fieldRally);

    // a small raid gets a detachment (the nearest units), the rest keep the main task
    std::unordered_map<int, int> useDef;
    if (!threatSrc.empty() && !bigThreat) {
        vector<int> order = army;
        std::sort(order.begin(), order.end(), [&](int a, int b) { return fieldDef[E[a].c()] < fieldDef[E[b].c()]; });
        double got = 0;
        int n = 0;
        for (int i : order) {
            if (fieldDef[E[i].c()] >= INF) break;
            if (got >= threatP * 1.6 + 1 && n >= 2) break;
            useDef[i] = 1;
            got += unitPow(E[i]);
            n++;
        }
    }
    // cohesion on the march: the leaders wait for the group
    bool leadersWait = false;
    int front = INF;
    if (armyMode == M_ATTACK) {
        int cntAll = 0, cntNear = 0;
        for (int i : army)
            if (!useDef.count(i) && fieldAtk[E[i].c()] < INF) front = min(front, fieldAtk[E[i].c()]);
        for (int i : army) {
            if (useDef.count(i) || fieldAtk[E[i].c()] >= INF) continue;
            cntAll++;
            if (fieldAtk[E[i].c()] <= front + 10) cntNear++;
        }
        leadersWait = cntAll > 0 && cntNear < min(15.0, 0.5 * cntAll);
    }

    pushTaken.assign(NN, 0);
    // the front: safe cells at the edge of the enemy's reach next tick; holding units spread along it
    vector<int> frontDist;
    bool useFront = knob("FRONT", 1) > 0;
    if (useFront) {
        vector<int> fsrc;
        for (int c = 0; c < NN; ++c) {
            if (blk[c] || dNext[c] > 0) continue;
            int o = cellE[c];
            if (o >= 0 && E[o].owner == me && isUnitType(E[o].type)) continue;
            int x = cxo(c), y = cyo(c);
            for (int d = 0; d < 4; ++d) {
                int nx = x + DX[d], ny = y + DY[d];
                if (inMap(nx, ny) && dNext[cid(nx, ny)] > 0 && !blk[cid(nx, ny)]) {
                    fsrc.push_back(c);
                    break;
                }
            }
        }
        if (!fsrc.empty())
            bfsField(frontDist, fsrc, [&](int c) { return !blk[c] && dNext[c] == 0; });
        else
            useFront = false;
    }
    for (int i : army) {
        if (decided[i]) continue;
        const Ent& u = E[i];
        int c = u.c();
        bool def = useDef.count(i) > 0 || armyMode == M_DEFEND;
        const vector<int>& Fv = useDef.count(i) ? fieldDef : mainF;
        int mode = useDef.count(i) ? M_DEFEND : armyMode;
        double mp = myPowAt(c, 6), ep = enPowAt(c, 9);
        double k = def ? 0.8 : 1.25;
        bool aggressive = ep < 0.05 || mp >= k * ep;
        bool hold = mode == M_ATTACK && leadersWait && Fv[c] <= front + 3 && ep < 0.05 && enPowAt(c, 18) > 0.05;
        std::pair<int, int> cand[4];
        int m = 0;
        for (int d = 0; d < 4; ++d) {
            int nx = u.x + DX[d], ny = u.y + DY[d];
            if (!inMap(nx, ny)) continue;
            int nc = cid(nx, ny);
            if (blk[nc] || stayC[nc] || enemyUnitAt(nc)) continue;
            if (site[nc] && !site[c]) continue;
            if (hold) continue;
            if (aggressive) {
                if (Fv[nc] < Fv[c]) cand[m++] = {Fv[nc] * 64 + min(63, dNext[nc]), nc};
            } else if (dNext[c] == 0) {
                if (Fv[nc] < Fv[c] && dNext[nc] == 0) cand[m++] = {Fv[nc], nc};
                else if (useFront && ep > 0.05 && frontDist[c] > 0 && frontDist[c] < 30 && frontDist[nc] < frontDist[c] && dNext[nc] == 0)
                    cand[m++] = {100000 + frontDist[nc], nc};  // no way forward: spread along the front
            } else {
                if (dNext[nc] < dNext[c]) cand[m++] = {dNext[nc] * 4096 + min(4095, homeD[nc]), nc};
            }
        }
        if (m == 0 && site[c]) {
            for (int d = 0; d < 4; ++d) {
                int nx = u.x + DX[d], ny = u.y + DY[d];
                if (!inMap(nx, ny)) continue;
                int nc = cid(nx, ny);
                if (!blk[nc] && !stayC[nc] && !site[nc] && !enemyUnitAt(nc)) cand[m++] = {0, nc};
            }
        }
        if (m == 0 && !hold && (aggressive || dNext[c] == 0) && Fv[c] > 0 && Fv[c] < INF) {
            // a builder of mine stands in the way: it steps aside
            int bestN = -1, bestF = Fv[c];
            for (int d = 0; d < 4; ++d) {
                int nx = u.x + DX[d], ny = u.y + DY[d];
                if (!inMap(nx, ny)) continue;
                int nc = cid(nx, ny);
                int o = cellE[nc];
                if (o < 0 || E[o].owner != me || E[o].type != T_BUILDER || !stayC[nc]) continue;
                if (!aggressive && dNext[nc] > 0) continue;
                auto f = acts.find(E[o].id);
                if (f != acts.end() && f->second.buildAction) continue;
                if (Fv[nc] < bestF) {
                    bestF = Fv[nc];
                    bestN = nc;
                }
            }
            if (bestN >= 0) {
                int o = cellE[bestN];
                int bx = cxo(bestN), by = cyo(bestN);
                int esc[4], ne = 0;
                for (int d = 0; d < 4; ++d) {
                    int ex = bx + DX[d], ey = by + DY[d];
                    if (!inMap(ex, ey)) continue;
                    int ec = cid(ex, ey);
                    if (ec == c || blk[ec] || site[ec] || stayC[ec] || cellE[ec] >= 0 || pushTaken[ec] || enemyUnitAt(ec)) continue;
                    esc[ne++] = ec;
                }
                if (ne > 0) {
                    dbgPush++;
                    pushTaken[esc[0]] = 1;
                    stayC[bestN] = 0;
                    int o1[1] = {esc[0]};
                    addReq(o, 90, o1, 1);
                    cand[m++] = {0, bestN};
                }
            }
        }
        std::sort(cand, cand + m);
        int opts[4];
        for (int k2 = 0; k2 < m; ++k2) opts[k2] = cand[k2].second;
        if (m > 0)
            addReq(i, u.type == T_RANGED ? 30 : 32, opts, m);
        else
            setAct(i, actNone(), true);
    }
}

// ------------------------------------------------------------------ production
int spawnCell(const Ent& base, const vector<int>* F) {
    int best = -1, bd = INF;
    for (int yy = base.y - 1; yy <= base.y + base.size; ++yy)
        for (int xx = base.x - 1; xx <= base.x + base.size; ++xx) {
            if (!inMap(xx, yy)) continue;
            if (sqDist(base.x, base.y, base.size, xx, yy, 1) != 1) continue;
            int c = cid(xx, yy);
            if (cellE[c] >= 0 || blk[c] || site[c] || claimC[c] != -1) continue;
            int d = F ? (*F)[c] : 0;
            if (d >= INF) d = INF - 1;
            if (d < bd) {
                bd = d;
                best = c;
            }
        }
    return best;
}

void planProduction() {
    int avail = budget - max(0, reserveRes);
    int popFree = popProv - popUsed;
    const vector<int>* armyF = armyMode == M_DEFEND ? &fieldDef : (armyMode == M_ATTACK ? &fieldAtk : &fieldRally);
    if (armyF->empty()) armyF = nullptr;
    auto trainBuilders = [&]() {
        for (int i : myOf[T_BBASE]) {
            const Ent& b = E[i];
            if (!b.active || decided[i]) continue;
            if (nB >= targetBuilders) break;
            if (idleBuilders >= (int)knob("IDLE_STOP", 3)) break;  // more builders would only stand around
            if (popFree <= 0) {
                dbgStallPop++;
                break;
            }
            int cost = unitCost(T_BUILDER);
            if (avail < cost) {
                if (budget >= cost) dbgStallReserve++; else dbgStallRes++;
                break;
            }
            int c = spawnCell(b, slotD.empty() ? nullptr : &slotD);
            if (c < 0) {
                dbgStallCell++;
                continue;
            }
            dbgTrained++;
            setAct(i, actBuild(T_BUILDER, cxo(c), cyo(c)), false);
            claimC[c] = -2;
            avail -= cost;
            popFree--;
            nB++;
        }
    };
    auto trainRanged = [&]() {
        for (int i : myOf[T_RBASE]) {
            const Ent& b = E[i];
            if (!b.active || decided[i]) continue;
            if (nR + nM >= armyWanted || popFree <= 0) break;
            int cost = unitCost(T_RANGED);
            if (avail < cost) break;
            int c = spawnCell(b, armyF);
            if (c < 0) continue;
            setAct(i, actBuild(T_RANGED, cxo(c), cyo(c)), false);
            claimC[c] = -2;
            avail -= cost;
            popFree--;
            nR++;
        }
    };
    if (wantRangedFirst) {
        trainRanged();
        trainBuilders();
    } else {
        trainBuilders();
        trainRanged();
    }
}

// ------------------------------------------------------------------ movement resolution
void resolveMoves() {
    std::stable_sort(reqs.begin(), reqs.end(), [](const MoveReq& a, const MoveReq& b) { return a.prio > b.prio; });
    vector<int> dest(E.size(), -1);
    vector<uint8_t> moving(E.size(), 0);
    for (const MoveReq& r : reqs) moving[r.ei] = 1;
    auto tryOpt = [&](const MoveReq& r) -> bool {
        int from = E[r.ei].c();
        for (int k = 0; k < r.n; ++k) {
            int c = r.opt[k];
            if (c < 0 || c >= NN || blk[c] || claimC[c] != -1 || stayC[c]) continue;
            int o = cellE[c];
            if (o >= 0) {
                if (E[o].owner != me || !isUnitType(E[o].type)) continue;
                if (!moving[o]) continue;
                if (dest[o] == from) continue;
            }
            dest[r.ei] = c;
            claimC[c] = r.ei;
            return true;
        }
        return false;
    };
    for (const MoveReq& r : reqs) tryOpt(r);
    for (int iter = 0; iter < 8; ++iter) {
        bool changed = false;
        for (const MoveReq& r : reqs) {
            int d = dest[r.ei];
            if (d < 0) {
                if (moving[r.ei]) {
                    moving[r.ei] = 0;
                    changed = true;
                }
                continue;
            }
            int o = cellE[d];
            if (o >= 0 && o != r.ei && (!moving[o] || dest[o] < 0)) {
                claimC[d] = -1;
                dest[r.ei] = -1;
                moving[r.ei] = 0;
                changed = true;
            }
        }
        if (!changed) break;
    }
    if (dbgLevel >= 3 && tick == 120)
        for (const MoveReq& r : reqs)
            std::fprintf(stderr, "req %d at %d,%d prio %d opts %d first %d,%d -> dest %d\n", E[r.ei].id, E[r.ei].x, E[r.ei].y, r.prio, r.n,
                         cxo(r.opt[0]), cyo(r.opt[0]), dest[r.ei]);
    for (const MoveReq& r : reqs) {
        int d = dest[r.ei];
        if (d >= 0 && E[r.ei].type == T_BUILDER) dbgMoveBy[r.prio]++;
        if (d >= 0)
            acts[E[r.ei].id] = actMove(d);
        else {
            // stays: mine if possible
            const Ent& u = E[r.ei];
            if (u.type == T_BUILDER) {
                int res = bestAdjRes(u.c());
                acts[u.id] = res >= 0 ? actAttack(E[res].id) : actNone();
            } else
                acts[u.id] = actNone();
        }
    }
}

int statEvery = 50;
void dumpMap(int w) {
    for (int y = 0; y < min(N, w); ++y) {
        std::string s;
        for (int x = 0; x < min(N, w); ++x) {
            int c = cid(x, y);
            int o = cellE[c];
            char ch = '.';
            if (!vis[c]) ch = seenEver[c] ? (memRes[c] > 0 ? 'r' : ',') : ' ';
            if (o >= 0) {
                const Ent& e = E[o];
                bool mine = e.owner == me;
                char bch = 'b';
                if (mine && e.type == T_BUILDER) {
                    auto f = acts.find(e.id);
                    if (f != acts.end()) {
                        const EntityAction& a = f->second;
                        bch = a.attackAction ? 'b' : a.moveAction ? 'v' : a.repairAction ? 'p' : a.buildAction ? 'x' : 'i';
                    }
                }
                switch (e.type) {
                    case T_RES: ch = '#'; break;
                    case T_BUILDER: ch = mine ? bch : 'B'; break;
                    case T_RANGED: ch = mine ? 'r' : 'R'; break;
                    case T_MELEE: ch = mine ? 'm' : 'M'; break;
                    case T_HOUSE: ch = mine ? (e.active ? 'h' : 'g') : 'H'; break;
                    case T_TURRET: ch = mine ? 't' : 'T'; break;
                    case T_WALL: ch = 'w'; break;
                    default: ch = mine ? 'o' : 'O'; break;
                }
            } else if (site[c])
                ch = '+';
            s += ch;
        }
        std::fprintf(stderr, "%s\n", s.c_str());
    }
}
void stats() {
    if (dbgLevel < 0) {
        const char* v = std::getenv("CC_DEBUG");
        dbgLevel = v ? std::atoi(v) : 0;
    }
    if (dbgLevel >= 1 && tick % 10 == 0 && tick <= 400) {
        int acM = 0, acMove = 0, acB = 0, acR = 0, acN = 0;
        for (int i : myOf[T_BUILDER]) {
            auto f = acts.find(E[i].id);
            if (f == acts.end()) continue;
            const EntityAction& a = f->second;
            if (a.attackAction) acM++;
            else if (a.moveAction) acMove++;
            else if (a.buildAction) acB++;
            else if (a.repairAction) acR++;
            else acN++;
        }
        std::fprintf(stderr, "t%d B%d mine%d move%d build%d rep%d idle%d res%d pop%d/%d tasks%zu reserve%d walk %d/%d/%d/%d/%d/x%d\n", tick,
                     (int)myOf[T_BUILDER].size(), acM, acMove, acB, acR, acN, myRes, popUsed, popProv, tasks.size(), reserveRes,
                     dbgWalk[0], dbgWalk[1], dbgWalk[2], dbgWalk[3], dbgWalk[4], dbgWalk[5]);
        std::string mb;
        for (const auto& kv : dbgMoveBy) mb += " p" + std::to_string(kv.first) + ":" + std::to_string(kv.second);
        std::fprintf(stderr, "t%d pushes %d, builder moves by prio (10 ticks):%s newTargets %d avgLen %.1f kept %d\n", tick, dbgPush,
                     mb.c_str(), dbgNewT, dbgNewT ? (double)dbgNewTLen / dbgNewT : 0.0, dbgKeptT);
        dbgNewT = dbgNewTLen = dbgKeptT = 0;
        std::fprintf(stderr, "t%d builder base (10 ticks): trained %d, no money %d, money reserved %d, pop full %d, no cell %d\n", tick,
                     dbgTrained, dbgStallRes, dbgStallReserve, dbgStallPop, dbgStallCell);
        dbgTrained = dbgStallRes = dbgStallReserve = dbgStallPop = dbgStallCell = 0;
        {
            int planned = 0, placed = 0, helpers = 0, waiting = 0, noPlacer = 0;
            std::string where;
            for (const Task& t : tasks)
                if (t.type == T_HOUSE) {
                    if (t.eid < 0) {
                        planned++;
                        if (t.placer < 0) noPlacer++;
                    } else
                        placed++;
                    helpers += (int)t.helpers.size();
                    where += " (" + std::to_string(t.x) + "," + std::to_string(t.y) + (t.eid < 0 ? "p" : "b") + std::to_string(t.helpers.size()) + ")";
                }
            std::fprintf(stderr, "t%d houses planned %d (no placer %d) placed %d helpers %d res %d popFree %d:%s\n", tick, planned, noPlacer,
                         placed, helpers, myRes, popProv - popUsed, where.c_str());
        }
        dbgPush = 0;
        dbgMoveBy.clear();
        if (dbgLevel >= 2 && (tick == 30 || tick == 60 || tick == 100 || tick == 120 || tick == 140 || tick == 150 || tick == 200 || tick == 300 || tick == 400)) dumpMap(40);
    }
    if (tick % statEvery != 0 && tick != 999) return;
    int houses = (int)myOf[T_HOUSE].size();
    std::fprintf(stderr, "T%d fmt%d B%d R%d M%d H%d TU%d res%d score%d pop%d/%d mode%d tasks%zu mining%d cpu%.2f max%.0fms\n", tick, fmt,
                 (int)myOf[T_BUILDER].size(), (int)myOf[T_RANGED].size(), (int)myOf[T_MELEE].size(), houses,
                 (int)myOf[T_TURRET].size(), myRes, myScore, popUsed, popProv, armyMode, tasks.size(), miningNow,
                 cpuNow() - cpuStart, maxTickCpu * 1000);
}

Action act(const PlayerView& pv) {
    double t0 = cpuNow();
    struct TickTimer {
        double t0;
        ~TickTimer() { maxTickCpu = max(maxTickCpu, cpuNow() - t0); }
    } timer{t0};
    if (dbgLevel < 0) {
        const char* v = std::getenv("CC_DEBUG");
        dbgLevel = v ? std::atoi(v) : 0;
    }
    readProps(pv);
    buildWorld(pv);
    computeDanger();
    computeHome();
    acts.clear();
    reqs.clear();
    decided.assign(E.size(), 0);
    stayC.assign(NN, 0);
    claimC.assign(NN, -1);
    // buildings and units that are not mine never get actions; mark them
    for (size_t i = 0; i < E.size(); ++i)
        if (E[i].owner != me) decided[i] = 1;
    planMacro();
    planCombat();
    planBuilders();
    computeReserve();
    planArmy();
    planProduction();
    resolveMoves();
    for (int t = 0; t < T_N; ++t)
        for (int i : myOf[t])
            if (!acts.count(E[i].id)) acts[E[i].id] = actNone();
    stats();
    Action a;
    a.entityActions = std::move(acts);
    acts = std::unordered_map<int, EntityAction>();
    return a;
}

}  // namespace

MyStrategy::MyStrategy() {}

Action MyStrategy::getAction(const PlayerView& playerView, DebugInterface*) {
    try {
        return act(playerView);
    } catch (const std::exception& e) {
        std::fprintf(stderr, "tick %d: exception %s\n", playerView.currentTick, e.what());
    } catch (...) {
        std::fprintf(stderr, "tick %d: unknown exception\n", playerView.currentTick);
    }
    return Action(std::unordered_map<int, EntityAction>());
}

void MyStrategy::debugUpdate(const PlayerView&, DebugInterface&) {}
