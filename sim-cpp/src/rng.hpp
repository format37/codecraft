// CPython's random.Random, exactly (Modules/_randommodule.c and Lib/random.py): MT19937 seeded by init_by_array with
// the 32-bit words of the seed, random() from 53 bits, getrandbits, _randbelow, shuffle and choice as Python does
// them. The Python engine (sim/codecraft_sim) draws from random.Random(seed); this one draws the same numbers in the
// same order, so the two engines play the same game from the same seed.
#pragma once
#include <cstddef>
#include <cstdint>
#include <utility>
#include <vector>

namespace cc {

class PyRandom {
public:
    explicit PyRandom(uint64_t seed) {
        std::vector<uint32_t> key;
        do { key.push_back(static_cast<uint32_t>(seed & 0xffffffffu)); seed >>= 32; } while (seed);
        init_by_array(key);
    }
    uint32_t genrand_uint32() {
        static const uint32_t mag01[2] = {0x0u, 0x9908b0dfu};
        uint32_t y;
        if (mti >= N) {
            int kk;
            for (kk = 0; kk < N - M; kk++) {
                y = (mt[kk] & UPPER) | (mt[kk + 1] & LOWER);
                mt[kk] = mt[kk + M] ^ (y >> 1) ^ mag01[y & 0x1u];
            }
            for (; kk < N - 1; kk++) {
                y = (mt[kk] & UPPER) | (mt[kk + 1] & LOWER);
                mt[kk] = mt[kk + (M - N)] ^ (y >> 1) ^ mag01[y & 0x1u];
            }
            y = (mt[N - 1] & UPPER) | (mt[0] & LOWER);
            mt[N - 1] = mt[M - 1] ^ (y >> 1) ^ mag01[y & 0x1u];
            mti = 0;
        }
        y = mt[mti++];
        y ^= (y >> 11);
        y ^= (y << 7) & 0x9d2c5680u;
        y ^= (y << 15) & 0xefc60000u;
        y ^= (y >> 18);
        return y;
    }
    // random.random(): 53 bits
    double random() {
        uint32_t a = genrand_uint32() >> 5, b = genrand_uint32() >> 6;
        return (a * 67108864.0 + b) * (1.0 / 9007199254740992.0);
    }
    // random.getrandbits(k), 0 < k <= 32
    uint32_t getrandbits(int k) { return genrand_uint32() >> (32 - k); }
    // CPython's _randbelow_with_getrandbits(n), n > 0
    uint32_t randbelow(uint32_t n) {
        int k = 0;
        for (uint32_t v = n; v; v >>= 1) k++;
        uint32_t r = getrandbits(k);
        while (r >= n) r = getrandbits(k);
        return r;
    }
    template <class T> void shuffle(std::vector<T>& x) {
        for (std::size_t i = x.size(); i-- > 1;) {
            std::size_t j = randbelow(static_cast<uint32_t>(i + 1));
            std::swap(x[i], x[j]);
        }
    }
    template <class T>
    const T& choice(const std::vector<T>& x) {
        return x[randbelow(static_cast<uint32_t>(x.size()))];
    }

private:
    static const int N = 624, M = 397;
    static const uint32_t UPPER = 0x80000000u, LOWER = 0x7fffffffu;
    uint32_t mt[N];
    int mti = N + 1;
    void init_genrand(uint32_t s) {
        mt[0] = s;
        for (mti = 1; mti < N; mti++) mt[mti] = 1812433253u * (mt[mti - 1] ^ (mt[mti - 1] >> 30)) + mti;
    }
    void init_by_array(const std::vector<uint32_t>& key) {
        init_genrand(19650218u);
        int i = 1, j = 0;
        int len = static_cast<int>(key.size());
        for (int k = (N > len ? N : len); k; k--) {
            mt[i] = (mt[i] ^ ((mt[i - 1] ^ (mt[i - 1] >> 30)) * 1664525u)) + key[j] + j;
            i++; j++;
            if (i >= N) { mt[0] = mt[N - 1]; i = 1; }
            if (j >= len) j = 0;
        }
        for (int k = N - 1; k; k--) {
            mt[i] = (mt[i] ^ ((mt[i - 1] ^ (mt[i - 1] >> 30)) * 1566083941u)) - i;
            i++;
            if (i >= N) { mt[0] = mt[N - 1]; i = 1; }
        }
        mt[0] = 0x80000000u;
        mti = N;
    }
};

}  // namespace cc
