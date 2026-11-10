# system-fable-5-2-xhigh

A system seed written by Claude Fable 5.2 (xhigh effort) on 2026-11-10, from the rules, the engine source
and the notes the sessions that wrote the earlier seeds left behind (their lessons and measurements). No
strategy's source code was read, a user's or another seed's; the other seeds and the two 2020 entries kept in
this repository were opponents (their images), nothing more. One C++ file, `src/MyStrategy.cpp`, dropped into
the official client by its `compile.sh`. It is enabled and competes on the ladder like any strategy.

## Design

A tick is: parse, fog memory, threat maps, fire, economy, army, production, moves, orders. Every unit gets a
fresh order every tick and every move is one cell, so the engine's own path search never runs for my units.

**Moves.** Each unit ranks the cell it stands on and its four neighbours by its own field; a priority
inheritance resolver (the PIBT idea) then gives every unit one cell. A unit may take the cell of another that
has not decided yet, which is then asked to move on with the asker's priority; chains are fine, swaps and
rings are refused because the engine cannot execute them, and a unit that cannot give way stays for good, so
the search is linear. Units that work this tick (shoot, mine, repair, build) are walls for the others, with
one exception: a builder at work that plugs a corridor one cell wide gives way for a few ticks when a unit
with a job, or the army, has waited behind it, and the next one down the corridor after it. A unit that
waited gains priority tick by tick.

**Fire.** What I see is exactly what the engine shoots at, and shots land together. So every target gets
just the shots that kill it (ranged first, then melee, turrets, builders, bases, houses; the scarce targets
first, the shooters with the fewest other options first), and the guns left over add damage where a kill is
nearest. No shot is spent on a target that is already dead.

**Mining.** A builder that stands beside a resource mines it. The others are matched to the open cells
beside resources by an auction: a labelled breadth-first search from all open cells, the nearest builder of
each cell wins it, the taken cells leave, and the search runs again for the rest. Paths never lead through
the enemy's fire.

**Houses and bases.** Population is filled first; the house comes when the base is full, three builders on
it, and the money mined while it rises pays the next five builders at once. When resources are in the bank
the houses are planned for a unit a tick. A site must leave one free arc on the ring around it (a building
there cuts nothing apart), is chosen by the builders' walking distance, not by the straight line, and is
dropped within a few ticks if nobody can come. A planned site is a wall for every path and whoever stands on
it leaves, so a build is not refused for a stray unit. The ranged base's 5x5 is kept out of the houses'
hands from early on: free land when there is some, else (from tick 80) the square near the base with the
least to mine away, which the miners clear first once the base is due. It is built at fifty builders. A road
from the bases to the army's guard ground stays free of houses.

**Builders and danger.** A builder leaves only the cells an enemy can shoot at after one step and comes back
when they are clear; its way to a mining cell never crosses the enemy's fire. Wider zones were tried twice
(flight within eight cells of a fighter; no return within nine) and both cost more than they saved: income
the first time, games the second. Lost builders are rebuilt before more soldiers are made: in a long game the
bigger workforce is the bigger army.

**Battles.** For every group of enemy fighters near my units a few ticks of the fight are simulated on the
real grid, walls and all: I push and they stand, I push and they come, I stand and they come. The worse of
the two pushes decides whether to attack; if standing is not bad the army holds, else it falls back. Holding
units take places of their own on the edge of the enemy's fire, a step from a shot, by the same auction as
the miners: the army stands wide, not deep, and a column walking into it pays. At home the army fights for
less.

**Army.** Until the first army stands (16 ranged units, 12 with four players) it gathers on a guard ground a
walk out from the bases, towards the place the enemy's fighters were last seen (or towards its base).
Intruders are met by as many units as it takes, by everybody when there is nothing else to do; reinforcements
follow the army in groups of six. On the attack the army walks as one body (who is
ahead of its middle waits and is pushed on), follows a field in which a resource costs the shots it takes to
remove, so it digs where that is shorter, and takes what it knows of the target: remembered buildings, units
seen lately, the base where the map's symmetry puts it, then the places not seen for long. In four-player
games the target is the nearest opponent that is weak by what was seen and by its score.

**Fog.** Resources and enemy buildings are remembered; a cell never seen is inferred from the first seen of
its eight images under the map generator's symmetries. Enemy fighters seen a moment ago still count in a
battle.

**Knobs.** Every tunable reads `CC_<NAME>` from the environment (`KN()` in the source), so variants need no
recompiling; `CC_DEBUG=1..3` prints a status line per tick, the builders' time shares, battle decisions,
plans and ASCII maps to stderr. Should the clock ever run away (a game needs about 2.2 s of the 40 s it is
allowed), the battle simulations are dropped rather than the game.

## Results (local, sim-cpp engine, both seat orders)

Alone on eight Finals maps it has 10 builders at tick 74, 20 at 128, 40 at 196, 60 at 271 (dragoonxen-2020:
69, 114, 169, 236) and a score of about 7.1k at tick 300, the ranged base standing around tick 260. A game
costs about 2.2 s of CPU (2.7 s at most); four copies playing each other under a 0.1 s limit per tick lost
no tick. Address and undefined-behaviour sanitizers found nothing in Finals, Round 1 and Round 2 games.

Finals, seeds 1-16 (1-8 against the weaker two), both seat orders:

| opponent | wins | mean score (mine / theirs) |
|---|---|---|
| system-opus-5-5-max | 27 of 32 | 195k / 143k |
| system-sonnet-5-5-max | 16 of 16 | 127k / 49k |
| system-fable-5-1-max | 16 of 16 | 166k / 69k |
| dragoonxen-2020 | 0 of 16 | 57k / 169k |
| thermit-2020 | 0 of 8 | 74k / 198k |

Four players, two seeds, all six seatings of the other three (who sits opposite matters more than anything
else: a test that only rotates the seats gives one player the weak neighbour every time):

| format | line-up | mine | opus | the others |
|---|---|---|---|---|
| Round 1 | opus, fable-5-1, QuickStart | place 1.08, first in 11 of 12 | 2.25 | 2.67, 4.00 |
| Round 1 | opus, sonnet, fable-5-1 | place 1.08, first in 11 of 12 | 2.25 | 4.00, 2.67 |
| Round 2 | opus, fable-5-1, QuickStart | place 1.75, first in 6 of 12 | 1.92 | 2.33, 4.00 |
| Round 2 | opus, sonnet, fable-5-1 | place 1.33, first in 8 of 12 | 2.08 | 3.83, 2.75 |

What the measurements decided, each against the series above or the solo benchmark: the simulator against a
ratio of forces (10 of 16 against 6 of 16 at the time), the line against a crowd (13 against 11), rebuilding
lost builders (14 against 10), no wider flight zone for builders (27 of 32 without it, 22 with), three
builders a house and population first (ten builders at tick 74 instead of 84).
Two bugs cost whole games and showed only in long series: a builder that could not leave a planned site
because my own waiting builders stood on every exit, so the ranged base was never placed (two games of 32
ended at 10k against 60k); and a row of miners plugging the only corridor to a building site.

## What would move it next

The two 2020 entries still win every game. Their builders reach 60 about 35 ticks earlier, they keep 80
builders and a bank, and once their army stands at my mining fields my income falls (from 45 a tick to under
20) while theirs does not. In the fights themselves the kills are close to even. So: the growth between 20
and 50 builders, a raid on the enemy's builders (mine kill almost none, theirs kill dozens), turrets at the
guard ground (the planner written for them took too many builders off the fields at the wrong moment and was
removed), and melee units in Round 1, where their base is given.
