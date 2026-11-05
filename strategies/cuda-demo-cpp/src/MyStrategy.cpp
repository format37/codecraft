// Demo strategy: the per-cell "distance to the nearest enemy" and "how many enemies can hit this
// cell" fields are computed on the GPU (gpu.cu) every tick; units walk down those fields.
#include "MyStrategy.hpp"
#include "gpu.hpp"

#include <climits>
#include <cstdio>

namespace {

int cellIndex(const Vec2Int& p, int mapSize) { return p.y * mapSize + p.x; }

// The neighbouring cell (or the cell itself) with the smallest field value.
Vec2Int descend(const Vec2Int& from, const std::vector<int>& field, int mapSize)
{
    Vec2Int best = from;
    int bestValue = field[cellIndex(from, mapSize)];
    const int dx[4] = {1, -1, 0, 0}, dy[4] = {0, 0, 1, -1};
    for (int k = 0; k < 4; ++k) {
        Vec2Int p(from.x + dx[k], from.y + dy[k]);
        if (p.x < 0 || p.y < 0 || p.x >= mapSize || p.y >= mapSize) {
            continue;
        }
        if (field[cellIndex(p, mapSize)] < bestValue) {
            bestValue = field[cellIndex(p, mapSize)];
            best = p;
        }
    }
    return best;
}

} // namespace

MyStrategy::MyStrategy()
{
    std::string info;
    gpuMapSize = 80; // every official format; re-initialised below if a game says otherwise
    gpuReady = gpuInit(gpuMapSize, info);
    fprintf(stderr, "[gpu] %s: %s\n", gpuReady ? "ready" : "unavailable, CPU fallback", info.c_str());
}

Action MyStrategy::getAction(const PlayerView& view, DebugInterface*)
{
    const int n = view.mapSize;
    if (n != gpuMapSize) {
        std::string info;
        gpuMapSize = n;
        gpuReady = gpuInit(n, info);
    }
    const int myId = view.myId;

    // Everything that is not mine and not a resource is an enemy.
    std::vector<GpuEnemy> enemies;
    for (const Entity& e : view.entities) {
        if (e.playerId == nullptr || *e.playerId == myId) {
            continue;
        }
        const EntityProperties& p = view.entityProperties.at(e.entityType);
        int reach = p.attack != nullptr ? p.attack->attackRange + (p.canMove ? 1 : 0) : 0;
        enemies.push_back({e.position.x, e.position.y, p.size, reach});
    }
    std::vector<int> dist, danger;
    double us = gpuFields(enemies, dist, danger);
    if (us >= 0) {
        gpuTotalUs += us;
        ++gpuCalls;
    }
    if (view.currentTick % 100 == 0) {
        fprintf(stderr, "[gpu] tick %d: %zu enemies, fields in %.0f us (%s), average %.0f us over %d launches\n",
                view.currentTick, enemies.size(), us, us >= 0 ? "GPU" : "CPU fallback",
                gpuCalls ? gpuTotalUs / gpuCalls : 0.0, gpuCalls);
    }

    int populationProvided = 0, populationUsed = 0;
    for (const Entity& e : view.entities) {
        if (e.playerId != nullptr && *e.playerId == myId) {
            const EntityProperties& p = view.entityProperties.at(e.entityType);
            populationUsed += p.populationUse;
            if (e.active) {
                populationProvided += p.populationProvide;
            }
        }
    }

    Action result = Action(std::unordered_map<int, EntityAction>());
    for (const Entity& e : view.entities) {
        if (e.playerId == nullptr || *e.playerId != myId) {
            continue;
        }
        const EntityProperties& p = view.entityProperties.at(e.entityType);
        std::shared_ptr<MoveAction> move;
        std::shared_ptr<BuildAction> build;
        std::shared_ptr<AttackAction> attack;
        const int here = cellIndex(e.position, n);

        if (e.entityType == BUILDER_UNIT) {
            // Gather; step away when an enemy can hit this cell.
            attack = std::make_shared<AttackAction>(nullptr, std::make_shared<AutoAttack>(2 * p.sightRange, std::vector<EntityType>{RESOURCE}));
            if (danger[here] > 0) {
                move = std::make_shared<MoveAction>(descend(e.position, danger, n), true, false);
            }
        } else if (p.canMove) {
            // Melee / ranged: retreat if the cell is too hot for the unit's health, else close in.
            attack = std::make_shared<AttackAction>(nullptr, std::make_shared<AutoAttack>(p.sightRange, std::vector<EntityType>{}));
            const int damage = p.attack != nullptr ? p.attack->damage : 5;
            const bool threatened = danger[here] * damage >= e.health;
            if (threatened) {
                move = std::make_shared<MoveAction>(descend(e.position, danger, n), true, false);
            } else if (dist[here] != INT_MAX) {
                move = std::make_shared<MoveAction>(descend(e.position, dist, n), true, false);
            } else {
                move = std::make_shared<MoveAction>(Vec2Int(n - 1, n - 1), true, true); // explore
            }
        } else if (p.build != nullptr) {
            EntityType type = p.build->options[0];
            if (populationUsed + view.entityProperties.at(type).populationUse <= populationProvided) {
                build = std::make_shared<BuildAction>(type, Vec2Int(e.position.x + p.size, e.position.y + p.size - 1));
                populationUsed += view.entityProperties.at(type).populationUse;
            }
        } else if (p.attack != nullptr) {
            attack = std::make_shared<AttackAction>(nullptr, std::make_shared<AutoAttack>(p.sightRange, std::vector<EntityType>{}));
        }
        result.entityActions[e.id] = EntityAction(move, build, attack, nullptr);
    }
    return result;
}

void MyStrategy::debugUpdate(const PlayerView& playerView, DebugInterface& debugInterface)
{
    debugInterface.send(DebugCommand::Clear());
    debugInterface.getState();
}
