# CodeCraft 2020 tournament formats

A summary of the stage descriptions from the 2020 contest's site; `formats`, `points` and `matchmaking` in
`config.yaml` follow it.

## Sandbox

Runs for the whole championship. A participant's latest accepted strategy plays; opponents are drawn at random,
but the closer two ratings are, the likelier the two strategies meet in a game. Each strategy plays about one
qualifying game an hour. The rating is a modified Elo: every participant's expected place is computed; a place
above it raises the rating, one below it lowers it. The initial rating is 1200. Sandbox games follow the rules of
a random stage among those passed and the current one.

## Round 1 (12-13 December 2020)

Format 4x1, no fog of war, bases of every type given at the start. Games are played in waves: in a wave every
strategy plays exactly one game, the line-up of each game drawn at random. Points for a game:

| place | points |
|-------|--------|
| 1 | 8 |
| 2 | 4 |
| 3 | 2 |
| 4 | 1 |

## Round 2 (19-20 December 2020)

Format 4x1 with fog of war, only builders at the start. The same points as in Round 1.

## Finals (26-28 December 2020)

Games 2x1 (one on one), everyone against everyone. Points: 2 for the first place, 0 for the second. On equal
points, the one who sent the strategy earlier is ahead.

## Strategy limits (from the official documentation)

- 1 second of real time to answer a tick;
- 40 seconds of CPU time for the whole game;
- 256 MB of memory;
- a game lasts up to 1000 ticks, on an 80x80 map.
