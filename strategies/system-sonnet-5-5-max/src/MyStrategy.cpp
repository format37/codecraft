// system-sonnet-5-5-max: a CodeCraft 2020 strategy by Claude Sonnet 5.5, written from scratch for this server.
// One file, a drop-in for the official C++ client's MyStrategy.cpp.
//
// The plan: economy first (builders spread over distinct resource cells, houses just in time, nobody parks in a
// passage), a ranged army (plus melee in Round 1, where the base is free) bought along a target curve, focus fire
// from every shooter, turrets at the gathering place, and attack waves that march as a ball around an anchor,
// regroup before contact and fall back only before contact. Where things are built matters as much as when: the
// soldiers' streets, their gathering place and the routes to the enemy corners stay free, and no site is accepted
// that would cut off free ground or mining slots.
//
// The code: `struct Brain` below. One tick = readWorld (the view plus a memory of seen resources and enemy
// buildings under fog) -> planEconomy (population, houses, bases, turrets, production) -> assignFire (focus fire)
// -> assignArmy (modes, anchor, moves) -> assignBuilders (flight, projects, mining). Every entity of mine gets an
// explicit order every tick, because an entity that is not mentioned repeats its old order. Moves are planned and
// then resolved in rounds, as the engine does it, so that a column can step into the cells its head leaves.
// Settings can be overridden by CC_* environment variables for local experiments; the server sets none of them.
#include "MyStrategy.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <climits>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <exception>
#include <map>
#include <memory>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

namespace {

// ---------------------------------------------------------------------------------------------------------------
// tunables (environment overrides are for local experiments only; the server sets none of them)
int envInt(const char* name, int def) {
    const char* s = std::getenv(name);
    return s && *s ? std::atoi(s) : def;
}

constexpr int NTYPES = 10;
const int DX[4] = {1, -1, 0, 0};
const int DY[4] = {0, 0, 1, -1};

struct Pr {
    int size = 1, buildScore = 0, destroyScore = 0, popProvide = 0, popUse = 0, maxHp = 1, cost = 0, sight = 0;
    bool canMove = false, canBuild = false, canAttack = false, collect = false, canRepair = false;
    int range = 0, damage = 0, repairPower = 0;
};

struct Ent {
    int id = -1;
    int owner = -1;  // player id; -1 for a resource
    EntityType type = WALL;
    int x = 0, y = 0;
    int hp = 0;
    int size = 1;
    bool active = false;
    bool stale = false;  // remembered under fog, not in this tick's view
    bool mine = false;
};

inline int distRect(int ax, int ay, int as, int bx, int by, int bs) {
    int dx = std::max({0, bx - (ax + as - 1), ax - (bx + bs - 1)});
    int dy = std::max({0, by - (ay + as - 1), ay - (by + bs - 1)});
    return dx + dy;
}

struct Proj {
    EntityType kind = HOUSE;
    int x = 0, y = 0, size = 3, cost = 50;
    int builder = -1;  // id of the builder that places it
    int bid = -1;      // id of the building once it stands
    int born = 0;
    int failTicks = 0;
    bool near = false;  // the builder is close: the money is held back
    long repDistSum = 0, repDistN = 0;
    int placedTick = -1;
    int wantRep = 0;  // 0: the default number of repairers
    int maxWalk = 1000;  // only builders closer than this are called
};

// ---------------------------------------------------------------------------------------------------------------
struct Brain {
    // ---- configuration
    int cfgTargetBuilders = envInt("CC_BUILDERS", 80);
    int cfgHouseBase = envInt("CC_HB", 1);
    int cfgHouseSlope = envInt("CC_HS", 8);  // units per +1 of the free-population threshold
    int cfgRepairers = envInt("CC_REP", 3);
    int cfgRepCapped = envInt("CC_REPCAP", 6);
    int cfgBaseRepairers = envInt("CC_BASEREP", 18);
    int cfgWalkW = envInt("CC_WALKW", 10);
    int cfgBaseW = envInt("CC_BASEW", 10);
    int cfgCornerW = envInt("CC_CORNERW", 0);  // x100: pull toward the map corner behind the base
    int cfgRbAt = envInt("CC_RBAT", 40);        // builders before the ranged base is started
    int cfgAttackAt = envInt("CC_ATTACKAT", 45);  // ranged units before the army marches
    int cfgRichGold = envInt("CC_RICHGOLD", 100);  // spare money that starts another house
    int cfgRichPop = envInt("CC_RICHPOP", 15);     // ... while the free population is below this
    int cfgMaxHouses = envInt("CC_MAXHOUSES", 4);
    int cfgLazy = envInt("CC_LAZY", 1);   // hold the money for a project only when its builder is close
    int cfgHoldSlack = envInt("CC_HOLD", 14);
    int cfgMaxTurrets = envInt("CC_TURRETS", 14);
    int cfgTurretGold = envInt("CC_TURRETGOLD", 200);   // spare money that starts a turret
    int cfgTurretRing = envInt("CC_TURRETRING", 7);     // turrets stand about this far from the gathering place
    int cfgFlee = envInt("CC_FLEE", 1);
    int cfgWeakBias = envInt("CC_WEAKBIAS", 60);   // percent: how much farther the weakest enemy may be than the strongest
    int cfgMeleeRatio = envInt("CC_MELEE", 70);   // melee soldiers as a percentage of the ranged ones, Round 1 (their base is free); 0 = none
    int cfgMeleeRatio2 = envInt("CC_MELEE2", 0);  // ... in the other formats, where the base has to be built
    int cfgMbAt = envInt("CC_MBAT", 45);
    int cfgSecondRbGold = envInt("CC_RB2GOLD", 1200);
    double cfgCpuBase = 5.0, cfgCpuPerTick = 0.02, cfgCpuPanic = 36.0;   // seconds: the server allows 40 for a game
    int cfgArmyStart = envInt("CC_ARMYSTART", 220);
    int cfgArmyStart1 = envInt("CC_ARMYSTART1", 50);    // Round 1: the bases are there from the start
    int cfgEarlyRate1 = envInt("CC_EARLYRATE1", 12);
    int cfgArmyRate1 = envInt("CC_ARMYRATE1", 30);
    int cfgArmyRate = envInt("CC_ARMYRATE", 45);   // percent of a soldier per tick after the start
    int cfgSpread = envInt("CC_SPREAD", 1);   // builders prefer resource cells that nobody else works on
    int cfgDefendAbove = envInt("CC_DEFENDABOVE", 90);   // percent: go out against home threats when ours >= this share of theirs
    int cfgEarlyHouses = envInt("CC_EARLYHOUSES", 2);  // houses placed for the miners' convenience before the border rows
    int cfgEdgeW = envInt("CC_EDGEW", 30);
    int cfgRecallPower = envInt("CC_RECALL", 12);
    int cfgAttackTick = envInt("CC_ATTACKTICK", 300);
    int cfgBallR = envInt("CC_BALLR", 9);
    int cfgHoldMax = envInt("CC_HOLDMAX", 40);
    int cfgLead = envInt("CC_LEAD", 2);
    int cfgAnchorWait = envInt("CC_ANCHORWAIT", 8);
    int cfgStageDist = envInt("CC_STAGE", 24);
    int cfgRetreatBelow = envInt("CC_RETREAT", 50);   // percent: retreat when ours/theirs is below this
    int cfgEngageAbove = envInt("CC_ENGAGE", 115);   // percent: advance on the enemy when above this
    int cfgLog = envInt("CC_LOG", 0);
    int cfgPlug = envInt("CC_PLUG", 1);       // keep builders from plugging corridors

    // ---- static info
    Pr pr[NTYPES];
    bool havePr = false;
    int N = 80;
    int tick = 0;
    int me = -1;
    bool fog = false;
    int numPlayers = 4;
    int resources = 0;
    int score = 0;
    std::map<int, int> scoreOf;   // player id -> score, as shown to everyone
    int cornerOwner[3] = {-1, -1, -1};   // who holds each enemy corner, once a building there was seen

    // ---- per tick
    std::vector<Ent> ents;
    std::unordered_map<int, int> idx;  // entity id -> index in ents
    std::vector<int> occ;              // cell -> index in ents (buildings, resources, units) or -1
    std::vector<char> solid;           // building or resource on the cell
    std::vector<char> sight;           // cell seen by one of my entities
    std::vector<char> seenEver;        // cell seen at any time (what is built on must have been looked at)
    std::vector<int> resHp;            // hp of the resource on the cell (0 = none)
    std::vector<int> mineBuilders, mineRanged, mineMelee, mineBuildings, foeIdx, foeUnits;
    std::vector<int> bases;  // my active bases of any kind (indices)
    int popProvided = 0, popUsed = 0;
    int houseCount = 0;
    bool startedWithRB = false;
    bool homeThreat = false;   // armed enemies are close to my buildings or builders (from the last tick)
    int rbX = -1, rbY = -1;  // reserved site of the ranged base
    double homeCx = 7, homeCy = 7;

    // ---- memory
    std::map<int, Ent> memRes, memFoe;  // resources / enemy buildings seen before (fog)
    std::vector<Proj> projs;
    std::vector<char> claimed;  // mining slots taken this tick
    std::vector<char> resv;     // cells chosen as a destination this tick
    std::vector<char> forbid;   // cells that must stay empty of units (building sites)
    std::vector<char> blockCell;  // cells of my units that stay put this tick
    std::vector<char> planned;    // cells a unit has planned to step into this tick
    std::vector<char> danger;     // cells an enemy shooter can reach next tick
    std::vector<int> minerCnt;    // per resource cell: how many of my builders work on it this tick
    bool attackPhase = false;
    int objX = -1, objY = -1, objSize = 1;  // army objective
    bool objGuess = false;                  // the objective is a corner where an enemy is expected, not a seen building
    std::set<int> clearedCorner;
    bool armyRetreat = false;
    std::set<int> waveIds;     // units launched with the current attack wave
    bool waveActive = false;
    int waveSize0 = 0;
    std::unordered_map<int, int> spawnTick, firstMineTick;
    long delaySum = 0, delayN = 0;
    long whyNoGoal = 0, whyCommit = 0, whyAtGoal = 0, whyPlug = 0, whyPark = 0, whyBfsNone = 0;
    long actMine = 0, actWalk = 0, actRepair = 0, actBuild = 0, actIdle = 0;  // builder-ticks since the last log line

    // ---- orders of this tick
    std::unordered_map<int, EntityAction> orders;
    std::unordered_map<int, char> busy;  // entity id -> already has an order

    // ---- helpers
    int C(int x, int y) const { return y * N + x; }
    bool inb(int x, int y) const { return x >= 0 && y >= 0 && x < N && y < N; }

    // ---- order constructors
    static EntityAction mkMove(int x, int y) {
        return EntityAction(std::make_shared<MoveAction>(Vec2Int(x, y), false, false), nullptr, nullptr, nullptr);
    }
    static EntityAction mkAttack(int target) {
        return EntityAction(nullptr, nullptr,
                            std::make_shared<AttackAction>(std::make_shared<int>(target), nullptr), nullptr);
    }
    static EntityAction mkBuild(EntityType t, int x, int y) {
        return EntityAction(nullptr, std::make_shared<BuildAction>(t, Vec2Int(x, y)), nullptr, nullptr);
    }
    static EntityAction mkRepair(int target) {
        return EntityAction(nullptr, nullptr, nullptr, std::make_shared<RepairAction>(target));
    }

    void order(int id, const EntityAction& a) {
        orders[id] = a;
        busy[id] = 1;
    }

    // ---------------------------------------------------------------------------------------------------------
    void readProps(const PlayerView& v) {
        for (const auto& kv : v.entityProperties) {
            const EntityProperties& p = kv.second;
            Pr& q = pr[kv.first];
            q.size = p.size;
            q.buildScore = p.buildScore;
            q.destroyScore = p.destroyScore;
            q.popProvide = p.populationProvide;
            q.popUse = p.populationUse;
            q.maxHp = p.maxHealth;
            q.cost = p.initialCost;
            q.sight = p.sightRange;
            q.canMove = p.canMove;
            q.canBuild = p.build != nullptr;
            if (p.attack) {
                q.canAttack = true;
                q.range = p.attack->attackRange;
                q.damage = p.attack->damage;
                q.collect = p.attack->collectResource;
            }
            if (p.repair) {
                q.canRepair = true;
                q.repairPower = p.repair->power;
            }
        }
        havePr = true;
    }

    void readWorld(const PlayerView& v) {
        if (!havePr) readProps(v);
        N = v.mapSize;
        tick = v.currentTick;
        me = v.myId;
        fog = v.fogOfWar;
        numPlayers = static_cast<int>(v.players.size());
        for (const Player& p : v.players) {
            scoreOf[p.id] = p.score;
            if (p.id == me) {
                resources = p.resource;
                score = p.score;
            }
        }
        const size_t cells = static_cast<size_t>(N) * N;
        ents.clear();
        idx.clear();
        occ.assign(cells, -1);
        solid.assign(cells, 0);
        sight.assign(cells, 0);
        resHp.assign(cells, 0);
        claimed.assign(cells, 0);
        resv.assign(cells, 0);
        forbid.assign(cells, 0);
        blockCell.assign(cells, 0);
        planned.assign(cells, 0);
        mineBuilders.clear();
        mineRanged.clear();
        mineMelee.clear();
        mineBuildings.clear();
        foeIdx.clear();
        foeUnits.clear();
        bases.clear();
        houseCount = 0;
        orders.clear();
        busy.clear();
        popProvided = popUsed = 0;

        for (const Entity& e : v.entities) {
            Ent t;
            t.id = e.id;
            t.owner = e.playerId ? *e.playerId : -1;
            t.type = e.entityType;
            t.x = e.position.x;
            t.y = e.position.y;
            t.hp = e.health;
            t.size = pr[e.entityType].size;
            t.active = e.active;
            t.mine = (t.owner == me);
            idx[t.id] = static_cast<int>(ents.size());
            ents.push_back(t);
        }
        if (fog) {
            for (const Ent& e : ents)
                if (e.mine) stampSight(e);
        } else {
            std::fill(sight.begin(), sight.end(), 1);
        }
        if (seenEver.size() != cells) seenEver.assign(cells, 0);
        for (size_t i = 0; i < cells; i++)
            if (sight[i]) seenEver[i] = 1;
        for (const Ent& e : ents) {
            if (e.type == RESOURCE) memRes[e.id] = e;
            else if (e.owner >= 0 && !e.mine && !pr[e.type].canMove) memFoe[e.id] = e;
        }
        if (fog) {
            auto expire = [&](std::map<int, Ent>& m) {
                for (auto it = m.begin(); it != m.end();) {
                    const Ent& r = it->second;
                    if (!idx.count(r.id) && anySeen(r)) it = m.erase(it);
                    else ++it;
                }
            };
            expire(memRes);
            expire(memFoe);
            auto addStale = [&](std::map<int, Ent>& m) {
                for (auto& kv : m)
                    if (!idx.count(kv.first)) {
                        Ent s = kv.second;
                        s.stale = true;
                        idx[s.id] = static_cast<int>(ents.size());
                        ents.push_back(s);
                    }
            };
            addStale(memRes);
            addStale(memFoe);
        } else {
            memRes.clear();
            memFoe.clear();
        }
        double sx = 0, sy = 0;
        int nb = 0;
        for (int i = 0; i < static_cast<int>(ents.size()); i++) {
            const Ent& e = ents[i];
            for (int dy = 0; dy < e.size; dy++)
                for (int dx = 0; dx < e.size; dx++) {
                    int x = e.x + dx, y = e.y + dy;
                    if (!inb(x, y)) continue;
                    occ[C(x, y)] = i;
                    if (!pr[e.type].canMove) solid[C(x, y)] = 1;
                    if (e.type == RESOURCE) resHp[C(x, y)] = e.hp;
                }
            if (e.mine) {
                popUsed += pr[e.type].popUse;
                if (e.active) popProvided += pr[e.type].popProvide;
                if (e.type == BUILDER_UNIT) mineBuilders.push_back(i);
                else if (e.type == RANGED_UNIT) mineRanged.push_back(i);
                else if (e.type == MELEE_UNIT) mineMelee.push_back(i);
                else {
                    if (e.type == HOUSE) houseCount++;
                    mineBuildings.push_back(i);
                    sx += e.x + e.size / 2.0;
                    sy += e.y + e.size / 2.0;
                    nb++;
                }
                if (e.active && (e.type == BUILDER_BASE || e.type == MELEE_BASE || e.type == RANGED_BASE))
                    bases.push_back(i);
            } else if (e.owner >= 0) {
                foeIdx.push_back(i);
                if (pr[e.type].canMove && !e.stale) foeUnits.push_back(i);
            }
        }
        if (nb) {
            homeCx = sx / nb;
            homeCy = sy / nb;
        }
    }

    void stampSight(const Ent& e) {
        int r = pr[e.type].sight;
        int x0 = std::max(e.x - r, 0), x1 = std::min(e.x + e.size + r, N);
        for (int x = x0; x < x1; x++) {
            int gap = std::max({0, e.x - x, x - (e.x + e.size - 1)});
            int rest = r - gap;
            int y0 = std::max(e.y - rest, 0), y1 = std::min(e.y + e.size + rest, N);
            for (int y = y0; y < y1; y++) sight[C(x, y)] = 1;
        }
    }

    bool anySeen(const Ent& e) const {
        for (int dy = 0; dy < e.size; dy++)
            for (int dx = 0; dx < e.size; dx++) {
                int x = e.x + dx, y = e.y + dy;
                if (inb(x, y) && sight[C(x, y)]) return true;
            }
        return false;
    }

    // ---------------------------------------------------------------------------------------------------------
    // BFS machinery
    std::vector<int> bdist, bpar, bq;

    // nearest goal from a start cell. pass(c): may a unit walk through c. goal(c): is c a destination.
    // returns the goal cell or -1; *first is the first step cell on the way (or the start when already there)
    template <class Pass, class Goal>
    int bfsNearest(int start, Pass pass, Goal goal, int* first, int maxDist = 1 << 20) {
        const size_t cells = static_cast<size_t>(N) * N;
        if (bdist.size() != cells) {
            bdist.assign(cells, -1);
            bpar.assign(cells, -1);
            bq.resize(cells);
        }
        std::vector<int> touched;
        touched.reserve(256);
        int head = 0, tail = 0;
        bq[tail++] = start;
        bdist[start] = 0;
        bpar[start] = -1;
        touched.push_back(start);
        int found = -1;
        while (head < tail) {
            int c = bq[head++];
            if (goal(c)) {
                found = c;
                break;
            }
            if (bdist[c] >= maxDist) continue;
            int x = c % N, y = c / N;
            for (int k = 0; k < 4; k++) {
                int nx = x + DX[k], ny = y + DY[k];
                if (!inb(nx, ny)) continue;
                int nc = C(nx, ny);
                if (bdist[nc] != -1) continue;
                if (!pass(nc) && !goal(nc)) continue;
                bdist[nc] = bdist[c] + 1;
                bpar[nc] = c;
                bq[tail++] = nc;
                touched.push_back(nc);
            }
        }
        if (found >= 0 && first) {
            int c = found;
            while (bpar[c] != -1 && bpar[c] != start) c = bpar[c];
            *first = (found == start) ? start : c;
        }
        for (int t : touched) {
            bdist[t] = -1;
            bpar[t] = -1;
        }
        return found;
    }

    // full distance field from a set of source cells over cells satisfying pass
    template <class Pass>
    void bfsField(const std::vector<int>& src, Pass pass, std::vector<int>& dist, int maxDist = 1 << 20) {
        const size_t cells = static_cast<size_t>(N) * N;
        dist.assign(cells, -1);
        std::vector<int> q;
        q.reserve(cells);
        for (int s : src)
            if (dist[s] == -1) {
                dist[s] = 0;
                q.push_back(s);
            }
        for (size_t h = 0; h < q.size(); h++) {
            int c = q[h];
            if (dist[c] >= maxDist) continue;
            int x = c % N, y = c / N;
            for (int k = 0; k < 4; k++) {
                int nx = x + DX[k], ny = y + DY[k];
                if (!inb(nx, ny)) continue;
                int nc = C(nx, ny);
                if (dist[nc] != -1 || !pass(nc)) continue;
                dist[nc] = dist[c] + 1;
                q.push_back(nc);
            }
        }
    }

    bool walkable(int c) const { return occ[c] < 0; }

    // ---------------------------------------------------------------------------------------------------------
    // economy helpers
    int countType(EntityType t) const {
        int n = 0;
        for (const Ent& e : ents)
            if (e.mine && e.type == t) n++;
        return n;
    }

    int unitCost(EntityType t) const { return pr[t].cost + countType(t); }

    std::vector<int> perimeter(int x, int y, int s) const {
        std::vector<int> out;
        for (int i = 0; i < s; i++) {
            if (inb(x + i, y - 1)) out.push_back(C(x + i, y - 1));
            if (inb(x + i, y + s)) out.push_back(C(x + i, y + s));
            if (inb(x - 1, y + i)) out.push_back(C(x - 1, y + i));
            if (inb(x + s, y + i)) out.push_back(C(x + s, y + i));
        }
        return out;
    }

    bool adjacentTo(const Ent& u, int bx, int by, int bs) const {
        return distRect(u.x, u.y, u.size, bx, by, bs) == 1;
    }

    // a free cell next to a resource, not taken by another builder
    bool isSlot(int c) const {
        if (solid[c] || claimed[c]) return false;
        int x = c % N, y = c / N;
        for (int k = 0; k < 4; k++) {
            int nx = x + DX[k], ny = y + DY[k];
            if (inb(nx, ny) && resHp[C(nx, ny)] > 0) return true;
        }
        return false;
    }

    // ---------------------------------------------------------------------------------------------------------
    // building sites
    bool footprintFree(int x, int y, int s) const {
        if (x < 0 || y < 0 || x + s > N || y + s > N) return false;
        for (int yy = y; yy < y + s; yy++)
            for (int xx = x; xx < x + s; xx++) {
                int c = C(xx, yy);
                if (occ[c] >= 0 || forbid[c] || !seenEver[c]) return false;
            }
        return true;
    }

    bool inProject(int x, int y, int s) const {
        for (const Proj& p : projs)
            if (p.x < x + s + 1 && x < p.x + p.size + 1 && p.y < y + s + 1 && y < p.y + p.size + 1) return true;
        return false;
    }

    // base-like buildings of mine (active or not), as footprints
    std::vector<int> baseLikes() const {
        std::vector<int> out;
        for (int i : mineBuildings) {
            EntityType t = ents[i].type;
            if (t == BUILDER_BASE || t == MELEE_BASE || t == RANGED_BASE) out.push_back(i);
        }
        return out;
    }

    // cells that must stay free of new buildings: a street from every base ring to the rally point, the plaza, and
    // the routes from the plaza toward the enemy corners
    std::vector<char> boulevard;
    std::vector<char> routeMask;
    bool routesDone = false;
    void computeRoutes() {
        routeMask.assign(static_cast<size_t>(N) * N, 0);
        int rc = rallyCell();
        if (rc < 0) return;
        const int corners[3][2] = {{N - 8, 7}, {7, N - 8}, {N - 8, N - 8}};
        for (int k = 0; k < 3; k++) {
            int target = C(corners[k][0], corners[k][1]);
            if (solid[target]) continue;
            std::vector<int> field;
            bfsField({target}, [&](int c) { return !solid[c]; }, field);
            int c = rc;
            for (int guard = 0; c >= 0 && field[c] > 0 && guard < 400; guard++) {
                int x = c % N, y = c / N, nxt = -1;
                // routeMask: a 5-wide lane along the shortest path
                for (int dy = -2; dy <= 2; dy++)
                    for (int dx = -2; dx <= 2; dx++)
                        if (inb(x + dx, y + dy)) routeMask[C(x + dx, y + dy)] = 1;
                for (int kk = 0; kk < 4; kk++) {
                    int nx = x + DX[kk], ny = y + DY[kk];
                    if (!inb(nx, ny)) continue;
                    int nc = C(nx, ny);
                    if (field[nc] >= 0 && field[nc] < field[c]) {
                        nxt = nc;
                        break;
                    }
                }
                c = nxt;
            }
        }
        routesDone = true;
    }

    std::vector<char> streetOnly;
    void computeBoulevard() {
        boulevard.assign(static_cast<size_t>(N) * N, 0);
        streetOnly.assign(static_cast<size_t>(N) * N, 0);
        int rc = rallyCell();
        if (rc < 0) return;
        if (!routesDone && tick >= 2) computeRoutes();
        if (routesDone)
            for (size_t i = 0; i < boulevard.size(); i++)
                if (routeMask[i]) boulevard[i] = 1;
        std::vector<int> field;
        bfsField({rc}, [&](int c) { return !solid[c]; }, field);
        auto mark = [&](int c) {
            int x = c % N, y = c / N;
            for (int dy = -2; dy <= 2; dy++)
                for (int dx = -2; dx <= 2; dx++) {
                    if (!inb(x + dx, y + dy)) continue;
                    boulevard[C(x + dx, y + dy)] = 1;
                    if (std::abs(dx) <= 1 && std::abs(dy) <= 1) streetOnly[C(x + dx, y + dy)] = 1;
                }
        };
        {   // the plaza where the soldiers gather
            int rx = rc % N, ry = rc / N;
            for (int dy = -5; dy <= 5; dy++)
                for (int dx = -5; dx <= 5; dx++)
                    if (std::abs(dx) + std::abs(dy) <= 6 && inb(rx + dx, ry + dy)) boulevard[C(rx + dx, ry + dy)] = 1;
        }
        std::vector<std::array<int, 3>> starts;  // (x, y, size) of base footprints, also those under construction
        for (int i : baseLikes()) starts.push_back({ents[i].x, ents[i].y, ents[i].size});
        for (const Proj& p : projs)
            if (p.size >= 5) starts.push_back({p.x, p.y, p.size});
        for (auto& b : starts) {
            int best = -1, bestD = INT_MAX;
            for (int c : perimeter(b[0], b[1], b[2]))
                if (field[c] >= 0 && field[c] < bestD) {
                    bestD = field[c];
                    best = c;
                }
            int c = best;
            for (int guard = 0; c >= 0 && field[c] > 0 && guard < 200; guard++) {
                mark(c);
                int x = c % N, y = c / N, nxt = -1;
                for (int k = 0; k < 4; k++) {
                    int nx = x + DX[k], ny = y + DY[k];
                    if (!inb(nx, ny)) continue;
                    int nc = C(nx, ny);
                    if (field[nc] >= 0 && field[nc] < field[c]) {
                        nxt = nc;
                        break;
                    }
                }
                c = nxt;
            }
        }
    }

    // reachable free cells from a reference cell, with an extra blocked footprint
    int reachCount(int ref, int bx, int by, int bs, std::vector<char>* seen) {
        const size_t cells = static_cast<size_t>(N) * N;
        std::vector<char> vis(cells, 0);
        std::vector<int> q;
        q.reserve(cells);
        q.push_back(ref);
        vis[ref] = 1;
        auto blocked = [&](int c) {
            if (solid[c]) return true;
            if (bs > 0) {
                int x = c % N, y = c / N;
                if (x >= bx && x < bx + bs && y >= by && y < by + bs) return true;
            }
            return false;
        };
        for (size_t h = 0; h < q.size(); h++) {
            int c = q[h];
            int x = c % N, y = c / N;
            for (int k = 0; k < 4; k++) {
                int nx = x + DX[k], ny = y + DY[k];
                if (!inb(nx, ny)) continue;
                int nc = C(nx, ny);
                if (vis[nc] || blocked(nc)) continue;
                vis[nc] = 1;
                q.push_back(nc);
            }
        }
        if (seen) *seen = std::move(vis);
        return static_cast<int>(q.size());
    }

    // level 0: all reservations; 1: only the streets of the bases; 2: only connectivity; 3: anything free
    bool chooseSite(EntityType kind, int s, int* ox, int* oy, int level = 0) {
        if (bases.empty()) return false;
        computeBoulevard();
        int rc = rallyCell();
        std::vector<std::array<int, 3>> bl;  // base-like footprints (also under construction)
        for (int i : baseLikes()) bl.push_back({ents[i].x, ents[i].y, ents[i].size});
        for (const Proj& p : projs)
            if (p.size >= 5) bl.push_back({p.x, p.y, p.size});
        std::vector<int> ring;
        for (int i : bases)
            for (int c : perimeter(ents[i].x, ents[i].y, ents[i].size))
                if (walkable(c)) ring.push_back(c);
        std::vector<int> dist;
        bfsField(ring, [&](int c) { return !solid[c]; }, dist, 45);
        struct Cand {
            double sc;
            int x, y;
        };
        std::vector<Cand> cands;
        for (int y = 0; y + s <= N; y++)
            for (int x = 0; x + s <= N; x++) {
                if (!footprintFree(x, y, s)) continue;
                if (inProject(x, y, s)) continue;
                bool bad = false;
                if (level <= 1)
                    for (int yy = y; yy < y + s && !bad; yy++)
                        for (int xx = x; xx < x + s; xx++)
                            if (level == 0 ? boulevard[C(xx, yy)] : streetOnly[C(xx, yy)]) {
                                bad = true;
                                break;
                            }
                if (bad) continue;
                // keep one free line around every base (its ring)
                for (auto& b : bl)
                    if (distRect(x, y, s, b[0], b[1], b[2]) < (level <= 2 ? 2 : 1)) {
                        bad = true;
                        break;
                    }
                if (bad) continue;
                int dmin = INT_MAX;
                bool reach = false;
                int freeRing = 0, resAround = 0, channels = 0;
                for (int yy = y - 1; yy <= y + s; yy++)
                    for (int xx = x - 1; xx <= x + s; xx++) {
                        if (!inb(xx, yy)) continue;
                        bool inside = xx >= x && xx < x + s && yy >= y && yy < y + s;
                        if (inside) continue;
                        int c = C(xx, yy);
                        if (resHp[c] > 0) resAround++;
                        int d = dist[c];
                        if (d >= 0) {
                            reach = true;
                            dmin = std::min(dmin, d);
                            freeRing++;
                        }
                    }
                if (!reach) continue;
                if (s >= 5 && freeRing < 12) continue;  // a base needs room around it for its units
                // a free strip one or two cells wide between the footprint and another building is a channel that a
                // couple of idle units can plug: avoid (touching is fine, three or more free cells is fine)
                for (int i = 0; i < s; i++) {
                    int dirs[4][4] = {{x + i, y - 1, 0, -1}, {x + i, y + s, 0, 1}, {x - 1, y + i, -1, 0}, {x + s, y + i, 1, 0}};
                    for (auto& d : dirs) {
                        int cx2 = d[0], cy2 = d[1];
                        if (!inb(cx2, cy2) || solid[C(cx2, cy2)]) continue;  // touching: no gap
                        for (int w = 1; w <= 2; w++) {
                            int nx2 = cx2 + d[2] * w, ny2 = cy2 + d[3] * w;
                            if (!inb(nx2, ny2)) break;
                            int c2 = C(nx2, ny2);
                            if (solid[c2]) {
                                if (resHp[c2] == 0) channels++;  // a building, not a resource wall
                                break;
                            }
                        }
                    }
                }
                if (channels > 0 && level <= 1) continue;
                int ds[64];
                int nd = 0;
                for (int bi : mineBuilders) {
                    if (nd >= 64) break;
                    const Ent& u = ents[bi];
                    ds[nd++] = distRect(u.x, u.y, 1, x, y, s);
                }
                int k = std::min(nd, cfgRepairers);
                int walk = 0;
                if (k > 0) {
                    std::nth_element(ds, ds + k - 1, ds + nd);
                    std::sort(ds, ds + k);
                    for (int i = 0; i < k; i++) walk += ds[i];
                }
                // houses cluster in the corner behind the base; the army's base stays close to the home
                double sc = walk * cfgWalkW / 10.0 + dmin * cfgBaseW / 10.0 + (x + y) * cfgCornerW / 100.0 + resAround * 2.0;
                if (kind == HOUSE && houseCount >= cfgEarlyHouses) {
                    // later houses stand in rows along the map's border, out of the soldiers' way
                    int edge = std::min(std::min(x, y), std::min(N - x - s, N - y - s));
                    int nb = 0;  // neighbouring houses: compact rows
                    for (int i : mineBuildings)
                        if (ents[i].type == HOUSE && distRect(x, y, s, ents[i].x, ents[i].y, 3) <= 1) nb++;
                    sc = edge * cfgEdgeW / 10.0 + dmin * 0.5 + resAround * 2.0 - std::min(nb, 3) * 2.0;
                }
                if (kind == TURRET) {
                    double dr = rc >= 0 ? std::abs(x + 1 - rc % N) + std::abs(y + 1 - rc / N) : 0;
                    int nb = 0;
                    for (int i : mineBuildings)
                        if (ents[i].type == TURRET && distRect(x, y, s, ents[i].x, ents[i].y, 2) <= 4) nb++;
                    // between the gathering place and the enemy: farther along the diagonal is better
                    sc = std::abs(dr - cfgTurretRing) * 1.0 - std::min(nb, 3) * 1.5 + resAround * 1.0 - (x + y) * 0.05;
                } else if (kind == RANGED_BASE) {
                    // close to the gathering place, with the street to it open
                    double dr = rc >= 0 ? std::abs(x + s / 2 - rc % N) + std::abs(y + s / 2 - rc / N) : 0;
                    sc = dmin * 0.3 + dr * 1.0 + resAround * 2.0;
                }
                cands.push_back({sc, x, y});
            }
        std::sort(cands.begin(), cands.end(), [](const Cand& a, const Cand& b) { return a.sc < b.sc; });
        if (level >= 3) {
            if (cands.empty()) return false;
            *ox = cands[0].x;
            *oy = cands[0].y;
            return true;
        }
        // connectivity: the new footprint must not cut anything off from the rally point
        std::vector<char> before;
        int refCell = rc >= 0 ? rc : ring.empty() ? -1 : ring[0];
        int nBefore = refCell >= 0 ? reachCount(refCell, 0, 0, 0, &before) : 0;
        auto slotsIn = [&](const std::vector<char>& vis, int bx, int by, int bs) {
            int n = 0;
            for (int c = 0; c < N * N; c++) {
                if (!vis[c] || solid[c]) continue;
                int x = c % N, y = c / N;
                if (bs > 0 && x >= bx && x < bx + bs && y >= by && y < by + bs) continue;
                bool adj = false;
                for (int k = 0; k < 4; k++) {
                    int nx = x + DX[k], ny = y + DY[k];
                    if (inb(nx, ny) && resHp[C(nx, ny)] > 0) adj = true;
                }
                if (adj) n++;
            }
            return n;
        };
        int slotsBefore = refCell >= 0 ? slotsIn(before, 0, 0, 0) : 0;
        int tried = 0;
        for (const Cand& c : cands) {
            if (tried++ >= 14) break;
            if (refCell >= 0) {
                std::vector<char> after;
                int nAfter = reachCount(refCell, c.x, c.y, s, &after);
                int footprintReach = 0;
                for (int yy = c.y; yy < c.y + s; yy++)
                    for (int xx = c.x; xx < c.x + s; xx++)
                        if (before[C(xx, yy)]) footprintReach++;
                if (nBefore - footprintReach - nAfter > 2) continue;  // some free cells got cut off
                if (slotsIn(after, c.x, c.y, s) < slotsBefore - 2) continue;  // mining slots got cut off
                bool ok = true;
                for (int cell : ring)
                    if (before[cell] && !after[cell] &&
                        !(cell % N >= c.x && cell % N < c.x + s && cell / N >= c.y && cell / N < c.y + s)) {
                        ok = false;
                        break;
                    }
                if (!ok) continue;
            }
            *ox = c.x;
            *oy = c.y;
            return true;
        }
        return false;
    }

    // ---------------------------------------------------------------------------------------------------------
    // main per tick routine
    bool lite = false;  // CPU safety valve: cheaper decisions when the process has used too much time
    Action act(const PlayerView& v) {
        auto t0 = std::chrono::steady_clock::now();
        {
            double cpu = static_cast<double>(std::clock()) / CLOCKS_PER_SEC;
            double allowed = cfgCpuBase + cfgCpuPerTick * v.currentTick;
            lite = cpu > allowed;
            if (cpu > cfgCpuPanic) return Action(std::unordered_map<int, EntityAction>());  // out of budget: keep the old orders
        }
        readWorld(v);
        planEconomy();
        assignFire();
        assignArmy();
        assignBuilders();
        for (int bi : mineBuilders) {
            int id = ents[bi].id;
            if (!spawnTick.count(id)) spawnTick[id] = tick;
            auto it = orders.find(id);
            if (it != orders.end() && it->second.attackAction && !firstMineTick.count(id)) {
                firstMineTick[id] = tick;
                delaySum += tick - spawnTick[id];
                delayN++;
            }
            if (it == orders.end()) actIdle++;
            else if (it->second.attackAction) actMine++;
            else if (it->second.repairAction) actRepair++;
            else if (it->second.buildAction) actBuild++;
            else if (it->second.moveAction) {
                const auto& m = it->second.moveAction;
                if (m->target.x == ents[bi].x && m->target.y == ents[bi].y) actIdle++;
                else actWalk++;
            } else actIdle++;
        }
        Action out;
        // every entity of mine gets an explicit order: a forgotten one would repeat its old one
        for (const Ent& e : ents)
            if (e.mine && !e.stale && !orders.count(e.id)) orders[e.id] = EntityAction();
        for (auto& kv : orders) out.entityActions[kv.first] = kv.second;
        if (cfgLog && tick % cfgLog == 0) logState(t0);
        return out;
    }

    void logState(std::chrono::steady_clock::time_point t0) {
        double ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
        int houses = 0, bases_ = 0, turrets = 0;
        for (int i : mineBuildings) {
            if (ents[i].type == HOUSE) houses++;
            else if (ents[i].type == TURRET) turrets++;
            else bases_++;
        }
        std::fprintf(stderr,
                     "[t=%4d] res=%5d score=%6d B=%2d R=%2d M=%2d H=%2d Bs=%d T=%d pop=%d/%d foes=%zu atk=%d cpu=%.2fs tick=%.1fms\n",
                     tick, resources, score, (int)mineBuilders.size(), (int)mineRanged.size(), (int)mineMelee.size(),
                     houses, bases_, turrets, popUsed, popProvided, foeUnits.size(), (int)attackPhase,
                     (double)std::clock() / CLOCKS_PER_SEC, ms);
        {   // free mining slots reachable from my base ring vs. builders
            std::vector<int> ringc;
            for (int i : mineBuildings)
                if (ents[i].type == BUILDER_BASE)
                    for (int c : perimeter(ents[i].x, ents[i].y, ents[i].size))
                        if (walkable(c)) ringc.push_back(c);
            std::vector<int> d;
            bfsField(ringc, [&](int c) { return !solid[c]; }, d);
            int slotsAll = 0, slotsReach = 0;
            for (int c = 0; c < N * N; c++) {
                if (solid[c]) continue;
                int x = c % N, y = c / N;
                bool adjRes = false;
                for (int k = 0; k < 4; k++) {
                    int nx = x + DX[k], ny = y + DY[k];
                    if (inb(nx, ny) && resHp[C(nx, ny)] > 0) adjRes = true;
                }
                if (!adjRes) continue;
                slotsAll++;
                if (d[c] >= 0) slotsReach++;
            }
            std::fprintf(stderr, "        slots t=%d: %d reachable of %d (builders %zu)\n", tick, slotsReach, slotsAll, mineBuilders.size());
        }
        long tot = actMine + actWalk + actRepair + actBuild + actIdle;
        if (tot > 0)
            std::fprintf(stderr, "        builders: mine %.0f%% walk %.0f%% repair %.0f%% build %.0f%% idle %.0f%%\n", 100.0 * actMine / tot,
                         100.0 * actWalk / tot, 100.0 * actRepair / tot, 100.0 * actBuild / tot, 100.0 * actIdle / tot);
        std::fprintf(stderr, "        waits: no goal %ld (bfs found none %ld, plug rejections %ld, parked %ld), first step blocked %ld, standing on goal %ld\n", whyNoGoal,
                     whyBfsNone, whyPlug, whyPark, whyCommit, whyAtGoal);
        whyNoGoal = whyCommit = whyAtGoal = whyPlug = whyPark = whyBfsNone = 0;
        if (delayN) std::fprintf(stderr, "        new builders: %ld started mining after %.1f ticks on average\n", delayN, (double)delaySum / delayN);
        delaySum = delayN = 0;
        actMine = actWalk = actRepair = actBuild = actIdle = 0;
    }

    // ---------------------------------------------------------------------------------------------------------
    // economy: pop, houses, bases, unit production
    int reservedGold = 0;

    int projectsOf(EntityType k) const {
        int n = 0;
        for (const Proj& p : projs)
            if (p.kind == k) n++;
        return n;
    }

    std::map<int, int> nextTry;  // project kind -> the tick before which no new site search is made
    bool startProject(EntityType kind, int size) {
        auto nt = nextTry.find(static_cast<int>(kind));
        if (nt != nextTry.end() && tick < nt->second) return false;
        int x, y;
        bool found = false;
        for (int level = 0; level <= 3 && !found; level++) found = chooseSite(kind, size, &x, &y, level);
        if (!found) {
            nextTry[static_cast<int>(kind)] = tick + 6;  // no room: look again in a few ticks (mining makes room)
            return false;
        }
        Proj p;
        p.kind = kind;
        p.x = x;
        p.y = y;
        p.size = size;
        p.cost = pr[kind].cost;
        p.born = tick;
        projs.push_back(p);
        for (int yy = y; yy < y + size; yy++)
            for (int xx = x; xx < x + size; xx++) forbid[C(xx, yy)] = 1;
        if (cfgLog) std::fprintf(stderr, "[t=%4d] new project %d at (%d,%d) res=%d\n", tick, (int)kind, x, y, resources);
        return true;
    }

    void planEconomy() {
        reservedGold = 0;
        // update projects
        for (size_t i = 0; i < projs.size();) {
            Proj& p = projs[i];
            if (p.bid < 0) {
                for (int j : mineBuildings) {
                    const Ent& e = ents[j];
                    if (e.type == p.kind && e.x == p.x && e.y == p.y) {
                        p.bid = e.id;
                        p.placedTick = tick;
                        if (cfgLog) std::fprintf(stderr, "[t=%4d] project %d placed at (%d,%d), born %d\n", tick, (int)p.kind, p.x, p.y, p.born);
                    }
                }
            }
            bool drop = false;
            if (p.bid >= 0) {
                auto it = idx.find(p.bid);
                if (it == idx.end()) drop = true;                // destroyed
                else if (ents[it->second].active) drop = true;   // finished
            } else if (tick - p.born > 120 || p.failTicks > 8) {
                drop = true;  // never got placed
            }
            if (drop) {
                if (cfgLog)
                    std::fprintf(stderr, "[t=%4d] project %d at (%d,%d) born %d placed %d done/dropped bid=%d  repair walk avg %.1f (n=%ld)\n", tick, (int)p.kind, p.x, p.y,
                                 p.born, p.placedTick, p.bid, p.repDistN ? (double)p.repDistSum / p.repDistN : 0.0, p.repDistN);
                projs.erase(projs.begin() + i);
                continue;
            }
            for (int yy = p.y; yy < p.y + p.size; yy++)
                for (int xx = p.x; xx < p.x + p.size; xx++)
                    if (inb(xx, yy)) forbid[C(xx, yy)] = 1;
            if (p.bid < 0) {
                p.near = !cfgLazy;
                if (resources >= p.cost) p.near = true;
                auto it = idx.find(p.builder);
                if (p.builder >= 0 && it != idx.end() && distRect(ents[it->second].x, ents[it->second].y, 1, p.x, p.y, p.size) <= 3)
                    p.near = true;
                if (p.near) reservedGold += p.cost;
            }
            i++;
        }
        int nBuilders = static_cast<int>(mineBuilders.size());
        int army = static_cast<int>(mineRanged.size() + mineMelee.size());
        if (rbX >= 0)
            for (int yy = rbY; yy < rbY + 5; yy++)
                for (int xx = rbX; xx < rbX + 5; xx++) forbid[C(xx, yy)] = 1;
        // houses: keep the free population above a threshold that grows with the production rate
        int freePop = popProvided - popUsed;
        int incoming = 5 * (projectsOf(HOUSE));
        int thr = cfgHouseBase + (nBuilders + army) / std::max(1, cfgHouseSlope);
        bool lean = freePop + incoming < thr;
        bool rich = (resources - reservedGold >= cfgRichGold) && (freePop + incoming < cfgRichPop);
        if ((lean || rich) && nBuilders >= 1 && !bases.empty() && projectsOf(HOUSE) < cfgMaxHouses) startProject(HOUSE, 3);
        // the ranged base: its site is reserved early so that nothing else is built there, and built when the economy allows
        bool haveRB = false;
        for (int i : mineBuildings)
            if (ents[i].type == RANGED_BASE) haveRB = true;
        if (!haveRB && projectsOf(RANGED_BASE) == 0 && !bases.empty()) {
            if (rbX >= 0) {
                bool ok = true;  // the reserved site must still be free
                for (int yy = rbY; yy < rbY + 5 && ok; yy++)
                    for (int xx = rbX; xx < rbX + 5; xx++)
                        if (occ[C(xx, yy)] >= 0 && (pr[ents[occ[C(xx, yy)]].type].canMove == false)) {
                            ok = false;
                            break;
                        }
                if (!ok) rbX = rbY = -1;
            }
            if (rbX < 0 && tick >= 20 && nBuilders >= 3) {
                int x, y;
                bool got = false;
                for (int level = 0; level <= 3 && !got; level++) got = chooseSite(RANGED_BASE, 5, &x, &y, level);
                if (got) {
                    rbX = x;
                    rbY = y;
                    if (cfgLog) std::fprintf(stderr, "[t=%4d] ranged base site reserved at (%d,%d)\n", tick, x, y);
                }
            }
            bool urgent = homeThreat && nBuilders >= 14 && tick >= 100;
            if (rbX >= 0 && (nBuilders >= cfgRbAt || urgent)) {
                Proj p;
                p.kind = RANGED_BASE;
                p.x = rbX;
                p.y = rbY;
                p.size = 5;
                p.cost = pr[RANGED_BASE].cost;
                p.born = tick;
                projs.push_back(p);
                rbX = rbY = -1;
                if (cfgLog) std::fprintf(stderr, "[t=%4d] ranged base project at (%d,%d) res=%d\n", tick, p.x, p.y, resources);
            }
        }

        // a melee base, once the ranged one works (Round 1 has one from the start)
        if (cfgMeleeRatio2 > 0) {
            bool haveMB = false, rbActive = false;
            for (int i : mineBuildings) {
                if (ents[i].type == MELEE_BASE) haveMB = true;
                if (ents[i].type == RANGED_BASE && ents[i].active) rbActive = true;
            }
            if (!haveMB && rbActive && projectsOf(MELEE_BASE) == 0 && nBuilders >= cfgMbAt && tick >= 250 && resources - reservedGold >= 350)
                startProject(MELEE_BASE, 5);
        }
        // a second ranged base doubles the output when the money piles up
        {
            int rbs = projectsOf(RANGED_BASE);
            for (int i : mineBuildings)
                if (ents[i].type == RANGED_BASE) rbs++;
            if (rbs == 1 && projectsOf(RANGED_BASE) == 0 && tick >= 330 && resources - reservedGold >= cfgSecondRbGold && nBuilders >= 50)
                startProject(RANGED_BASE, 5);
        }
        // turrets: defence that needs no population, paid from the spare money once the army exists
        {
            int turrets = projectsOf(TURRET);
            for (int i : mineBuildings)
                if (ents[i].type == TURRET) turrets++;
            bool haveRB2 = false;
            for (int i : mineBuildings)
                if (ents[i].type == RANGED_BASE && ents[i].active) haveRB2 = true;
            bool emergency = homeThreat && nBuilders >= 12;
            if ((haveRB2 || emergency) && turrets < cfgMaxTurrets && projectsOf(TURRET) < 2 && (nBuilders >= 30 || emergency) &&
                resources - reservedGold >= (emergency ? 60 : cfgTurretGold) && (tick >= 150 || emergency))
                startProject(TURRET, 2);
        }

        // unit production
        int avail = resources - reservedGold;
        int pop = popProvided - popUsed;
        // the army follows a target curve over time; behind it, soldiers are bought before builders
        if (tick == 0) {
            for (int i : mineBuildings)
                if (ents[i].type == RANGED_BASE) startedWithRB = true;
        }
        int rTarget;
        if (startedWithRB) {   // Round 1: the bases are there from the start, and so are the neighbours' first units
            int early = std::max(0, std::min(tick, 200) - cfgArmyStart1) * cfgEarlyRate1 / 100;
            rTarget = early + std::max(0, tick - 200) * cfgArmyRate1 / 100;
        } else {
            rTarget = std::max(0, (tick - cfgArmyStart) * cfgArmyRate / 100);
        }
        bool endgame = tick >= 960;
        bool armyFirst = static_cast<int>(mineRanged.size()) < rTarget || homeThreat || endgame;
        for (int k = 0; k < 2; k++) {
            int pass = armyFirst ? 1 - k : k;
            for (int bi : bases) {
                const Ent& b = ents[bi];
                EntityType unit;
                if (pass == 0 && b.type == BUILDER_BASE) unit = BUILDER_UNIT;
                else if (pass == 1 && b.type == RANGED_BASE) unit = RANGED_UNIT;
                else if (pass == 1 && b.type == MELEE_BASE) unit = MELEE_UNIT;
                else continue;
                if (unit == BUILDER_UNIT && (nBuilders >= cfgTargetBuilders || endgame)) continue;
                // melee soldiers (tough, but they have to reach their target) join in a fixed share of the ranged ones
                if (unit == MELEE_UNIT && static_cast<int>(mineMelee.size()) * 100 >= (startedWithRB ? cfgMeleeRatio : cfgMeleeRatio2) * (static_cast<int>(mineRanged.size()) + 1)) continue;
                int cost = unitCost(unit);
                if (avail < cost || pop < 1) continue;
                int cell = chooseSpawn(b, unit == BUILDER_UNIT);
                if (cell < 0) continue;
                order(b.id, mkBuild(unit, cell % N, cell / N));
                avail -= cost;
                pop--;
                if (unit == BUILDER_UNIT) nBuilders++;
            }
        }
    }

    // spawn cell next to base b: free; builders near the mining front, soldiers toward the rally point
    std::vector<int> mineField;
    int chooseSpawn(const Ent& b, bool forBuilder) {
        std::vector<int> goals;
        if (forBuilder) {
            for (int c = 0; c < N * N; c++)
                if (isSlot(c)) goals.push_back(c);
        } else {
            int rc = rallyCell();
            if (rc >= 0) goals.push_back(rc);
        }
        bfsField(goals, [&](int c) { return !solid[c]; }, mineField, 160);
        int best = -1, bestD = INT_MAX;
        for (int c : perimeter(b.x, b.y, b.size)) {
            if (occ[c] >= 0 || forbid[c]) continue;
            if (mineField[c] < 0) continue;  // walled in: a unit born there could never leave
            int d = mineField[c];
            if (d < bestD) {
                bestD = d;
                best = c;
            }
        }
        return best;
    }

    // ---------------------------------------------------------------------------------------------------------
    // builders
    struct Plan {
        int id;    // entity id
        int from;  // cell
        int to;    // first step
    };
    std::vector<Plan> plans;

    // a builder may walk through any cell without a building, a resource, an enemy, or a unit of mine that stays put
    bool bpass(int c) const {
        if (solid[c] || blockCell[c] || forbid[c]) return false;
        int o = occ[c];
        if (o >= 0 && !ents[o].mine) return false;
        return true;
    }

    void stayPut(const Ent& u) {
        int c = C(u.x, u.y);
        resv[c] = 1;
        blockCell[c] = 1;
    }

    // record the intention to step to cell `to`
    void planStep(const Ent& u, int to) {
        plans.push_back({u.id, C(u.x, u.y), to});
        busy[u.id] = 1;
        planned[to] = 1;
    }

    // resolve the planned steps in rounds as the engine does: a unit may enter a cell that a unit ahead of it leaves
    void commitPlans() {
        std::unordered_map<int, int> planAt;  // cell -> plan index of the unit standing there
        for (size_t k = 0; k < plans.size(); k++) planAt[plans[k].from] = static_cast<int>(k);
        std::vector<char> done(plans.size(), 0);
        bool progress = true;
        for (int round = 0; progress && round < 40; round++) {
            progress = false;
            for (size_t k = 0; k < plans.size(); k++) {
                if (done[k]) continue;
                int to = plans[k].to;
                if (resv[to]) continue;
                int o = occ[to];
                if (o >= 0) {
                    auto it = planAt.find(to);
                    if (it == planAt.end() || !done[it->second]) continue;  // the occupant has not left (yet)
                }
                done[k] = 1;
                resv[to] = 1;
                progress = true;
                orders[plans[k].id] = mkMove(to % N, to / N);
            }
        }
        for (size_t k = 0; k < plans.size(); k++)
            if (!done[k]) {
                whyCommit++;
                int c = plans[k].from;
                orders[plans[k].id] = mkMove(c % N, c / N);
                resv[c] = 1;
                blockCell[c] = 1;
            }
        plans.clear();
    }

    // ---- plug avoidance: a builder standing in a narrow passage can cut off every slot behind it
    std::vector<int> ringCells;      // free cells around my builder bases
    std::vector<char> rejectedSlot;  // slots refused this tick because taking them would plug a passage
    int slotBase = -1;               // cached count of free slots reachable from the ring (-1: stale)
    std::vector<int> pbq;
    std::vector<char> pbVis;

    int slotsReachable(int skipCell) {
        const size_t cells = static_cast<size_t>(N) * N;
        pbVis.assign(cells, 0);
        pbq.clear();
        for (int c : ringCells)
            if (c != skipCell && !pbVis[c] && bpass(c)) {
                pbVis[c] = 1;
                pbq.push_back(c);
            }
        int count = 0;
        for (size_t h = 0; h < pbq.size(); h++) {
            int c = pbq[h];
            if (occ[c] < 0 && !claimed[c] && isSlot(c)) count++;
            int x = c % N, y = c / N;
            for (int k = 0; k < 4; k++) {
                int nx = x + DX[k], ny = y + DY[k];
                if (!inb(nx, ny)) continue;
                int nc = C(nx, ny);
                if (pbVis[nc] || nc == skipCell || !bpass(nc)) continue;
                pbVis[nc] = 1;
                pbq.push_back(nc);
            }
        }
        return count;
    }

    // would a builder standing on `cell` cut off slots other than that cell?
    int plugBudget = 0;
    bool plugs(int cell) {
        if (!cfgPlug || lite || ringCells.empty() || mineBuilders.size() < 16) return false;
        if (plugBudget <= 0) return false;
        plugBudget--;
        if (slotBase < 0) slotBase = slotsReachable(-1);
        int self = (occ[cell] < 0 && !claimed[cell] && isSlot(cell)) ? 1 : 0;
        int after = slotsReachable(cell);
        return slotBase - after - self >= 2;
    }

    int openNeighbors(int c) const {
        int x = c % N, y = c / N, n = 0;
        for (int dy = -1; dy <= 1; dy++)
            for (int dx = -1; dx <= 1; dx++) {
                if (!dx && !dy) continue;
                if (inb(x + dx, y + dy) && !solid[C(x + dx, y + dy)]) n++;
            }
        return n;
    }

    void computeDanger() {
        danger.assign(static_cast<size_t>(N) * N, 0);
        for (int fi : foeIdx) {
            const Ent& f = ents[fi];
            if (f.stale || !pr[f.type].canAttack || f.type == BUILDER_UNIT) continue;
            int r = f.type == MELEE_UNIT ? 2 : 6;   // shooting range plus one step: what can be hit on the next tick
            int x0 = std::max(f.x - r, 0), x1 = std::min(f.x + f.size + r, N);
            for (int x = x0; x < x1; x++) {
                int gap = std::max({0, f.x - x, x - (f.x + f.size - 1)});
                int rest = r - gap;
                int y0 = std::max(f.y - rest, 0), y1 = std::min(f.y + f.size + rest, N);
                for (int y = y0; y < y1; y++) danger[C(x, y)] = 1;
            }
        }
    }

    bool dangerNear(const Ent& b, int bs) const {  // is any perimeter cell of the building safe to stand on?
        for (int c : perimeter(b.x, b.y, bs))
            if (!danger[c]) return false;
        return true;
    }

    void assignBuilders() {
        plans.clear();
        computeDanger();
        ringCells.clear();
        for (int i : mineBuildings)
            if (ents[i].type == BUILDER_BASE)
                for (int c : perimeter(ents[i].x, ents[i].y, ents[i].size))
                    if (occ[c] < 0) ringCells.push_back(c);
        rejectedSlot.assign(static_cast<size_t>(N) * N, 0);
        plugBudget = 16;
        slotBase = -1;
        if (!lite && (tick % 5 == 0 || boulevard.empty())) computeBoulevard();
        // 0. builders in the reach of an enemy shooter run to the nearest safe cell
        if (cfgFlee) {
            for (int bi : mineBuilders) {
                const Ent& u = ents[bi];
                int start = C(u.x, u.y);
                if (!danger[start] || busy.count(u.id)) continue;
                int first = -1;
                // run to a mining slot out of reach; if there is none, stay and work (idling would only starve us)
                int safe = bfsNearest(
                    start, [&](int c) { return bpass(c); }, [&](int c) { return isSlot(c) && !danger[c] && occ[c] < 0 && !forbid[c]; }, &first, 18);
                if (safe >= 0 && first != start && !resv[first]) {
                    claimed[safe] = 1;
                    planStep(u, first);
                }
            }
        }
        // 1. builders that already stand next to a resource keep mining and block their cell
        std::vector<int> stayers, movers;
        for (int bi : mineBuilders) {
            if (busy.count(ents[bi].id)) continue;
            const Ent& u = ents[bi];
            if (adjacentResource(u) >= 0 && !forbid[C(u.x, u.y)]) {
                stayers.push_back(bi);
                claimed[C(u.x, u.y)] = 1;
                blockCell[C(u.x, u.y)] = 1;
                slotBase = -1;
            } else {
                movers.push_back(bi);
            }
        }
        // 2. construction projects take builders (repairers stand still, walkers make plans)
        for (Proj& p : projs) runProject(p);
        // 2b. damaged buildings of mine get a few repairers when it is quiet around them
        {
            std::vector<std::pair<int, int>> hurt;  // (missing hp, index)
            for (int i : mineBuildings) {
                const Ent& e = ents[i];
                int miss = pr[e.type].maxHp - e.hp;
                if (!e.active || miss * 100 < pr[e.type].maxHp * 25) continue;   // only real damage
                bool inProj = false;
                for (const Proj& p : projs)
                    if (p.bid == e.id) inProj = true;
                if (inProj || dangerNear(e, e.size)) continue;
                bool underFire = false;
                for (int fi : foeUnits) {
                    const Ent& f = ents[fi];
                    if (pr[f.type].canAttack && f.type != BUILDER_UNIT && distRect(f.x, f.y, 1, e.x, e.y, e.size) <= 10) underFire = true;
                }
                if (underFire) continue;
                hurt.push_back({miss, i});
            }
            std::sort(hurt.rbegin(), hurt.rend());
            for (size_t k = 0; k < hurt.size() && k < 2; k++) {
                const Ent& e = ents[hurt[k].second];
                Proj job;
                job.bid = e.id;
                job.kind = e.type;
                job.x = e.x;
                job.y = e.y;
                job.size = e.size;
                job.wantRep = e.type == TURRET ? 4 : (e.size >= 5 ? 6 : 2);
                job.maxWalk = 14;
                runProject(job);
            }
        }
        // 3. the rest mines
        minerCnt.assign(static_cast<size_t>(N) * N, 0);
        for (int bi : stayers) {
            const Ent& u = ents[bi];
            if (busy.count(u.id)) continue;
            int r = adjacentResource(u);
            order(u.id, mkAttack(ents[r].id));
            countMiner(u, r);
            resv[C(u.x, u.y)] = 1;
        }
        for (int bi : movers) {
            const Ent& u = ents[bi];
            if (busy.count(u.id)) continue;
            int start = C(u.x, u.y);
            int first = -1;
            int goal = -1;
            for (int attempt = 0; attempt < 6; attempt++) {
                bool fresh = cfgSpread && attempt < 3;  // first look for a slot at an unshared resource, nearby
                goal = bfsNearest(
                    start, [&](int c) { return bpass(c); },
                    [&](int c) {
                        return isSlot(c) && !danger[c] && !rejectedSlot[c] && (occ[c] < 0 || c == start) && !forbid[c] &&
                               (!fresh || freshSlot(c));
                    },
                    &first, fresh ? 12 : (1 << 20));
                if (goal < 0 && fresh) {
                    goal = bfsNearest(
                        start, [&](int c) { return bpass(c); },
                        [&](int c) { return isSlot(c) && !danger[c] && !rejectedSlot[c] && (occ[c] < 0 || c == start) && !forbid[c]; }, &first);
                }
                if (goal < 0) {  // nothing out of reach: work under fire rather than not at all
                    goal = bfsNearest(
                        start, [&](int c) { return bpass(c); },
                        [&](int c) { return isSlot(c) && !rejectedSlot[c] && (occ[c] < 0 || c == start) && !forbid[c]; }, &first);
                }
                if (goal < 0) {
                    whyBfsNone++;
                    break;
                }
                if (!plugs(goal)) break;
                whyPlug++;
                rejectedSlot[goal] = 1;
                goal = -1;
            }
            if (goal < 0) {
                // no slot that is safe to take: stand somewhere that blocks nothing
                int park = -1;
                bool onRing = false;
                for (int i : mineBuildings) {
                    EntityType t = ents[i].type;
                    if ((t == BUILDER_BASE || t == MELEE_BASE || t == RANGED_BASE) &&
                        distRect(u.x, u.y, 1, ents[i].x, ents[i].y, ents[i].size) <= 2)
                        onRing = true;
                }
                if (cfgPlug && (onRing || openNeighbors(start) < 6 || plugs(start))) {
                    int pf = -1;
                    park = bfsNearest(
                        start, [&](int c) { return bpass(c); },
                        [&](int c) {
                            if (c == start || occ[c] >= 0 || isSlot(c) || forbid[c] || openNeighbors(c) < 7) return false;
                            for (int i : mineBuildings) {
                                EntityType t = ents[i].type;
                                if ((t == BUILDER_BASE || t == MELEE_BASE || t == RANGED_BASE) &&
                                    distRect(c % N, c / N, 1, ents[i].x, ents[i].y, ents[i].size) <= 3)
                                    return false;
                            }
                            return !boulevard.empty() ? !boulevard[c] : true;
                        },
                        &pf, 30);
                    if (park >= 0 && pf != start && !resv[pf]) {
                        planStep(u, pf);
                        whyNoGoal++;
                        whyPark++;
                        continue;
                    }
                }
                order(u.id, mkMove(u.x, u.y));
                stayPut(u);
                whyNoGoal++;
                continue;
            }
            claimed[goal] = 1;
            slotBase = -1;
            {
                int gx = goal % N, gy = goal / N, bk = -1, bc = INT_MAX;
                for (int k = 0; k < 4; k++) {
                    int nx = gx + DX[k], ny = gy + DY[k];
                    if (inb(nx, ny) && resHp[C(nx, ny)] > 0 && minerCnt[C(nx, ny)] < bc) {
                        bc = minerCnt[C(nx, ny)];
                        bk = C(nx, ny);
                    }
                }
                if (bk >= 0) minerCnt[bk]++;
            }
            if (first == start) {
                order(u.id, mkMove(u.x, u.y));
                stayPut(u);
                whyAtGoal++;
            } else {
                planStep(u, first);
            }
        }
        commitPlans();
    }

    // the adjacent resource that the fewest of my builders work on (then the one with the least health)
    int adjacentResource(const Ent& u) const {
        int best = -1, bestHp = INT_MAX, bestCnt = INT_MAX;
        for (int k = 0; k < 4; k++) {
            int nx = u.x + DX[k], ny = u.y + DY[k];
            if (!inb(nx, ny)) continue;
            int c = C(nx, ny);
            int o = occ[c];
            if (o < 0 || ents[o].type != RESOURCE || ents[o].stale) continue;
            int cnt = (cfgSpread && !minerCnt.empty()) ? minerCnt[c] : 0;
            if (cnt < bestCnt || (cnt == bestCnt && ents[o].hp < bestHp)) {
                bestCnt = cnt;
                bestHp = ents[o].hp;
                best = o;
            }
        }
        return best;
    }

    // does cell c touch a resource that none of my builders work on?
    bool freshSlot(int c) const {
        int x = c % N, y = c / N;
        for (int k = 0; k < 4; k++) {
            int nx = x + DX[k], ny = y + DY[k];
            if (inb(nx, ny) && resHp[C(nx, ny)] > 0 && minerCnt[C(nx, ny)] == 0) return true;
        }
        return false;
    }

    void countMiner(const Ent& u, int resIdx) {
        const Ent& r = ents[resIdx];
        minerCnt[C(r.x, r.y)]++;
        (void)u;
    }

    // one builder places the building, others repair it
    void runProject(Proj& p) {
        if (p.bid < 0) {
            int b = -1;
            if (p.builder >= 0 && idx.count(p.builder) && !busy.count(p.builder)) b = idx[p.builder];
            if (b < 0) {
                int bestD = INT_MAX;
                for (int bi : mineBuilders) {
                    const Ent& u = ents[bi];
                    if (busy.count(u.id)) continue;
                    int d = distRect(u.x, u.y, 1, p.x, p.y, p.size);
                    if (d < bestD) {
                        bestD = d;
                        b = bi;
                    }
                }
            }
            if (b < 0) return;
            const Ent& u = ents[b];
            p.builder = u.id;
            if (adjacentTo(u, p.x, p.y, p.size)) {
                if (resources >= p.cost) {
                    order(u.id, mkBuild(p.kind, p.x, p.y));
                    p.failTicks++;
                } else {
                    order(u.id, mkMove(u.x, u.y));
                }
                stayPut(u);
            } else {
                if (!walkToPerimeter(u, p.x, p.y, p.size)) p.failTicks++;
            }
            return;
        }
        auto it = idx.find(p.bid);
        if (it == idx.end()) return;
        const Ent& bld = ents[it->second];
        int need = pr[bld.type].maxHp - bld.hp;
        if (need <= 0) return;
        int want = (bld.type == HOUSE) ? cfgRepairers : (bld.type == TURRET ? cfgRepairers + 1 : cfgBaseRepairers);
        if (bld.type == HOUSE && popProvided - popUsed <= 0) want = cfgRepCapped;  // growth is stalled: finish fast
        if (bld.type == TURRET && p.wantRep == 0) want = 8;
        if (p.wantRep > 0) want = p.wantRep;
        want = std::min(want, std::max(2, static_cast<int>(mineBuilders.size())));
        std::vector<std::pair<int, int>> cand;
        for (int bi : mineBuilders) {
            const Ent& u = ents[bi];
            if (busy.count(u.id)) continue;
            int dd = distRect(u.x, u.y, 1, bld.x, bld.y, bld.size);
            if (dd > p.maxWalk) continue;
            cand.push_back({dd, bi});
        }
        std::sort(cand.begin(), cand.end());
        int used = 0;
        for (auto& cd : cand) {
            if (used >= want) break;
            const Ent& u = ents[cd.second];
            if (adjacentTo(u, bld.x, bld.y, bld.size)) {
                order(u.id, mkRepair(bld.id));
                stayPut(u);
                claimed[C(u.x, u.y)] = 1;
                used++;
            }
        }
        for (auto& cd : cand) {
            if (used >= want) break;
            const Ent& u = ents[cd.second];
            if (busy.count(u.id)) continue;
            if (walkToPerimeter(u, bld.x, bld.y, bld.size)) {
                used++;
                p.repDistSum += cd.first;
                p.repDistN++;
            }
        }
    }

    bool walkToPerimeter(const Ent& u, int bx, int by, int bs) {
        int start = C(u.x, u.y);
        std::vector<char> isPer(static_cast<size_t>(N) * N, 0);
        for (int c : perimeter(bx, by, bs)) isPer[c] = 1;
        int first = -1;
        int goal = bfsNearest(
            start, [&](int c) { return bpass(c); },
            [&](int c) { return isPer[c] && !claimed[c] && (occ[c] < 0 || c == start) && !forbid[c]; }, &first);
        if (goal < 0) return false;
        claimed[goal] = 1;
        if (first == start) {
            order(u.id, mkMove(u.x, u.y));
            stayPut(u);
        } else {
            planStep(u, first);
        }
        return true;
    }

    // ---------------------------------------------------------------------------------------------------------
    // military: focus fire
    std::unordered_map<int, int> fireAt;  // shooter id -> target id

    double targetPrio(const Ent& t) const {
        const Pr& p = pr[t.type];
        int dmg = std::max(1, pr[RANGED_UNIT].damage);
        int hits = std::max(1, (t.hp + dmg - 1) / dmg);
        double value = static_cast<double>(p.destroyScore) / hits;
        double threat = 0;
        if (t.type == RANGED_UNIT) threat = 400;
        else if (t.type == MELEE_UNIT) threat = 250;
        else if (t.type == TURRET) threat = 100;
        if (t.type == WALL) value = 1;
        return value + threat;
    }

    void assignFire() {
        fireAt.clear();
        struct Sh {
            int i;
            int range;
            std::vector<int> cand;  // target slots in range
            bool used = false;
        };
        std::vector<Sh> shooters;
        auto addShooters = [&](const std::vector<int>& v) {
            for (int i : v)
                if (ents[i].active && pr[ents[i].type].canAttack) shooters.push_back({i, pr[ents[i].type].range, {}, false});
        };
        addShooters(mineRanged);
        addShooters(mineMelee);
        for (int i : mineBuildings)
            if (ents[i].type == TURRET && ents[i].active) shooters.push_back({i, pr[TURRET].range, {}, false});
        if (shooters.empty()) return;
        struct Tg {
            int i;
            double prio;
            int need;
            int hp;
            std::vector<int> sh;
        };
        std::vector<Tg> tg;
        for (int i : foeIdx) {
            const Ent& t = ents[i];
            if (t.stale) continue;
            tg.push_back({i, targetPrio(t), 0, t.hp, {}});
        }
        if (tg.empty()) return;
        int dmg = std::max(1, pr[RANGED_UNIT].damage);
        for (size_t a = 0; a < shooters.size(); a++) {
            const Ent& s = ents[shooters[a].i];
            for (size_t b = 0; b < tg.size(); b++) {
                const Ent& t = ents[tg[b].i];
                if (distRect(s.x, s.y, s.size, t.x, t.y, t.size) <= shooters[a].range) {
                    shooters[a].cand.push_back(static_cast<int>(b));
                    tg[b].sh.push_back(static_cast<int>(a));
                }
            }
        }
        for (auto& t : tg) t.need = std::max(1, (t.hp + dmg - 1) / dmg);
        std::vector<int> order_(tg.size());
        for (size_t b = 0; b < tg.size(); b++) order_[b] = static_cast<int>(b);
        std::sort(order_.begin(), order_.end(), [&](int a, int b) {
            if (tg[a].prio != tg[b].prio) return tg[a].prio > tg[b].prio;
            return tg[a].hp < tg[b].hp;
        });
        // pass 1: targets that can be killed this tick
        for (int b : order_) {
            Tg& t = tg[b];
            std::vector<int> free_;
            for (int a : t.sh)
                if (!shooters[a].used) free_.push_back(a);
            if (static_cast<int>(free_.size()) < t.need) continue;
            std::sort(free_.begin(), free_.end(), [&](int x, int y) { return shooters[x].cand.size() < shooters[y].cand.size(); });
            for (int k = 0; k < t.need; k++) {
                shooters[free_[k]].used = true;
                fireAt[ents[shooters[free_[k]].i].id] = ents[t.i].id;
            }
        }
        // pass 2: the rest shoot the best target in range, concentrating on one
        for (size_t a = 0; a < shooters.size(); a++) {
            if (shooters[a].used) continue;
            int best = -1;
            double bestKey = -1e18;
            for (int b : shooters[a].cand) {
                // prefer the target that already takes the most fire (finishes it), then priority
                double key = tg[b].prio - tg[b].hp * 0.5;
                if (key > bestKey) {
                    bestKey = key;
                    best = b;
                }
            }
            if (best >= 0) {
                shooters[a].used = true;
                fireAt[ents[shooters[a].i].id] = ents[tg[best].i].id;
            }
        }
        for (const Sh& s : shooters) {
            auto it = fireAt.find(ents[s.i].id);
            if (it != fireAt.end()) {
                order(ents[s.i].id, mkAttack(it->second));
                resv[C(ents[s.i].x, ents[s.i].y)] = 1;
                blockCell[C(ents[s.i].x, ents[s.i].y)] = 1;
            }
        }
    }

    // ---------------------------------------------------------------------------------------------------------
    // military: movement
    int rallyCache = -1;
    int rallyCell() {
        // a walkable cell about 9 cells from the main base toward the middle of the map; stable over the game
        if (rallyCache >= 0 && !solid[rallyCache]) return rallyCache;
        double bx0 = homeCx, by0 = homeCy;
        for (int i : mineBuildings)
            if (ents[i].type == BUILDER_BASE) {
                bx0 = ents[i].x + ents[i].size / 2.0;
                by0 = ents[i].y + ents[i].size / 2.0;
                break;
            }
        int bx = static_cast<int>(std::lround(bx0 + 9)), by = static_cast<int>(std::lround(by0 + 9));
        bx = std::min(std::max(bx, 0), N - 1);
        by = std::min(std::max(by, 0), N - 1);
        int best = -1, bestD = INT_MAX;
        for (int r = 0; r <= 14 && best < 0; r++)
            for (int dy = -r; dy <= r; dy++)
                for (int dx = -r; dx <= r; dx++) {
                    if (std::max(std::abs(dx), std::abs(dy)) != r) continue;
                    int x = bx + dx, y = by + dy;
                    if (!inb(x, y)) continue;
                    int c = C(x, y);
                    if (solid[c]) continue;
                    int d = std::abs(dx) + std::abs(dy);
                    if (d < bestD) {
                        bestD = d;
                        best = c;
                    }
                }
        rallyCache = best;
        return best;
    }

    int objId = -1;
    std::set<int> badObj;  // objectives that cannot be reached (building ids, or 1000+corner)

    bool objectiveStillValid() const {
        if (objX < 0) return false;
        if (objGuess) {
            for (int k = 0; k < 3; k++) {
                const int cx = k == 0 ? N - 10 : (k == 1 ? 5 : N - 10), cy = k == 0 ? 5 : (k == 1 ? N - 10 : N - 10);
                if (cx == objX && cy == objY) return !clearedCorner.count(k) && !badObj.count(1000 + k);
            }
            return false;
        }
        auto it = idx.find(objId);
        return it != idx.end() && !ents[it->second].mine && !badObj.count(objId);
    }

    // how much stronger than the rest an enemy looks: 0 for the weakest, 1 for the strongest (by score)
    double strengthOf(int owner) const {
        int lo = INT_MAX, hi = INT_MIN;
        for (const auto& kv : scoreOf)
            if (kv.first != me) {
                lo = std::min(lo, kv.second);
                hi = std::max(hi, kv.second);
            }
        auto it = scoreOf.find(owner);
        if (owner < 0 || it == scoreOf.end() || hi <= lo) return 0.5;
        return static_cast<double>(it->second - lo) / (hi - lo);
    }

    void chooseObjective(const std::vector<int>& army) {
        if (objectiveStillValid() && tick % 25 != 0) return;
        objX = -1;
        objGuess = false;
        objId = -1;
        double cx = homeCx, cy = homeCy;
        if (!army.empty()) {
            cx = cy = 0;
            for (int i : army) {
                cx += ents[i].x;
                cy += ents[i].y;
            }
            cx /= army.size();
            cy /= army.size();
        }
        double bestD = 1e18;
        for (int i : foeIdx) {
            const Ent& e = ents[i];
            if (pr[e.type].canMove || badObj.count(e.id)) continue;
            double d = (std::abs(e.x - cx) + std::abs(e.y - cy)) * (1.0 + cfgWeakBias / 100.0 * strengthOf(e.owner));
            if (e.type == WALL) d += 200;
            if (d < bestD) {
                bestD = d;
                objX = e.x;
                objY = e.y;
                objSize = e.size;
                objId = e.id;
            }
        }
        if (objX >= 0) return;
        // no building known: the corners where the enemies start (my frame: I am at the bottom left)
        const int corners[3][2] = {{N - 10, 5}, {5, N - 10}, {N - 10, N - 10}};
        for (int k = 0; k < 3; k++) {
            if (clearedCorner.count(k) || badObj.count(1000 + k)) continue;
            if (numPlayers == 2 && k != 2) continue;  // 1v1: the enemy sits in the opposite corner
            int x = corners[k][0], y = corners[k][1];
            double d = (std::abs(x - cx) + std::abs(y - cy)) * (1.0 + cfgWeakBias / 100.0 * strengthOf(cornerOwner[k]));
            if (d < bestD) {
                bestD = d;
                objX = x;
                objY = y;
                objSize = 5;
                objGuess = true;
            }
        }
    }

    void updateClearedCorners() {
        const int corners[3][2] = {{N - 10, 5}, {5, N - 10}, {N - 10, N - 10}};
        for (int fi : foeIdx) {  // whose buildings stand in which corner
            const Ent& f = ents[fi];
            if (pr[f.type].canMove) continue;
            for (int k = 0; k < 3; k++)
                if (std::abs(f.x - corners[k][0]) + std::abs(f.y - corners[k][1]) <= 12) cornerOwner[k] = f.owner;
        }
        for (int k = 0; k < 3; k++) {
            int x = corners[k][0], y = corners[k][1];
            bool seen = true;
            for (int dy = 0; dy < 5 && seen; dy++)
                for (int dx = 0; dx < 5; dx++)
                    if (!sight[C(x + dx, y + dy)]) seen = false;
            if (!seen) continue;
            bool foe = false;
            for (int fi : foeIdx)
                if (!pr[ents[fi].type].canMove && std::abs(ents[fi].x - x) + std::abs(ents[fi].y - y) <= 12) foe = true;
            if (!foe) clearedCorner.insert(k);
        }
    }

    // goal cells (standable) within Manhattan distance r of a footprint
    void addAround(std::vector<int>& goals, int x, int y, int size, int r) const {
        int x0 = std::max(x - r, 0), x1 = std::min(x + size + r, N);
        for (int xx = x0; xx < x1; xx++) {
            int gap = std::max({0, x - xx, xx - (x + size - 1)});
            int rest = r - gap;
            int y0 = std::max(y - rest, 0), y1 = std::min(y + size + rest, N);
            for (int yy = y0; yy < y1; yy++)
                if (!solid[C(xx, yy)]) goals.push_back(C(xx, yy));
        }
    }

    // the neighbour cell with a smaller field value that a unit may plan to enter; -1 when there is none
    int stepDown(const Ent& u, const std::vector<int>& field) const {
        int c0 = C(u.x, u.y);
        int d0 = field[c0];
        if (d0 <= 0) return -1;
        int best = -1, bestD = d0;
        for (int k = 0; k < 4; k++) {
            int nx = u.x + DX[k], ny = u.y + DY[k];
            if (!inb(nx, ny)) continue;
            int nc = C(nx, ny);
            if (field[nc] < 0 || field[nc] >= bestD) continue;
            if (solid[nc] || blockCell[nc] || resv[nc] || planned[nc]) continue;
            int o = occ[nc];
            if (o >= 0 && !ents[o].mine) continue;
            best = nc;
            bestD = field[nc];
        }
        return best;
    }

    // a free neighbour with the same field value (sidestep around a blocked unit); -1 when there is none
    int stepSideways(const Ent& u, const std::vector<int>& field) const {
        int c0 = C(u.x, u.y);
        int d0 = field[c0];
        int pick = -1;
        int off = (tick + u.id) % 4;
        for (int t = 0; t < 4; t++) {
            int k = (t + off) % 4;
            int nx = u.x + DX[k], ny = u.y + DY[k];
            if (!inb(nx, ny)) continue;
            int nc = C(nx, ny);
            if (field[nc] != d0 || solid[nc] || blockCell[nc] || resv[nc] || planned[nc]) continue;
            int o = occ[nc];
            if (o >= 0) continue;
            pick = nc;
            break;
        }
        return pick;
    }

    int anchor = -1;
    int holdTicks = 0;
    int anchorWait = 0;
    std::vector<int> objField;   // distance to the objective (ADVANCE only)
    std::unordered_map<int, int> stuckTicks;  // soldier id -> consecutive ticks without moving
    double anchorCredit = 0;
    bool retreatFlag = false;
    bool retreatNow = false;  // read by assignFire: a retreating army does not stop to shoot
    int lastEnemySeen = -1000;
    const char* armyMode = "rally";

    int nearestFree(int x, int y) const {
        x = std::min(std::max(x, 0), N - 1);
        y = std::min(std::max(y, 0), N - 1);
        for (int r = 0; r <= 10; r++)
            for (int dy = -r; dy <= r; dy++)
                for (int dx = -r; dx <= r; dx++) {
                    if (std::max(std::abs(dx), std::abs(dy)) != r) continue;
                    int xx = x + dx, yy = y + dy;
                    if (inb(xx, yy) && !solid[C(xx, yy)]) return C(xx, yy);
                }
        return -1;
    }

    void assignArmy() {
        std::vector<int> army;
        for (int i : mineRanged)
            if (ents[i].active) army.push_back(i);
        for (int i : mineMelee)
            if (ents[i].active) army.push_back(i);
        retreatNow = false;
        if (army.empty()) {
            waveActive = false;
            waveIds.clear();
            attackPhase = false;
            return;
        }
        updateClearedCorners();

        // ---- threats near home: armed enemy units close to my buildings or builders
        std::vector<int> threats;
        double threatPower = 0;
        for (int fi : foeUnits) {
            const Ent& f = ents[fi];
            if (!pr[f.type].canAttack || f.type == BUILDER_UNIT) continue;
            bool near = false;
            for (int bi : mineBuildings) {
                const Ent& b = ents[bi];
                if (distRect(f.x, f.y, 1, b.x, b.y, b.size) <= 14) {
                    near = true;
                    break;
                }
            }
            if (!near)
                for (int bi : mineBuilders) {
                    const Ent& b = ents[bi];
                    if (std::abs(f.x - b.x) + std::abs(f.y - b.y) <= 14) {
                        near = true;
                        break;
                    }
                }
            if (near) {
                threats.push_back(fi);
                threatPower += (f.type == MELEE_UNIT ? 1.5 : 1.0);
            }
        }

        homeThreat = !threats.empty();
        // ---- wave lifecycle
        if (!waveActive) {
            if (static_cast<int>(army.size()) >= cfgAttackAt && tick >= cfgAttackTick) {
                waveActive = true;
                waveIds.clear();
                for (int i : army) waveIds.insert(ents[i].id);
                waveSize0 = static_cast<int>(waveIds.size());
                anchor = rallyCell();
                retreatFlag = false;
                if (cfgLog) std::fprintf(stderr, "[t=%4d] wave launched with %d\n", tick, waveSize0);
            }
        } else {
            int alive = 0;
            for (int i : army)
                if (waveIds.count(ents[i].id)) alive++;
            if (alive < std::max(6, waveSize0 * 3 / 10)) {
                waveActive = false;
                waveIds.clear();
                anchor = -1;
                if (cfgLog) std::fprintf(stderr, "[t=%4d] wave over (%d left of %d)\n", tick, alive, waveSize0);
            }
        }
        // the home is attacked seriously: the wave is called off, everybody defends (a parked army protects nothing)
        if (waveActive && threatPower >= cfgRecallPower) {
            waveActive = false;
            waveIds.clear();
            anchor = -1;
            retreatFlag = false;
            if (cfgLog) std::fprintf(stderr, "[t=%4d] wave recalled: %.0f enemy shooters at home\n", tick, threatPower);
        }
        attackPhase = waveActive;
        if (waveActive && anchor < 0) anchor = rallyCell();
        if (waveActive && anchor >= 0 && solid[anchor]) {
            int na = nearestFree(anchor % N, anchor / N);
            anchor = na >= 0 ? na : rallyCell();
        }

        // ---- strength around the centre of action
        int ctr = waveActive ? anchor : rallyCell();
        int cx = ctr >= 0 ? ctr % N : 7, cy = ctr >= 0 ? ctr / N : 7;
        double ours = 0, theirs = 0;
        for (int i : army)
            if (std::abs(ents[i].x - cx) + std::abs(ents[i].y - cy) <= 14) ours += ents[i].type == MELEE_UNIT ? 1.5 : 1.0;
        std::vector<int> nearFoes;  // armed enemies (and turrets) near the centre
        for (int fi : foeIdx) {
            const Ent& f = ents[fi];
            if (f.stale || !pr[f.type].canAttack || f.type == BUILDER_UNIT) continue;
            if (std::abs(f.x - cx) + std::abs(f.y - cy) <= 15) {
                nearFoes.push_back(fi);
                theirs += (f.type == TURRET ? 2.0 : (f.type == MELEE_UNIT ? 1.2 : 1.0));
            }
        }
        if (theirs > 0) lastEnemySeen = tick;

        // ---- mode
        objField.clear();
        enum Mode { RALLY, DEFEND, ADVANCE, ENGAGE, HOLD, RETREAT } mode = RALLY;
        if (!waveActive) {
            if (threats.empty()) {
                mode = RALLY;
            } else {
                // at home: go out against a threat that we outnumber, otherwise wait for it at the gathering place
                double turretsNear = 0;
                for (int i : mineBuildings)
                    if (ents[i].type == TURRET && ents[i].active) turretsNear += 1.5;
                double mine = ours + turretsNear * 0.5;
                mode = mine >= threatPower * cfgDefendAbove / 100.0 ? DEFEND : RALLY;
            }
        } else if (threatPower >= cfgRecallPower) {
            mode = RETREAT;  // the home is attacked seriously: come back
        } else if (theirs > 0) {
            double ratio = ours / theirs;
            // contact: one of ours is within reach of an armed enemy; from then on running only gets us shot in the back
            bool contact = false;
            for (int fi : nearFoes) {
                for (int i : army)
                    if (distRect(ents[i].x, ents[i].y, 1, ents[fi].x, ents[fi].y, ents[fi].size) <= 7) {
                        contact = true;
                        break;
                    }
                if (contact) break;
            }
            if (retreatFlag) {
                if (ratio >= 1.3 || contact) retreatFlag = false;
            } else if (ratio < cfgRetreatBelow / 100.0 && !contact) {
                retreatFlag = true;
            }
            mode = retreatFlag ? RETREAT : (ratio >= cfgEngageAbove / 100.0 ? ENGAGE : HOLD);
            // an even standoff must not last for ever: after a while the side that is not behind goes in
            if (mode == HOLD) {
                if (++holdTicks >= cfgHoldMax && ratio >= 0.85) mode = ENGAGE;
            } else {
                holdTicks = 0;
            }
        } else {
            retreatFlag = false;
            mode = ADVANCE;
        }
        retreatNow = (mode == RETREAT);
        static const char* names[] = {"rally", "defend", "advance", "engage", "hold", "retreat"};
        armyMode = names[mode];

        static int prevAnchor = -1;
        if (cfgLog && waveActive && prevAnchor >= 0 && anchor >= 0 &&
            std::abs(prevAnchor % N - anchor % N) + std::abs(prevAnchor / N - anchor / N) > 3)
            std::fprintf(stderr, "[t=%4d] ANCHOR JUMP (%d,%d)->(%d,%d) mode=%s retreatFlag=%d threatPower=%.1f\n", tick, prevAnchor % N, prevAnchor / N,
                         anchor % N, anchor / N, names[mode], (int)retreatFlag, threatPower);
        prevAnchor = anchor;
        // ---- goal field of this mode
        std::vector<int> goals, field;
        std::vector<int> goalsM, fieldM;  // the same for melee soldiers, who have to stand next to their target
        int holdR = 1;
        if (mode == RALLY || mode == RETREAT) {
            int rc = rallyCell();
            if (rc >= 0) addAround(goals, rc % N, rc / N, 1, 2);
        } else if (mode == DEFEND) {
            for (int fi : threats) {
                addAround(goals, ents[fi].x, ents[fi].y, 1, 3);
                addAround(goalsM, ents[fi].x, ents[fi].y, 1, 1);
            }
        } else if (mode == ENGAGE) {
            for (int fi : nearFoes) {
                addAround(goals, ents[fi].x, ents[fi].y, ents[fi].size, 3);
                addAround(goalsM, ents[fi].x, ents[fi].y, ents[fi].size, 1);
            }
            for (int fi : foeIdx) {  // buildings near the centre are targets too
                const Ent& f = ents[fi];
                if (!pr[f.type].canMove && !f.stale && std::abs(f.x - cx) + std::abs(f.y - cy) <= 14 && f.type != WALL) {
                    addAround(goals, f.x, f.y, f.size, 3);
                    addAround(goalsM, f.x, f.y, f.size, 1);
                }
            }
        }
        std::vector<int> members;  // wave members (alive)
        if (waveActive)
            for (int i : army)
                if (waveIds.count(ents[i].id)) members.push_back(i);

        if (mode == ADVANCE || mode == RETREAT || mode == HOLD || mode == ENGAGE) {
            // move the anchor
            if (mode == ADVANCE) {
                chooseObjective(members);
                std::vector<int> og;
                if (objX >= 0) {
                    if (objGuess) addAround(og, objX + objSize / 2, objY + objSize / 2, 1, 2);
                    else addAround(og, objX, objY, objSize, 4);
                }
                if (og.empty()) {
                    mode = RALLY;  // nothing left to attack: go home
                    armyMode = "rally";
                    int rc = rallyCell();
                    if (rc >= 0) addAround(goals, rc % N, rc / N, 1, 2);
                } else {
                    std::vector<int>& fo = objField;
                    bfsField(og, [&](int c) { return !solid[c]; }, fo);
                    if (fo[anchor] < 0) {  // cannot get there
                        if (objGuess) {
                            for (int k = 0; k < 3; k++)
                                if (N - 10 == objX || 5 == objX) badObj.insert(1000 + (objX == N - 10 && objY == 5 ? 0 : (objX == 5 ? 1 : 2)));
                        } else {
                            badObj.insert(objId);
                        }
                    } else {
                        // the anchor stays a few cells ahead of the median member: the ball moves as fast as it can,
                        // and the ones in front wait for the rest by coming back to the anchor
                        std::vector<int> mf;
                        for (int i : members) {
                            int d = fo[C(ents[i].x, ents[i].y)];
                            if (d >= 0) mf.push_back(d);
                        }
                        bool gather = true;
                        if (mf.size() >= 4 && fo[anchor] <= cfgStageDist) {  // march freely, regroup before contact
                            std::sort(mf.begin(), mf.end());
                            int lead = cfgLead + static_cast<int>(std::sqrt(static_cast<double>(mf.size())) * 1.2);
                            int rear = mf[mf.size() * 4 / 5];
                            int target = std::max(0, rear - lead);
                            gather = fo[anchor] > target;
                            if (!gather && ++anchorWait >= cfgAnchorWait) gather = true;  // a jammed crowd must not stop the march for good
                            if (gather) anchorWait = 0;
                            if (cfgLog && tick % cfgLog == 0)
                                std::fprintf(stderr, "        anchor fo=%d rear(p80) member fo=%d lead=%d target=%d gather=%d (members %zu with a path)\n", fo[anchor], rear, lead, target, (int)gather, mf.size());
                        }
                        if (gather && fo[anchor] > 0) {
                            int best = -1, bestD = fo[anchor];
                            for (int k = 0; k < 4; k++) {
                                int nx = cx + DX[k], ny = cy + DY[k];
                                if (!inb(nx, ny)) continue;
                                int nc = C(nx, ny);
                                if (fo[nc] >= 0 && fo[nc] < bestD && !solid[nc]) {
                                    bestD = fo[nc];
                                    best = nc;
                                }
                            }
                            if (best >= 0) anchor = best;
                        }
                        // at the objective the soldiers go to the goal cells themselves
                        if (fo[anchor] <= 2) {
                            goals = og;
                            field = fo;
                            holdR = 0;
                            if (objGuess) addAround(goalsM, objX + objSize / 2, objY + objSize / 2, 1, 2);
                            else addAround(goalsM, objX, objY, objSize, 1);
                        }
                    }
                }
            } else if (mode == RETREAT) {
                int rc = rallyCell();
                std::vector<int> g2;
                if (rc >= 0) addAround(g2, rc % N, rc / N, 1, 2);
                std::vector<int> fr;
                bfsField(g2, [&](int c) { return !solid[c]; }, fr);
                if (anchor >= 0 && fr[anchor] > 0) {
                    int best = -1, bestD = fr[anchor];
                    for (int k = 0; k < 4; k++) {
                        int nx = anchor % N + DX[k], ny = anchor / N + DY[k];
                        if (!inb(nx, ny)) continue;
                        int nc = C(nx, ny);
                        if (fr[nc] >= 0 && fr[nc] < bestD && !solid[nc]) {
                            bestD = fr[nc];
                            best = nc;
                        }
                    }
                    if (best >= 0) anchor = best;
                }
            } else {
                // HOLD / ENGAGE: the anchor follows the centre of the members
                if (!members.empty()) {
                    double mx = 0, my = 0;
                    for (int i : members) {
                        mx += ents[i].x;
                        my += ents[i].y;
                    }
                    int na = nearestFree(static_cast<int>(mx / members.size() + 0.5), static_cast<int>(my / members.size() + 0.5));
                    if (na >= 0) anchor = na;
                }
            }
            cx = anchor % N;
            cy = anchor / N;
        }
        if (field.empty()) {
            if (mode == ADVANCE || mode == RETREAT) {
                // the soldiers gather around the anchor
                std::vector<int> ag;
                addAround(ag, cx, cy, 1, 0);
                bfsField(ag, [&](int c) { return !solid[c]; }, field);
                int nm = static_cast<int>(std::max<size_t>(members.size(), 1));
                holdR = mode == RETREAT ? 2 : std::min(std::max(static_cast<int>(std::sqrt(static_cast<double>(nm)) * 0.35) + 1, 2), 4);
            } else if (mode == HOLD) {
                // stand
            } else {
                bfsField(goals, [&](int c) { return !solid[c]; }, field);
            }
        }
        if (cfgLog && tick % cfgLog == 0 && !members.empty() && anchor >= 0) {
            std::vector<int> dd;
            for (int i : members) dd.push_back(std::abs(ents[i].x - anchor % N) + std::abs(ents[i].y - anchor / N));
            std::sort(dd.begin(), dd.end());
            std::fprintf(stderr, "        ball: member distance to anchor min %d p25 %d median %d p75 %d max %d (credit %.2f)\n", dd.front(), dd[dd.size() / 4],
                         dd[dd.size() / 2], dd[dd.size() * 3 / 4], dd.back(), anchorCredit);
        }
        if (!goalsM.empty() && fieldM.empty()) bfsField(goalsM, [&](int c) { return !solid[c]; }, fieldM);
        if (!field.empty() && cfgLog && tick % cfgLog == 0) {
            int unreachable = 0;
            for (int i : army)
                if (field[C(ents[i].x, ents[i].y)] < 0) unreachable++;
            std::fprintf(stderr, "        army: mode=%s n=%zu members=%zu anchor=(%d,%d) obj=(%d,%d%s) ours=%.0f theirs=%.1f unreachable=%d\n", armyMode,
                         army.size(), members.size(), cx, cy, objX, objY, objGuess ? "?" : "", ours, theirs, unreachable);
        } else if (cfgLog && tick % cfgLog == 0) {
            std::fprintf(stderr, "        army: mode=%s n=%zu members=%zu anchor=(%d,%d) obj=(%d,%d%s) ours=%.0f theirs=%.1f\n", armyMode, army.size(),
                         members.size(), cx, cy, objX, objY, objGuess ? "?" : "", ours, theirs);
        }

        // ---- soldiers standing on the ring of a base make room for the units it builds
        std::vector<char> baseRing(static_cast<size_t>(N) * N, 0);
        for (int bi : baseLikes())
            for (int c : perimeter(ents[bi].x, ents[bi].y, ents[bi].size)) baseRing[c] = 1;
        if (mode == RALLY || mode == HOLD || mode == DEFEND) {
            for (int i : army) {
                const Ent& u = ents[i];
                int c0 = C(u.x, u.y);
                if (!baseRing[c0] || busy.count(u.id)) continue;
                int best = -1, bestD = INT_MAX;
                for (int k = 0; k < 4; k++) {
                    int nx = u.x + DX[k], ny = u.y + DY[k];
                    if (!inb(nx, ny)) continue;
                    int nc = C(nx, ny);
                    if (baseRing[nc] || solid[nc] || blockCell[nc] || resv[nc] || planned[nc] || occ[nc] >= 0) continue;
                    int d = field.empty() || field[nc] < 0 ? 1000 : field[nc];
                    if (d < bestD) {
                        bestD = d;
                        best = nc;
                    }
                }
                if (best >= 0) planStep(u, best);
            }
        }
        // ---- the soldiers move (front first so that followers can enter the cells that open)
        auto fieldOf = [&](const Ent& u) -> const std::vector<int>& { return (u.type == MELEE_UNIT && !fieldM.empty()) ? fieldM : field; };
        std::vector<std::pair<int, int>> movers;
        for (int i : army) {
            const Ent& u = ents[i];
            if (busy.count(u.id)) continue;  // shooting
            const std::vector<int>& F = fieldOf(u);
            int d = F.empty() ? 0 : F[C(u.x, u.y)];
            movers.push_back({d < 0 ? 100000 : d, i});
        }
        std::sort(movers.begin(), movers.end());
        for (auto& mv : movers) {
            const Ent& u = ents[mv.second];
            int c0 = C(u.x, u.y);
            const std::vector<int>& F = fieldOf(u);
            if (F.empty() || F[c0] < 0) {
                stayPut(u);
                continue;
            }
            // wave travellers join the ball once they are close to the anchor
            if (waveActive && !waveIds.count(u.id) && std::abs(u.x - cx) + std::abs(u.y - cy) <= cfgBallR) waveIds.insert(u.id);
            if (F[c0] <= holdR) {
                stayPut(u);
                continue;
            }
            if (mode == ADVANCE && !objField.empty() && anchor >= 0 && objField[c0] >= 0 && objField[anchor] >= 0 &&
                objField[c0] < objField[anchor] && holdR > 0) {
                stayPut(u);  // already ahead of the anchor: wait there, do not push back through the others
                continue;
            }
            int nc = stepDown(u, F);
            if (nc < 0 && stuckTicks[u.id] >= 3) nc = stepSideways(u, F);
            if (nc < 0) {
                stuckTicks[u.id]++;
                stayPut(u);
                continue;
            }
            stuckTicks[u.id] = 0;
            planStep(u, nc);
        }
        commitPlans();
    }

};

Brain g_brain;

}  // namespace

MyStrategy::MyStrategy() {}

Action MyStrategy::getAction(const PlayerView& playerView, DebugInterface* debugInterface) {
    try {
        return g_brain.act(playerView);
    } catch (const std::exception& e) {
        std::fprintf(stderr, "[t=%d] exception: %s\n", playerView.currentTick, e.what());
    } catch (...) {
        std::fprintf(stderr, "[t=%d] unknown exception\n", playerView.currentTick);
    }
    return Action(std::unordered_map<int, EntityAction>());
}

void MyStrategy::debugUpdate(const PlayerView& playerView, DebugInterface& debugInterface) {
    debugInterface.send(DebugCommand::Clear());
    debugInterface.getState();
}
