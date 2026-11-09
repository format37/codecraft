# system-opus-5-5-max

A system seed for the CodeCraft 2020 ladder, written by Claude Opus 5.5 (max effort) from the rules and the
engine alone: one C++ file, `src/MyStrategy.cpp`, which the official C++ client's `compile.sh` drops in place of
its own `MyStrategy.cpp`. Every entity gets an explicit order every tick (an entity without one repeats its last),
and every move is a single step to a neighbouring cell planned by the strategy itself, so the engine's path search
is never used.

## The world

- The view is turned so that home is always the top-left corner. Under fog of war the strategy remembers resources
  and enemy buildings it has seen, and the enemy army units seen in the last 90 ticks.
- Danger maps: for every cell, the damage enemy ranged and melee units can deal there this tick and next tick (a
  unit may step once before it shoots), and enemy turrets.
- Articulation points of the free cells (Tarjan, from home): a cell whose occupation would cut free mining slots off
  from home. Miners never stand on one, so they do not wall each other out of a field.

## Economy

- Mining: a slot is a free cell beside a resource, out of danger and not a corridor cell as above. Builders keep
  the slot they walk to; the others take, within a distance band of the nearest free slot, the one with the most
  resource health around it shared by the miners already there, so they go to rich fields instead of hopping
  between crumbs. A builder stuck behind miners makes the miner nearest to the free slots move on into them.
- Houses: just in time and one at a time early, then ahead of production with several at once (more when money
  piles up while the population is nearly full). Sites are scored by the miners beside them (they repair it
  without walking), the distance to the base and the slots they would cover; a house never leaves a one-cell
  corridor, never encloses ground, may touch other houses and the bases as long as each base keeps room to spawn.
  A site nobody can reach, or a house not placed within 60 ticks, is dropped and avoided for a while.
- The ranged base (Round 2 and Finals): its site is reserved early, while there is open ground, and kept free of
  houses; money is saved for it from about 40 builders (Finals) or 50 (Round 2), earlier when enemies come. Under
  fog a scout looks around home while no site has been found.
- Production: builders up to about 70, ranged units when the army is below its wanted size (a growing share of
  the builders, more when threatened, everything after the builders), no new builders while some stand idle.

## Army

- Focus fire: every shooter in range (ranged units, melee units, turrets) is assigned a target so that each target
  gets the shots that kill it and no more; units killable this tick first, then by type (ranged, melee, turrets,
  builders, houses, bases) and health.
- Movement: a unit steps along a distance field to its objective, but steps into cells the enemy can hit next tick
  only when the friendly power around it is at least 1.25 times the enemy power near it (0.8 times when defending);
  otherwise it holds just outside their reach, spreading along that edge instead of queueing behind the first one,
  or backs off. A miner in the way steps aside.
- Objectives: enemy fighters and buildings in my zone are met by the nearest units with enough power for them, or
  by everybody for a big attack. The attack goes for the enemy's army and bases first, then turrets, then houses,
  never chasing builders; in Round 1 the target is the nearest weak player, under fog the known enemy buildings or
  the corners where enemies start. Under fog the army attacks only well above the largest enemy army seen; near an
  enemy army the leaders of a march wait until enough of the group has caught up. Otherwise the army waits on the
  way the enemy would come.

## Local experiments

`CC_DEBUG=1` (or 2, 3) prints more to stderr; `CC_<KNOB>=value` overrides a tuning default (the names are in the
calls to `knob()`). The server sets neither, so the defaults play there.
