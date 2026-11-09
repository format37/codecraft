# system-fable-5-1-max

A system seed written by Claude Fable 5.1 (max effort) on 2026-11-09, from the rules and the engine source
alone: no user strategy and no other seed was read. One C++ file, `src/MyStrategy.cpp`, dropped into the
official client by its `compile.sh`. It is enabled and competes on the ladder like any strategy.

## Design

**Movement.** Every unit gets a one-cell move each tick: the engine steps straight into an adjacent cell, so
the server's A* (1000-node limit, random tie-breaks) never runs for my units. Steps are planned on my own BFS
fields; a resolver grants cells by priority, refuses swaps (two units trading places deadlock in this engine)
and lets a unit that has been blocked for two ticks sidestep. Cells of units that stand still this tick are
walls for the planning of the others.

**Mining.** A builder owns one mining cell (a free cell next to a known resource) and keeps it while a
resource stands beside it; it names the resource as its attack target, finishing weak cells first and peeling
the surface rather than digging lanes. Builders without a cell search the nearest free one (BFS), and the
shortest walks are granted first, so a builder whose cell just ran out steps into the cell it mined instead
of trekking across the base while someone else takes its lane. Two adjacent miners avoid sharing one resource
cell. Danger (an enemy ranged unit within range+1) is a reason to leave a cell, never a ban on mining.

**Houses and bases.** Houses go on a pitch-5 lattice (2-wide corridors by construction), placed next to the
nearest builders, scored by the mining cells they eat and the buildings they touch, and checked for
connectivity (no free cell may be cut off) and reachability (a placer must be able to walk to the ring).
A site whose footprint turns out to hold a resource is cleared by a few dedicated builders first. The
ranged base's site is reserved as soon as a free 5x5 is known, so the houses leave it alone; it is started
when the mining income reaches 18 (or by tick 200). Planned footprints are walls for every path, and units
standing on one step off, so a build is never refused for a stray unit. Repairers each get their own ring
cell and their own path. The population margin grows with the income, several houses are built in parallel,
and nothing stands still with money in the bank: the pop cap is 400, extra ranged bases are built when
resources pile up (each is +500 score and another unit a tick).

**Army.** Shooting is a kill-first assignment: targets sorted by value (ranged, melee, turrets, builders,
bases, houses) and health, exactly enough shooters to kill each, the rest chip at the best target in range.
A unit steps toward an enemy only where my support within 5 of the step cell outweighs the enemy shooters
that cover it (`threatR1`, the cells enemy ranged units reach after one step); otherwise it holds the edge of
their range or steps out of it. Fresh units gather on open ground beyond the builders' centre and never idle
on a base's spawn ring (the first version lost a game by ringing its own ranged base). Modes: `HOLD` (gather),
`DEFEND` (enemy military within 12 of my buildings or builders: the army goes there, a small raid gets a small
detachment) and `ATTACK`: only when the army is 1.3x the enemy's estimated strength (what has been seen,
with a fading memory of its peak, and, against an enemy whose score matches mine, at least my own army, up to
55) and my score is not behind, or when it is big late in the game. The march follows a Dial's-algorithm
field in which a resource cell costs the six shots it takes a ranged unit to remove it: the army digs
straight through walls instead of threading the map's carved corridors. The target is what is in sight, then
remembered enemy buildings, then the enemy bases at the positions the map's 8-fold symmetry dictates (each
visited once), then the last enemy seen, then a sweep. Two turrets anchor the rally; more go up near threats.

**Fog.** Resources and enemy buildings are remembered; a cell never seen is inferred from the earliest-seen of
its eight images under the generator's symmetries (the map is exactly symmetric at tick 0). Enemy unit
sightings decay after 12 ticks.

**Knobs.** Every tunable reads `CC_<NAME>` from the environment (`knob()` in the source), so variants need no
recompiling; `CC_DEBUG=1..3` prints status lines, builder statistics, army decisions and ASCII maps to stderr.

## Results (local, sim-cpp engine, the harness of the session)

Alone on 8 Finals maps: ~10 builders at t100, ~30 at t200, ~47 + 22 ranged at t300, ~69 + 42 at t400,
80 (the cap) from ~t450; score ~6.2k at t300, ~11k at t400. Builders spend 55-65% of their time mining
(dragoonxen-2020: 75-95%); the rest is walking to the next cell or a construction site.

Against the other seeds and the 2020 entries (Finals, 4 seeds, both seat orders):

| opponent | result | scores (mine / theirs) |
|---|---|---|
| system-sonnet-5-5-max | 7-8 of 8 wins | ~150k / ~95k |
| system-opus-5-5-max | 0 of 8 | ~95k / ~170k, rarely eliminated |
| dragoonxen-2020 | 0 of 8 | eliminated around t500-700 |
| thermit-2020 | 0 of 8 | eliminated around t550-650 |

Four-player games with system-opus-5-5-max, system-sonnet-5-5-max and QuickStart: second place in all of
Round 1 and Round 2 (behind opus, ahead of sonnet and QuickStart), ~100-120k.

CPU: 1.5-3.7 s per 1000-tick game natively (the server allows 40 s).

## What would move it next

The economy, first of all: the 2020 entries keep 80 builders mining 85% of the time by tick 350; this one
reaches 80 by t450 at ~60%. Then the fights: trades are roughly even, so the side with the bigger army wins,
and the attack trigger errs on the side of waiting. Scouting (one unit to see the enemy's army) would make
the estimate honest; melee units are unused (`CC_MELEE_SHARE`).
