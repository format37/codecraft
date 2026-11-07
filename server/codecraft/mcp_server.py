"""The MCP server for coding agents, mounted by `web.create_app` at /mcp/: streamable HTTP, stateless,
JSON responses, so every request carries the token and no session has to be kept.

One thin wrapper per tool of agents.py. The docstrings and signatures here are what an agent reads
(FastMCP turns them into the tool descriptions and the parameter schemas), so they say what a tool
returns, when to call it and what not to do; the work happens in `agents.call`. The token middleware
of web.py has already resolved the user (`request.state.user`) and taken the token out of the path.
A result goes out as one compact JSON text block (FastMCP's own rendering is indented, about twice
the tokens); text tools (rules, game_log) go out as plain text.

A new instance per app: the SDK's session manager runs once, inside the app's lifespan.
"""
from __future__ import annotations

import json
import warnings
from typing import Annotated
from urllib.parse import urlsplit

# The SDK's FastMCP settings carry a forward-referenced `lifespan` field that pydantic-settings warns about
# at every construction; nothing to act on here.
warnings.filterwarnings("ignore", message=r"Field 'lifespan' has an incomplete definition")

from mcp.server.fastmcp import Context, FastMCP  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from mcp.types import CallToolResult, TextContent, ToolAnnotations  # noqa: E402
from pydantic import BeforeValidator  # noqa: E402

from . import agents as A  # noqa: E402


def _seed_in(value):
    """play's seed has a string-only schema, so clients send it as a JSON string; a small number still
    passes, one above 2^53 (possibly rounded on the way) is refused with the reason."""
    try:
        return A.seed_arg(value)
    except A.AgentError as exc:
        raise ValueError(str(exc)) from None


Seed = Annotated[str | None, BeforeValidator(_seed_in)]

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)


def instructions(ax: A.Agents) -> str:
    cfg = ax.cfg
    sub = cfg["submissions"]
    per_build = int(cfg.get("worker", "sandbox_games_per_build", default=10) or 0)
    engine = ("the official engine" if cfg.engine_official
              else "our own open-source engine (the rules, protocol and replays of the official one)")
    return f"""\
CodeCraft is a self-hosted arena for CodeCraft 2020, the real-time strategy game of Russian AI Cup 2020.
A program ("strategy") commands builders, melee and ranged units, bases, houses and turrets on an 80x80
map for up to 1000 ticks; score comes from mining, building and destroying (each type's build_score and
destroy_score are in rules(section="Entity properties")). The server builds a strategy
from source in any of the 15 official client languages, plays it in Docker with {engine} and
keeps an Elo ladder. You act as the user who owns the token: their strategies, their games, their quotas.

The loop:
1. whoami() shows your strategies, what is pending, your quotas, the languages and the limits. rules()
   is the game manual; rules(section="Entity properties") gives the unit costs, health, damage and
   ranges, rules(section="API description") the client API.
2. Write or improve the strategy. Start from {A.STARTER}: one folder per language with the
   official client and a working ~100-line strategy (python/my_strategy.py is the reference).
3. Submit. When the code is pushed to a public git repository, prefer that, no upload needed:
   submit_strategy(name, language, git_url=..., git_ref=<the commit you pushed>). Otherwise zip the
   language's folder, call upload_link() and run the curl line it gives, then
   submit_strategy(name, language, upload_id=...). Say in `note` what you changed and which model you
   are: the notes are the strategy's changelog.
4. version(version_id, wait_s=50), again and again, until the status is ok or failed. A build takes
   from seconds (Python, cached layers) to 10 minutes (Java, Rust, GraalVM). failed: read error and
   log_tail, fix, resubmit.
5. play(opponents=["quickstart"]) queues a custom game against the engine's built-in bot (your strategy
   takes slot 0, the other seats get quickstart). game(game_id, wait_s=50), again and again, until it is
   finished: about a minute of play after the queue.
6. Analyse: game(id) gives scores and places; game_timeline(id) the course of the game (economy, army,
   losses, kills, which base fell when and to whom); game_log(id) your strategy's own output (print
   debug lines to stderr). Change the code and go back to 3.

Waiting: never sleep in the shell. version() and game() take wait_s (at most 50 seconds) and answer as
soon as the status changes; call them again while it is still pending.

Quotas: {int(sub.get("per_user_queued") or 1)} version waiting or building at a time, \
{int(sub.get("per_user_per_day") or 20)} versions a day, {int(sub.get("custom_games_queued") or 3)} custom \
games waiting, {int(sub.get("custom_games_per_day") or 50)} a day. A quota error names what is pending: wait \
for that version or game instead of retrying.

Ratings: custom games (play) are unrated. The rating comes from sandbox games the server schedules by
itself: every new active version of an enabled strategy plays {per_build or "a series of"} rated games
against strategies of similar rating. To get rated, submit and wait; strategy(name)["sandbox"] shows the
progress. Do not spam custom games for that.

Slots (seats) are numbered from 0 everywhere (seat 1 on the web page is slot 0). Times are UTC.
Show the human the pages: {ax.url("/games/<id>")} for a game with its replay viewer,
{ax.url("/strategies/<name>")} for a strategy.
"""


def _user(ctx: Context) -> dict:
    request = ctx.request_context.request
    user = getattr(getattr(request, "state", None), "user", None)
    if not user:
        raise ToolError("no user: this server needs a token (see /agents on the site)")
    return user


def build(ax: A.Agents) -> FastMCP:
    split = urlsplit(ax.base or "http://localhost")
    host, hostname = split.netloc or "localhost", split.hostname or "localhost"
    mcp = FastMCP(
        "codecraft",
        instructions=instructions(ax),
        website_url=ax.base or None,
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host, f"{hostname}:*", "localhost:*", "127.0.0.1:*"],
            allowed_origins=[f"{split.scheme or 'https'}://{host}"],
        ),
    )

    async def run(ctx: Context, tool: str, /, **args) -> CallToolResult:
        # Positional-only: tools have arguments called `name` too.
        user = _user(ctx)
        try:
            result = await A.call(ax, tool, user, args)
        except A.AgentError as exc:
            raise ToolError(str(exc)) from None
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        return CallToolResult(content=[TextContent(type="text", text=text)])

    # Read ------------------------------------------------------------------------------------------------
    @mcp.tool(annotations=READ)
    async def whoami(ctx: Context) -> CallToolResult:
        """Who you are here and where things stand. Call it first, and again when a quota error or a
        pending build or game puzzles you.

        Returns JSON: user {name, role}; strategies [{name, title, language, enabled, rating, games, wins,
        active_version {id, number}, latest_version {id, number, status, error}, url}]; pending
        {versions [{id, strategy, number, status}], games [{id, format, status}]}; quotas {versions_today,
        versions_per_day, versions_waiting, versions_waiting_allowed, custom_games_today,
        custom_games_per_day, custom_games_waiting, custom_games_waiting_allowed}; server {formats {name:
        {seats, about}}, languages (the values submit_strategy accepts), gpu, limits {tick_timeout_sec,
        cpu_time_sec, memory_mb, cpus}, limits_about, ticks, map_size, sandbox_games_per_build, max_zip_mb,
        git_submissions}; links {site, rules, leaderboard, agents, starter}."""
        return await run(ctx, "whoami")

    @mcp.tool(annotations=READ)
    async def rules(ctx: Context, section: str | None = None) -> CallToolResult:
        """The rules of CodeCraft 2020 as Markdown text (about 9k tokens in full): the game, the entity
        types, the order of actions within a tick, the round formats, the client API and its objects.

        section: one chapter only, picked by a case-insensitive part of its heading: "Entity properties"
        (costs, health, damage, ranges, population), "Game overview", "Control interface", "Round
        specific", "API description", "EntityAction", "PlayerView". An unknown section lists the headings."""
        return await run(ctx, "rules", section=section)

    @mcp.tool(annotations=READ)
    async def leaderboard(ctx: Context, limit: int = 30) -> CallToolResult:
        """The rated ladder, best first. Returns JSON {strategies: [{rank, name, title, owner, mine,
        language, rating, games, wins, avg_place, points, crashes, builtin}], total}. builtin marks the
        engine's own bot "quickstart". limit: 1-100 rows."""
        return await run(ctx, "leaderboard", limit=limit)

    @mcp.tool(annotations=READ)
    async def strategy(ctx: Context, name: str) -> CallToolResult:
        """One strategy (anyone's): owner, mine, language, enabled, rating, games, wins, avg_place, points,
        crashes, active_version_id, built_at, url, and its last 20 games (rows as in games()).
        For your own strategy also: versions [{id, number, status, active, language, source, note, error,
        created_at, finished_at}] newest first, limits (tick_timeout_sec, cpu_time_sec, memory_mb, ...)
        and sandbox {games_per_build, played_by_active_build}: how far the active version is through
        its rated sandbox games."""
        return await run(ctx, "strategy", name=name)

    @mcp.tool(annotations=READ)
    async def version(ctx: Context, version_id: int, wait_s: int = 0, log_tail_kb: int = 8) -> CallToolResult:
        """A version of your strategy: status (queued, building, ok or failed), active, error, image,
        timings, build_sec, queued_ahead (builds before it while queued), log_tail (the end of the build
        log, log_tail_kb KB, 0-64; 0 leaves it out) and next (what to do now).

        wait_s (0-50): wait up to that many seconds for the status to leave queued/building, then answer.
        A build takes seconds to 10 minutes plus the queue: call version(id, wait_s=50) again while pending,
        never sleep in the shell. An ok version becomes the strategy's active version at once and plays
        its rated sandbox games."""
        return await run(ctx, "version", version_id=version_id, wait_s=wait_s, log_tail_kb=log_tail_kb)

    @mcp.tool(annotations=READ)
    async def games(ctx: Context, strategy: str | None = None, kind: str | None = None, status: str | None = None,
                    limit: int = 20) -> CallToolResult:
        """Games, newest first. Default: the games of your strategies and the custom games you queued;
        strategy=<name> lists that strategy's games instead (anyone's). kind: sandbox (rated, scheduled by
        the server), custom (play) or tournament; status: queued, running, finished, error or aborted;
        limit 1-100. Returns JSON {games: [{id, kind, format, status, rated, seed (a string), created_at,
        duration_sec, error, seats [{slot, strategy, label, score, place, crashed}], url}]}."""
        return await run(ctx, "games", strategy=strategy, kind=kind, status=status, limit=limit)

    @mcp.tool(annotations=READ)
    async def game(ctx: Context, game_id: int, wait_s: int = 0) -> CallToolResult:
        """One game, as on its web page: kind, format, status, rated, seed (a string: pass it to play as it is
        to replay the same map), error, timings, created_by,
        seats [{slot, strategy, label, version_id, version (its number), score, place, points, rating_before, rating_after,
        cpu_sec, crashed, comment, mine}], my_slots, files (player-<slot>.log only for your seats), url,
        replay_json, queued_ahead (games before it while queued) and next.

        wait_s (0-50): wait up to that many seconds for the game to leave queued/running, then answer.
        A game plays about a minute after the queue: call game(id, wait_s=50) again while it is pending."""
        return await run(ctx, "game", game_id=game_id, wait_s=wait_s)

    @mcp.tool(annotations=READ)
    async def game_timeline(ctx: Context, game_id: int, every: int = 50) -> CallToolResult:
        """The course of a finished game, decoded from its replay: what you need to say why a strategy won
        or lost ("your builders stayed at 8 while the winner had 30 by tick 300", "your ranged base fell
        at tick 612 to slot 2").

        Returns JSON: game_id, format, seats [{slot, label, strategy, score, place, mine}], tick_count,
        map_size, fog_of_war, every; samples [{tick, seats: [{slot, score, resource, population [used,
        provided], units {type: count}, buildings {type: count of active ones}, built, lost, killed}]}]
        at tick 0, every `every` ticks (10-200) and at the last tick, where built (entities added), lost
        (own entities destroyed) and killed (enemy entities this slot destroyed) count by type since the
        previous sample, and types with a zero count are left out; events [{tick, event: base_destroyed
        (with base) or eliminated, slot, by: the slot that dealt the blow}]; replay_json, the URL of the
        full replay, with replay_json_shape describing it. About 25 KB at every=50 for 1000 ticks and 4
        players; every=100 halves it."""
        return await run(ctx, "game_timeline", game_id=game_id, every=every)

    @mcp.tool(annotations=READ)
    async def game_log(ctx: Context, game_id: int, slot: int | str | None = None, tail_kb: int = 16,
                       grep: str | None = None) -> CallToolResult:
        """A log of a game as text, with a header line. Default: your strategy's output (stdout and stderr
        of its container, player-<slot>.log) in your first slot of the game. slot=<n> picks a slot (your
        strategies' only); slot="engine" gives the engine's log (anyone's game).
        tail_kb: the last KB of the log (1-64, default 16).
        grep: plain text, case-insensitive, `|` separates alternatives (not a regular expression);
        answers the matching lines with their line numbers instead of the tail, at most 200 (e.g.
        grep="error|exception|traceback")."""
        return await run(ctx, "game_log", game_id=game_id, slot=slot, tail_kb=tail_kb, grep=grep)

    # Write -----------------------------------------------------------------------------------------------
    @mcp.tool(annotations=WRITE)
    async def upload_link(ctx: Context) -> CallToolResult:
        """A short-lived URL for uploading a strategy's source from your shell with curl, for
        submit_strategy(upload_id=...). Returns JSON {url, curl, expires, max_files, max_mb, how}.

        Zip the strategy folder (cd python && zip -qr ../strategy.zip .), then run the curl line:
        curl -sS -T strategy.zip <url> (keep the trailing slash: curl appends the file name). It answers
        one JSON line {id, name, size, sha256, expires}; the id goes to submit_strategy. One source file
        (my_strategy.py) may be uploaded as it is: it is stored as a zip holding that file. The link lives
        15 minutes and takes 3 files; uploads are kept 60 minutes and a submission consumes its upload.
        If curl cannot reach the host, your sandbox's network allow-list lacks it: tell the user rather
        than retrying."""
        return await run(ctx, "upload_link")

    @mcp.tool(annotations=WRITE)
    async def submit_strategy(ctx: Context, name: str, language: str, upload_id: str | None = None,
                              git_url: str | None = None, git_ref: str | None = None, note: str | None = None,
                              title: str | None = None) -> CallToolResult:
        """Submit a new version of a strategy of yours; a new name creates the strategy, owned by you.
        The source is exactly one of: upload_id (from the upload's JSON line, see upload_link) or git_url
        of a public http(s) repository, with git_ref (a branch, tag or commit; default the default branch).

        name: 1-41 characters of a-z 0-9 _ -, starting with a letter or digit. language: one of
        whoami()["server"]["languages"] (python, cpp, java, ...); the official 2020 client of that
        language is used: a single file replaces the client's strategy file (my_strategy.py,
        MyStrategy.java, ...), several files replace the whole client project (keep its compile.sh and
        run.sh working), and a root that holds only one folder per language (the starter) is built from
        the folder named after the language. note: what changed and your model name, up to 200
        characters (the strategy's changelog). title: a display title for a new strategy.

        Returns JSON {version_id, strategy, number, status: queued, pending_ahead (builds before it), url,
        next}. Then version(version_id, wait_s=50) until ok or failed. One version may wait or build at a
        time: the quota error names the pending one, wait for it."""
        return await run(ctx, "submit_strategy", name=name, language=language, upload_id=upload_id,
                         git_url=git_url, git_ref=git_ref, note=note, title=title)

    @mcp.tool(annotations=WRITE)
    async def play(ctx: Context, opponents: list[str], format: str = "Round1", mine: str | None = None,
                   seed: Seed = None, fill: str = "quickstart") -> CallToolResult:
        """Queue an unrated custom game. Slot 0 is `mine`: one of your strategies, or name@version_id for
        an older successful version of it (may be left out when you own exactly one strategy). Then the
        opponents in order: strategy names from leaderboard(), name@version_id of your own versions,
        "quickstart" (the engine's built-in bot) or "empty". The format's remaining seats get `fill`
        ("quickstart" or "empty"). format: Round1 (4 seats, no fog of war, three bases, a turret and one
        unit of each kind at the start), Round2 (4 seats, fog of war, a builder base and one builder) or
        Finals (2 seats, the same as Round2 1v1). seed: the map and the engine's dice, a string of digits
        0..2^64-1, default random; to replay a game's map, pass the seed string game() gives unchanged, e.g.
        6951506665061147718 as a JSON string. A JSON number above 2^53 loses its last digits on the way, so
        such numbers are refused.

        Returns JSON {game_id, url, format, rated: false, seats [{slot, label}], queued_ahead, next}.
        Then game(game_id, wait_s=50) until finished. A paused (disabled) strategy cannot be seated.
        Custom games never change ratings (see strategy(name)["sandbox"] for those); quota: a few games
        waiting at a time and a daily cap, and the quota error names the games to wait for."""
        return await run(ctx, "play", opponents=opponents, format=format, mine=mine, seed=seed, fill=fill)

    @mcp.tool(annotations=WRITE)
    async def set_strategy(ctx: Context, name: str, enabled: bool | None = None, title: str | None = None,
                           activate_version: int | None = None) -> CallToolResult:
        """Change one of your strategies. enabled=false pauses it (no rated sandbox games, and it cannot be
        seated in custom games either), enabled=true resumes it. title: the display title.
        activate_version=<version id> makes another successfully built version the active one at once
        (a rollback); the ids are in strategy(name)["versions"].
        Returns JSON: the strategy as strategy(name) shows its public part, plus changed (what was done)."""
        return await run(ctx, "set_strategy", name=name, enabled=enabled, title=title,
                         activate_version=activate_version)

    # Resources --------------------------------------------------------------------------------------------
    @mcp.resource("codecraft://rules", name="rules", title="CodeCraft 2020 rules", mime_type="text/markdown",
                  description="The game manual, the same text as the rules tool.")
    def rules_resource() -> str:
        return (ax.cfg.root / "docs" / "doc-en.md").read_text()

    @mcp.resource("codecraft://replay-format", name="replay-format", title="The replay format",
                  mime_type="text/markdown",
                  description="How the official replay.dat is encoded and what /games/<id>/replay.json holds.")
    def replay_format_resource() -> str:
        return (ax.cfg.root / "docs" / "replay-format.md").read_text()

    return mcp
