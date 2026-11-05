#ifndef _MY_STRATEGY_HPP_
#define _MY_STRATEGY_HPP_

#include "DebugInterface.hpp"
#include "model/Model.hpp"

class MyStrategy {
public:
    MyStrategy();
    Action getAction(const PlayerView& playerView, DebugInterface* debugInterface);
    void debugUpdate(const PlayerView& playerView, DebugInterface& debugInterface);

private:
    bool gpuReady = false;
    int gpuMapSize = 0;
    double gpuTotalUs = 0;
    int gpuCalls = 0;
};

#endif
