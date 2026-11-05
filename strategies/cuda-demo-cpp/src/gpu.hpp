#ifndef _GPU_HPP_
#define _GPU_HPP_

#include <string>
#include <vector>

// An enemy entity as the kernel sees it: its square (min corner + size) and how far it can hit
// next tick (attack range, +1 if it can move; 0 if it cannot attack).
struct GpuEnemy {
    int x, y, size, reach;
};

// Creates the CUDA context and the device buffers for a mapSize x mapSize field. Safe to call
// again (re-allocates when the size changes). Returns false when no usable GPU is present;
// `info` always receives a description of what was found.
bool gpuInit(int mapSize, std::string& info);

// For every cell: Manhattan distance to the nearest enemy square (INT_MAX when there is none) and
// the number of enemies able to hit it next tick. Returns the kernel+copy time in microseconds,
// or -1 when it had to fall back to the CPU implementation.
double gpuFields(const std::vector<GpuEnemy>& enemies, std::vector<int>& dist, std::vector<int>& danger);

#endif
