"""Demo strategy: a hand-written CUDA C kernel, compiled at start-up by CuPy (NVRTC) and launched
every tick. It computes, for every map cell, the Manhattan distance to the nearest enemy and the
number of enemies that can hit the cell next tick; units walk down those fields. Falls back to
NumPy when no GPU is available (and says so in the log)."""
import sys
import time

import numpy as np

from model import *

KERNEL = r"""
extern "C" __global__
void fields(const int* ex, const int* ey, const int* es, const int* er, int n, int mapSize,
            int* dist, int* danger)
{
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= mapSize * mapSize) return;
    int cx = idx % mapSize, cy = idx / mapSize;
    int best = 0x7fffffff, hits = 0;
    for (int i = 0; i < n; ++i) {
        // Manhattan distance from the cell to the nearest cell of the enemy's size x size square
        int dx = cx < ex[i] ? ex[i] - cx : (cx >= ex[i] + es[i] ? cx - (ex[i] + es[i] - 1) : 0);
        int dy = cy < ey[i] ? ey[i] - cy : (cy >= ey[i] + es[i] ? cy - (ey[i] + es[i] - 1) : 0);
        int d = dx + dy;
        best = min(best, d);
        if (er[i] > 0 && d <= er[i]) ++hits;
    }
    dist[idx] = best;
    danger[idx] = hits;
}
"""

# Compiled at import time, i.e. before main.py connects to the engine: the first tick's clock must
# not pay for NVRTC and the CUDA context.
try:
    import cupy as cp

    _kernel = cp.RawKernel(KERNEL, "fields")
    _props = cp.cuda.runtime.getDeviceProperties(0)
    GPU_INFO = "%s (sm_%d%d, %d SMs), cupy %s, CUDA runtime %d" % (
        _props["name"].decode(), _props["major"], _props["minor"], _props["multiProcessorCount"],
        cp.__version__, cp.cuda.runtime.runtimeGetVersion())
    _z = cp.zeros(1, dtype=cp.int32)
    _kernel((1,), (1,), (_z, _z, _z, _z, np.int32(0), np.int32(1), cp.zeros(1, cp.int32), cp.zeros(1, cp.int32)))
    cp.cuda.Stream.null.synchronize()
    GPU = True
except Exception as exc:  # noqa: BLE001
    GPU = False
    GPU_INFO = "no usable GPU (%r)" % (exc,)
print("[gpu] %s: %s" % ("ready" if GPU else "unavailable, NumPy fallback", GPU_INFO), file=sys.stderr, flush=True)


def fields(ex, ey, es, er, map_size):
    """-> (dist, danger) as (map_size*map_size,) int32 arrays, and the time spent in microseconds."""
    cells = map_size * map_size
    t0 = time.perf_counter()
    if GPU:
        n = len(ex)
        dist = cp.empty(cells, dtype=cp.int32)
        danger = cp.empty(cells, dtype=cp.int32)
        args = (cp.asarray(ex), cp.asarray(ey), cp.asarray(es), cp.asarray(er), np.int32(n), np.int32(map_size), dist, danger)
        _kernel(((cells + 255) // 256,), (256,), args)
        out = (cp.asnumpy(dist), cp.asnumpy(danger))
    else:
        cx = np.arange(cells, dtype=np.int32) % map_size
        cy = np.arange(cells, dtype=np.int32) // map_size
        dist = np.full(cells, np.iinfo(np.int32).max, dtype=np.int32)
        danger = np.zeros(cells, dtype=np.int32)
        for x, y, s, r in zip(ex, ey, es, er):
            dx = np.where(cx < x, x - cx, np.where(cx >= x + s, cx - (x + s - 1), 0))
            dy = np.where(cy < y, y - cy, np.where(cy >= y + s, cy - (y + s - 1), 0))
            d = dx + dy
            dist = np.minimum(dist, d)
            if r > 0:
                danger += (d <= r)
        out = (dist, danger)
    return out[0], out[1], (time.perf_counter() - t0) * 1e6


def descend(pos, field, map_size):
    """The neighbouring cell (or the cell itself) with the smallest field value."""
    best, best_value = pos, field[pos.y * map_size + pos.x]
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        x, y = pos.x + dx, pos.y + dy
        if 0 <= x < map_size and 0 <= y < map_size and field[y * map_size + x] < best_value:
            best, best_value = Vec2Int(x, y), field[y * map_size + x]
    return best


class MyStrategy:
    def __init__(self):
        self.total_us = 0.0
        self.calls = 0

    def get_action(self, view, debug_interface):
        n = view.map_size
        my_id = view.my_id
        props = view.entity_properties
        ex, ey, es, er = [], [], [], []
        for e in view.entities:
            if e.player_id is None or e.player_id == my_id:
                continue
            p = props[e.entity_type]
            ex.append(e.position.x)
            ey.append(e.position.y)
            es.append(p.size)
            er.append(p.attack.attack_range + (1 if p.can_move else 0) if p.attack is not None else 0)
        to_arr = lambda a: np.asarray(a, dtype=np.int32)  # noqa: E731
        dist, danger, us = fields(to_arr(ex), to_arr(ey), to_arr(es), to_arr(er), n)
        self.total_us += us
        self.calls += 1
        if view.current_tick % 100 == 0:
            print("[gpu] tick %d: %d enemies, fields in %.0f us (%s), average %.0f us over %d launches" % (
                view.current_tick, len(ex), us, "GPU" if GPU else "NumPy fallback", self.total_us / self.calls, self.calls),
                file=sys.stderr, flush=True)

        provided = used = 0
        for e in view.entities:
            if e.player_id == my_id:
                p = props[e.entity_type]
                used += p.population_use
                if e.active:
                    provided += p.population_provide

        result = Action({})
        for e in view.entities:
            if e.player_id != my_id:
                continue
            p = props[e.entity_type]
            here = e.position.y * n + e.position.x
            move = build = attack = None
            if e.entity_type == EntityType.BUILDER_UNIT:
                attack = AttackAction(None, AutoAttack(2 * p.sight_range, [EntityType.RESOURCE]))
                if danger[here] > 0:
                    move = MoveAction(descend(e.position, danger, n), True, False)
            elif p.can_move:
                attack = AttackAction(None, AutoAttack(p.sight_range, []))
                damage = p.attack.damage if p.attack is not None else 5
                if danger[here] * damage >= e.health:
                    move = MoveAction(descend(e.position, danger, n), True, False)
                elif dist[here] != np.iinfo(np.int32).max:
                    move = MoveAction(descend(e.position, dist, n), True, False)
                else:
                    move = MoveAction(Vec2Int(n - 1, n - 1), True, True)
            elif p.build is not None:
                t = p.build.options[0]
                if used + props[t].population_use <= provided:
                    build = BuildAction(t, Vec2Int(e.position.x + p.size, e.position.y + p.size - 1))
                    used += props[t].population_use
            elif p.attack is not None:
                attack = AttackAction(None, AutoAttack(p.sight_range, []))
            result.entity_actions[e.id] = EntityAction(move, build, attack, None)
        return result

    def debug_update(self, player_view, debug_interface):
        debug_interface.send(DebugCommand.Clear())
        debug_interface.get_state()
