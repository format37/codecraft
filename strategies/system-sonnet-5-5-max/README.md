# system-sonnet-5-5-max

A system seed written from scratch by Claude Sonnet 5.5 (max effort), to compete with the players' strategies on the
ladder. One C++ file, `src/MyStrategy.cpp`, dropped into the official C++ client by its `compile.sh` (a one-file source
replaces `MyStrategy.cpp`; keep `src/` to that single file, a second file would make `compile.sh` wipe the client).

It was developed and tested against the engine's own games only (local batch games and the built-in QuickStart bot,
the old 2020 entries as sparring partners), never against the players' strategies, and without reading any other
strategy's source.

## How it plays

**Economy first.** Builders mine, houses are built just in time (a house is started when the free population runs out,
more of them in parallel when the money piles up), builders are bought every tick the money allows, up to 80. Builders
spread over distinct resource cells (a cell shared by four builders dies fast and sends all of them walking), avoid
standing in narrow passages (a builder parked in a corridor cuts off every slot behind it) and run from armed enemies.

**Layout.** Where a building may stand matters as much as when: the soldiers' gathering place, the streets from every
base to it and the lanes toward the enemy corners are kept free; a house never leaves a one- or two-cell channel that
two idle units could plug, and a site is accepted only if the free ground and the mining slots stay connected.
Later houses stand in rows along the map's border, out of the way.

**Army.** A ranged base is started once enough builders work (earlier when armed enemies show up), a melee base
after it and a second ranged one when money is idle (Round 1 has them from the start). The number of ranged units
follows a target curve over time, behind it soldiers are bought before builders; melee units, who are tough but have
to reach their target, join in a fixed share. All shooters, turrets included, fire with focus: every tick the shots
are assigned so that targets die with as few shots as possible, killable targets first, by value per shot plus a
threat bonus. Turrets (cheap, no population, repaired by the builders around) stand between the gathering place and
the enemy. Builders also repair damaged buildings, but never while armed enemies are close to them.

**Waves.** Once enough soldiers stand ready, the army marches as a ball around an anchor that stays a few cells ahead
of the group's rear (it never outruns itself), regroups before contact, advances on an enemy it outnumbers, holds
against an even one, and falls back only before contact (running with the enemy in range only gets the rear shot).
Reinforcements join the ball as they arrive. At home the army meets threats it can match and waits for the rest at the
gathering place, next to the turrets.

**How it fares (local games, 2026-11-09).** It wins every format against the QuickStart bot and, one on one, about
nine games in ten against the old format37 entry. The old dragoonxen and thermit entries beat it: their economy runs
about a quarter ahead from the first house on and their armies are out by tick 300, so in four-player games with
them it is usually eliminated between ticks 300 and 500 and places third or fourth.

**Limits.** It keeps its own account of the CPU time it has used and switches to cheaper decisions if it ever gets
close to the server's 40 s; ordinary games take 5-10 s.

## Layout of the code

Everything is in `struct Brain` of `MyStrategy.cpp`: reading the view (with a memory of seen resources and enemy
buildings under fog), the breadth-first searches, site selection, the economy and the projects, the builders (a plan
for every move, resolved in rounds like the engine does), the focus fire and the army. Settings can be overridden by
`CC_*` environment variables, which exist only for local experiments (the server sets none).
