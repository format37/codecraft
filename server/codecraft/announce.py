"""Announcements to the community: the worker posts to a Discord webhook (`community.discord_webhook`, or
CW_DISCORD_WEBHOOK) when a strategy's first version builds, on Mondays the top ten, and when the engine version
changes. One POST each, 5 s timeout; a failure is logged and swallowed, a game or a build never waits for Discord.
What goes out is public already: strategy names, owners' display names, ratings, the engine's version."""
from __future__ import annotations

import json
import logging
import threading
import urllib.request
from datetime import datetime, timezone

from .config import Config
from .db import DB

log = logging.getLogger("codecraft.announce")

TIMEOUT = 5
RUNNER_IMAGE = "ghcr.io/format37/codecraft-runner"
STATE_FILE = "announce.json"      # in data/: the week of the last top ten, the engine last seen


class Announcer:
    def __init__(self, cfg: Config, db: DB):
        self.cfg, self.db = cfg, db
        self.url = cfg.get("community", "discord_webhook") or None
        self.base = str(cfg["auth"]["base_url"]).rstrip("/")
        self.state_path = cfg.data_dir / STATE_FILE
        self.lock = threading.Lock()
        self.state = self._load()

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def _load(self) -> dict:
        try:
            return dict(json.loads(self.state_path.read_text()))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self.state))
        except OSError as exc:
            log.warning("announce state not saved: %s", exc)

    def post(self, text: str) -> bool:
        """One message to the webhook; False, and a warning in the log, when it did not go out."""
        if not self.url:
            return False
        body = json.dumps({"content": text[:2000], "allowed_mentions": {"parse": []}}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "CodeCraft worker"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                r.read()
            return True
        except Exception as exc:  # noqa: BLE001  the network, a 4xx, a timeout: none of them is the worker's problem
            log.warning("announcement not posted: %s", exc)
            return False

    # ---- what is announced ------------------------------------------------------------------------------------------
    def built(self, version, strategy) -> None:
        """A version built (status ok). The strategy's first one makes it a newcomer on the ladder."""
        if not self.url or version is None:
            return
        name = version["strategy"]
        if len(self.db.ok_versions(name)) != 1:
            return
        owner = (strategy["owner_name"] if strategy is not None and "owner_name" in strategy.keys() else None) or "Someone"
        language = version["language"] or "?"
        self.post(f"**{owner}**'s **{name}** entered the ladder ({language}): {self.base}/strategies/{name}")

    def top_ten(self) -> str | None:
        """The ten best settled ratings: a strategy still playing its placement games is not in it."""
        settled = int((self.cfg.get("leagues") or {}).get("settled_games", 30))
        rows = [r for r in self.db.leaderboard() if r["rating"] is not None and (r["build_games"] or 0) >= settled][:10]
        if not rows:
            return None
        lines = [f"{i}. **{r['name']}** {r['rating']:.0f}" + (f" ({r['owner_name']})" if r["owner_name"] else "")
                 for i, r in enumerate(rows, 1)]
        return "This week's top ten:\n" + "\n".join(lines) + f"\n{self.base}/leaderboard"

    def tick(self, engine: str | None = None, now: datetime | None = None) -> None:
        """Between games: Monday's top ten (once a week, UTC) and the engine's version when it changes. The first
        engine seen is only remembered, so that a deploy does not announce the engine it already played on."""
        if not self.url:
            return
        now = now or datetime.now(timezone.utc)
        with self.lock:
            year, week, weekday = now.isocalendar()
            this_week = f"{year}-W{week:02d}"
            if weekday == 1 and self.state.get("week") != this_week:
                self.state["week"] = this_week
                self._save()                   # saved first: a post that fails is not retried all Monday long
                text = self.top_ten()
                if text:
                    self.post(text)
            if engine and engine != self.state.get("engine"):
                first = "engine" not in self.state
                self.state["engine"] = engine
                self._save()
                if not first:
                    version = engine.rsplit(":", 1)[-1]
                    self.post(f"The engine moved to version `{version}`: every game from now on is played on it. "
                              f"The same build for your machine: `docker run --rm -it -p 31000:31000 "
                              f"-p 31001-31004:31001-31004 -v \"$PWD\":/game {RUNNER_IMAGE}:{version}` "
                              f"({self.base}/quickstart)")
