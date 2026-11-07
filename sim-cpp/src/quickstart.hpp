// The built-in QuickStart bot, as sim/codecraft_sim/quickstart.py (the starter strategy of
// github.com/format37/codecraft-starter): the same orders from the same view, Python's tie-breaks included.
#pragma once
#include "model.hpp"
#include "trans.hpp"

namespace cc {

Action quickstart_action(const PlayerView& view);

}  // namespace cc
