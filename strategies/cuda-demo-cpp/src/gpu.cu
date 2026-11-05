// The CUDA part of the demo strategy. Compiled by nvcc (see CMakeLists.txt); the rest of the
// strategy only sees gpu.hpp.
#include "gpu.hpp"

#include <chrono>
#include <climits>
#include <cstdio>
#include <cuda_runtime.h>

namespace {

int g_mapSize = 0;
int g_capacity = 0;
bool g_ready = false;
GpuEnemy* d_enemies = nullptr;
int* d_dist = nullptr;
int* d_danger = nullptr;

__host__ __device__ inline int cellToSquare(int cx, int cy, const GpuEnemy& e)
{
    // Manhattan distance from a cell to the nearest cell of the enemy's size x size square.
    int dx = cx < e.x ? e.x - cx : (cx >= e.x + e.size ? cx - (e.x + e.size - 1) : 0);
    int dy = cy < e.y ? e.y - cy : (cy >= e.y + e.size ? cy - (e.y + e.size - 1) : 0);
    return dx + dy;
}

__global__ void fieldsKernel(const GpuEnemy* enemies, int n, int mapSize, int* dist, int* danger)
{
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= mapSize * mapSize) {
        return;
    }
    int cx = idx % mapSize;
    int cy = idx / mapSize;
    int best = INT_MAX;
    int hits = 0;
    for (int i = 0; i < n; ++i) {
        int d = cellToSquare(cx, cy, enemies[i]);
        best = min(best, d);
        if (enemies[i].reach > 0 && d <= enemies[i].reach) {
            ++hits;
        }
    }
    dist[idx] = best;
    danger[idx] = hits;
}

void cpuFields(const std::vector<GpuEnemy>& enemies, int mapSize, std::vector<int>& dist, std::vector<int>& danger)
{
    for (int idx = 0; idx < mapSize * mapSize; ++idx) {
        int cx = idx % mapSize, cy = idx / mapSize, best = INT_MAX, hits = 0;
        for (const GpuEnemy& e : enemies) {
            int d = cellToSquare(cx, cy, e);
            best = d < best ? d : best;
            if (e.reach > 0 && d <= e.reach) {
                ++hits;
            }
        }
        dist[idx] = best;
        danger[idx] = hits;
    }
}

bool check(cudaError_t err, const char* what)
{
    if (err == cudaSuccess) {
        return true;
    }
    fprintf(stderr, "[gpu] %s failed: %s\n", what, cudaGetErrorString(err));
    g_ready = false;
    return false;
}

bool reserve(int n)
{
    if (n <= g_capacity) {
        return true;
    }
    int capacity = g_capacity ? g_capacity : 256;
    while (capacity < n) {
        capacity *= 2;
    }
    if (d_enemies != nullptr) {
        cudaFree(d_enemies);
    }
    if (!check(cudaMalloc(&d_enemies, capacity * sizeof(GpuEnemy)), "cudaMalloc(enemies)")) {
        return false;
    }
    g_capacity = capacity;
    return true;
}

} // namespace

bool gpuInit(int mapSize, std::string& info)
{
    int count = 0;
    cudaError_t err = cudaGetDeviceCount(&count);
    if (err != cudaSuccess || count == 0) {
        info = std::string("no CUDA device: ") + (err == cudaSuccess ? "count is 0" : cudaGetErrorString(err));
        g_ready = false;
        return false;
    }
    cudaDeviceProp prop;
    cudaGetDeviceProperties(&prop, 0);
    int runtime = 0, driver = 0;
    cudaRuntimeGetVersion(&runtime);
    cudaDriverGetVersion(&driver);
    char buf[512];
    snprintf(buf, sizeof buf, "%s (sm_%d%d, %d SMs), CUDA runtime %d.%d, driver %d.%d", prop.name, prop.major,
             prop.minor, prop.multiProcessorCount, runtime / 1000, runtime % 1000 / 10, driver / 1000, driver % 1000 / 10);
    info = buf;
    if (g_mapSize != mapSize) {
        if (d_dist != nullptr) {
            cudaFree(d_dist);
            cudaFree(d_danger);
        }
        if (!check(cudaMalloc(&d_dist, mapSize * mapSize * sizeof(int)), "cudaMalloc(dist)")
            || !check(cudaMalloc(&d_danger, mapSize * mapSize * sizeof(int)), "cudaMalloc(danger)")) {
            return false;
        }
        g_mapSize = mapSize;
    }
    if (!reserve(256)) {
        return false;
    }
    g_ready = true;
    // Warm up: the first launch pays for module loading, keep that out of the first tick.
    std::vector<GpuEnemy> none;
    std::vector<int> a, b;
    gpuFields(none, a, b);
    return g_ready;
}

double gpuFields(const std::vector<GpuEnemy>& enemies, std::vector<int>& dist, std::vector<int>& danger)
{
    const int cells = g_mapSize * g_mapSize;
    dist.assign(cells, INT_MAX);
    danger.assign(cells, 0);
    if (!g_ready || !reserve((int)enemies.size())) {
        cpuFields(enemies, g_mapSize, dist, danger);
        return -1;
    }
    auto t0 = std::chrono::steady_clock::now();
    const int n = (int)enemies.size();
    if (n > 0 && !check(cudaMemcpy(d_enemies, enemies.data(), n * sizeof(GpuEnemy), cudaMemcpyHostToDevice), "cudaMemcpy H2D")) {
        cpuFields(enemies, g_mapSize, dist, danger);
        return -1;
    }
    const int block = 256;
    fieldsKernel<<<(cells + block - 1) / block, block>>>(d_enemies, n, g_mapSize, d_dist, d_danger);
    if (!check(cudaGetLastError(), "kernel launch")
        || !check(cudaMemcpy(dist.data(), d_dist, cells * sizeof(int), cudaMemcpyDeviceToHost), "cudaMemcpy D2H")
        || !check(cudaMemcpy(danger.data(), d_danger, cells * sizeof(int), cudaMemcpyDeviceToHost), "cudaMemcpy D2H")) {
        cpuFields(enemies, g_mapSize, dist, danger);
        return -1;
    }
    return std::chrono::duration<double, std::micro>(std::chrono::steady_clock::now() - t0).count();
}
