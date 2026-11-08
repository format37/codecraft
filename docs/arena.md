<!-- The server renders this with the numbers of config.yaml ({{ ... }}): https://codecraft.day/rules -->
# The arena

CodeCraft 2020 was a championship: rounds on fixed dates, a Sandbox that closed, a final table. This server keeps
the game and drops the calendar. Nothing opens or closes here, nobody is eliminated and there is no deadline: come
when you like, improve your strategy at your own pace, and the ladder tells you where it stands.

## The ladder

- Every enabled strategy with a successful build is on the ladder, with an Elo rating that starts at {{ initial }}.
  The [leaderboard]({{ base }}/leaderboard) orders them by rating and shows their games, wins, average place,
  tournament points and crashes.
- Rated games are *sandbox games*, which the server schedules by itself. For each one it draws a format
  ({{ formats_text }}), then the players: a strategy that is due a game, and opponents drawn with a preference for
  ratings close to its own (weight exp(-|difference| / {{ sigma }})). The engine's QuickStart bot is a regular rated
  player and fills a seat when no strategy is free.
{% if per_build %}- When a new version of a strategy is built, it plays {{ per_build }} rated games a few minutes
  apart.{% if daily %} After that every strategy plays {{ daily_text }} a day for as long as it is enabled, so the
  ladder keeps moving and the ratings settle.{% endif %} A paused strategy plays nothing and keeps its rating.
{% else %}- An enabled strategy plays rated games whenever the server has a free moment. A paused strategy plays
  nothing and keeps its rating.
{% endif %}- The rating belongs to the strategy, not to the version: a new version carries on from where the old one
  stood. A player may have several strategies, each with a rating of its own.
- Custom games (any format, any opponents: your own older versions, other players' strategies, QuickStart, empty
  seats) are not rated: {{ custom_per_day }} a day, {{ custom_queued }} waiting at a time.

## Rating

Multiplayer Elo. After a rated game every pair of players is scored as a duel: 1 to the better place, 0 to the worse,
1/2 for a tie; the expected score follows from the two ratings as in chess (400 points of difference expect 10 to 1).
A strategy's change is K times its average surplus over its opponents. K starts at {{ k }} and falls towards
{{ k_min }} with a half-life of {{ half_life }} games: an established rating moves more slowly than a fresh one.
Tournament points are counted as in 2020 ({{ points_r }} by place in Round 1 and Round 2, {{ points_f }} in the
Finals format) and shown next to the rating.

A strategy that crashes (its process ends, it breaks the protocol, or it exceeds a limit) gives no more orders in that
game; the game goes on for the others and is rated as played. Crashes are counted on the leaderboard.

## Leagues

Players range from hand-written bots to strategies written with frontier models, and all are welcome. So that each
level has a race of its own, the ladder is split into leagues by strength, not by what a player says they use.

- The ladder is one league until {{ min_players }} players have settled ratings ({{ settled_games }} rated games or
  more; a player's rating is that of their best strategy). Today the leaderboard is that one league.
- From then on the ratings are clustered once a week into the leagues that fit them best, league 0 the strongest,
  each with at least four players. Your best strategy's rating places you. The boundaries have a margin, so a rating
  sitting on a line does not change league every week, and a new strategy is unplaced until its rating has settled.
- Promotion and relegation follow the rating: cross the line above you and the next weekly placement moves you up;
  fall below the line under you and it moves you down. There are no qualifying series. Matchmaking already plays most
  of your games within your league, and the games it draws at the boundaries keep the leagues on one scale.
- Each league gets its own standings and, as this server's next step, its own tournaments in the 2020 formats.
  Custom games know no leagues: play whoever you like.

## Limits

A strategy has {{ limits_text }}; a game lasts up to 1000 ticks on an 80x80 map. Past a limit the strategy is cut
off and counts as crashed. A strategy is built from the official client's image of its language, without network
access: what the language's client does not ship is not there. Submissions: {{ versions_per_day }} versions a day per
player, one building at a time.

## What is yours

Your sources, build logs and your strategy's own output (stderr) are seen by you alone, and by the server's admins.
Everyone sees every game: its replay, the seat table, the scores. The server keeps your sources only to build them.
