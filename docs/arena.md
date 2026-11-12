<!-- The server renders this with the numbers of config.yaml ({{ ... }}): https://codecraft.day/rules -->
# The arena

CodeCraft 2020 was a championship: rounds on fixed dates, a Sandbox that closed, a final table. This server keeps
the game and drops the calendar. Nothing opens or closes here, nobody is eliminated and there is no deadline: come
when you like, improve your strategy at your own pace, and the ladder tells you where it stands.

## The ladder

- Every enabled strategy with a successful build is on the ladder, with a rating that starts at {{ initial }}. The
  [leaderboard]({{ base }}/leaderboard) shows the ladder league by league: every strategy's place in its league, its
  rating and how far that can be trusted (±), its games, wins, average place, tournament points and crashes.
- Rated games are *sandbox games*, which the server schedules by itself. A strategy that is due a game starts one, in
  the format it has played least ({{ formats_text }}), against opponents drawn with a preference for ratings close
  to its own (weight exp(-|difference| / {{ sigma }})) and for strategies that are due a game themselves. The
  engine's QuickStart bot is a regular rated player and fills a seat when no strategy is free.
{% if per_build %}- When a new version of a strategy is built, it plays {{ per_build }} *placement games* one after another. Its
  games of two each go to the strategy it has met least so far, the closest rating first: on a small ladder a new
  version meets everybody one on one.
{% endif %}{% if interval %}- After {% if per_build %}that{% else %}its first game{% endif %} a strategy plays one rated game {{ interval_text }} for as long as it is enabled{% if interval_steps %}, and less
  often as its version ages: with every full week the version has been playing, the interval grows
  ({{ interval_steps }} hours, then {{ interval_cap }}), as in the 2020 Sandbox{% endif %}. A game played in another strategy's
  seat counts too, so in an hour everybody plays about once.
{% endif %}- A paused strategy plays nothing and keeps its rating. A player may have several strategies, each with a rating
  of its own.
- Custom games (any format, any opponents: your own older versions, other players' strategies, QuickStart, empty
  seats) are not rated: {{ custom_per_day }} a day, {{ custom_queued }} waiting at a time.
- Games are played and strategies built on machines apart from this site, which may be off for a while
  (maintenance, a power cut; a banner says so). Nothing is lost then: builds and games wait in the queue, and a game
  that was running is played again from the start (a sandbox game is dropped, unrated).

## Rating

A strategy does not change between two versions, so its games are not a history to follow but a sample to explain.
The ratings are the numbers that explain every rated game best: all games at once, each at full weight, whatever the
order they were played in. Chess engine rating lists are computed this way; the scale is Elo's.

- After a rated game every pair of its players is a duel: won by the better place, shared on a tie. What a difference
  of ratings expects of a duel is as in chess: 400 points expect 10 to 1. In a game of four a duel weighs
  {{ pair4 }} of a game of two, because six duels read off one order of finish are not six independent results.
- What is rated is a version of a strategy (two versions built from the same sources are one). The first version
  starts from {{ initial }}. A later version starts from the rating of the version before it, and its own games move
  it from there: a few games in, a real improvement shows in full, and so does a slip. The engine's QuickStart bot is
  a new version with every new version of the engine.
- After every rated game all ratings are fitted again, so yours can move a little without a game of yours, when the
  ratings of your opponents do. The scale has one fixed point: the first versions of all strategies average
  {{ initial }}.
- The ± next to a rating is one standard error: how far the rating may be off, given the games so far. It shrinks
  with every game. Until a version has played {{ settled_games }} rated games it is marked *placing*.

Tournament points are counted as in 2020 ({{ points_r }} by place in Round 1 and Round 2, {{ points_f }} in the
Finals format) and shown next to the rating.

A strategy that crashes (its process ends, it breaks the protocol, or it exceeds a limit) gives no more orders in that
game; the game goes on for the others and is rated as played. Crashes are counted on the leaderboard.

## Leagues

Players range from hand-written bots to strategies written with frontier models, and all are welcome. So that each
level has a race of its own, the ladder is split into leagues by strength, not by what a player says they use.

- There are {{ leagues }} leagues, league 0 the strongest. Every Monday (UTC) the settled ratings are clustered into
  {{ leagues }} groups, the split that leaves the least spread inside the groups, and the line between two
  neighbouring leagues is drawn halfway between their average ratings. The lines stay for the week, and the
  leaderboard shows them.
- A rating is settled when its version has played {{ settled_games }} rated games. A new strategy has no league
  until then. A new version of a strategy keeps the league while it plays its placement games.
- Promotion and relegation follow the rating, without qualifying series: as soon as a settled rating is {{ margin }}
  points past a line, the strategy changes league. The margin keeps a rating that sits on a line from going to and
  fro.
- A league is a group on the leaderboard with places of its own. The rating scale is one for all leagues and
  matchmaking knows no lines: most of your games are within your league because the ratings there are the closest
  to yours, and the games across a line keep the leagues on one scale.
- On a small ladder the leagues are small, and one strategy that moves a long way can pull a line with it at the
  next clustering. That evens out as the ladder grows.
- Tournaments in the 2020 formats, league by league, are this server's next step; a player will be seated there by
  their best strategy. Custom games know no leagues: play whoever you like.

## The engine

The game is played on our own open-source engine, with the 2020 rules, protocol, maps and replays, and one
deliberate change, in how units find their path under fog of war (the path of a move order, and that of an
auto-attack heading for an enemy):

- **2020:** a unit's path search took every cell its player did not see at that moment as free, whatever stood there.
  A unit sent far away along a forest or a wall longer than its sight could walk to and fro forever: each step moved
  the edge of its sight, the cells it uncovered or lost changed the shortest path, and the path flipped from one end
  of the obstacle to the other.
- **Here:** the search also remembers the resources and buildings its player has already seen (not units, which
  move), where they stood and with the health they had then. A remembered one is forgotten when a cell it stood on
  comes into sight again and it is gone. Cells never seen still count as free. So a unit learns an obstacle as it
  walks along it and goes round it (or through it, with `break_through`, when that is shorter: digging a resource
  costs its health divided by the damage, 30 steps for a builder).

The searches use nothing the player has not been shown, and everything else (`break_through`, the random choice
among equally short paths, the 1000 cells a search may visit, what an auto-attack may target) is as in 2020. The local runner
plays these rules by default; `--rules 2020` plays the 2020 engine's rules exactly. Strong strategies usually plan
their own paths and give one-cell moves; the change matters to the built-in long moves.

## Limits

A strategy has {{ limits_text }}; a game lasts up to 1000 ticks on an 80x80 map. Past a limit the strategy is cut
off and counts as crashed. A strategy is built from the official client's image of its language, without network
access: what the language's client does not ship is not there. Submissions: {{ versions_per_day }} versions a day per
player, one building at a time.

## What is yours

Your sources, build logs and your strategy's own output (stderr) are seen by you alone, and by the server's admins.
Everyone sees every game: its replay, the seat table, the scores. The server keeps your sources only to build them.
