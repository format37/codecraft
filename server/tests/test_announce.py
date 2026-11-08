"""The worker's announcements to the community webhook: a strategy's first build, Monday's top ten, a new engine
version; one POST each, a failure never propagates."""
import json
from datetime import datetime, timezone

from codecraft import announce
from codecraft.announce import Announcer
from test_agents import Env

WEBHOOK = "https://discord.com/api/webhooks/1/abc"
MONDAY = datetime(2026, 11, 9, 0, 3, tzinfo=timezone.utc)
TUESDAY = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
NEXT_MONDAY = datetime(2026, 11, 16, 0, 1, tzinfo=timezone.utc)


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b""


def fake_urlopen(calls: list, fail: bool = False):
    def urlopen(req, timeout=None):
        calls.append((req.full_url, json.loads(req.data), timeout, req.get_header("Content-type")))
        if fail:
            raise OSError("connection refused")
        return FakeResponse()
    return urlopen


def make(tmp_path, monkeypatch, webhook=WEBHOOK, fail=False) -> tuple[Env, Announcer, list]:
    env = Env(tmp_path)
    env.cfg.data["community"]["discord_webhook"] = webhook
    env.cfg.data["auth"]["base_url"] = "https://codecraft.day"
    calls: list = []
    monkeypatch.setattr(announce.urllib.request, "urlopen", fake_urlopen(calls, fail))
    return env, Announcer(env.cfg, env.db), calls


def test_a_strategys_first_build_is_announced_and_the_next_ones_are_not(tmp_path, monkeypatch):
    env, ann, calls = make(tmp_path, monkeypatch)
    assert ann.enabled
    user, _ = env.user()
    (vid,) = env.built(user, "bot")
    ann.built(env.db.version(vid), env.db.strategy("bot"))
    assert len(calls) == 1
    url, payload, timeout, ctype = calls[0]
    assert url == WEBHOOK and timeout == announce.TIMEOUT and ctype == "application/json"
    assert payload["content"] == "**Alice**'s **bot** entered the ladder (python): https://codecraft.day/strategies/bot"
    assert payload["allowed_mentions"] == {"parse": []}           # a name can never ping the server
    (vid2,) = env.built(user, "bot")
    ann.built(env.db.version(vid2), env.db.strategy("bot"))
    assert len(calls) == 1


def test_a_webhook_that_fails_is_logged_and_swallowed(tmp_path, monkeypatch, caplog):
    env, ann, calls = make(tmp_path, monkeypatch, fail=True)
    user, _ = env.user()
    (vid,) = env.built(user, "bot")
    with caplog.at_level("WARNING", logger="codecraft.announce"):
        ann.built(env.db.version(vid), env.db.strategy("bot"))     # no exception
    assert len(calls) == 1 and "announcement not posted: connection refused" in caplog.text


def test_mondays_top_ten_goes_out_once_a_week(tmp_path, monkeypatch):
    env, ann, calls = make(tmp_path, monkeypatch)
    user, _ = env.user()
    env.built(user, "bot")
    with env.db.tx() as c:
        c.execute("UPDATE strategies SET games=12, rating=1337 WHERE name='bot'")
        c.execute("UPDATE strategies SET games=12, rating=1180 WHERE name='quickstart'")
    ann.tick(now=TUESDAY)
    assert calls == []
    ann.tick(now=MONDAY)
    ann.tick(now=MONDAY.replace(hour=9))
    assert len(calls) == 1
    text = calls[0][1]["content"]
    assert text.startswith("This week's top ten:\n1. **bot** 1337 (Alice)\n2. **quickstart** 1180\n")
    assert text.endswith("https://codecraft.day/leaderboard")
    ann.tick(now=NEXT_MONDAY)
    assert len(calls) == 2
    # the week is remembered across a worker restart
    again = Announcer(env.cfg, env.db)
    again.tick(now=NEXT_MONDAY.replace(hour=20))
    assert len(calls) == 2


def test_an_empty_ladder_has_no_top_ten(tmp_path, monkeypatch):
    env, ann, calls = make(tmp_path, monkeypatch)
    ann.tick(now=MONDAY)                                           # quickstart has no game yet
    assert calls == []


def test_the_engine_version_is_announced_when_it_changes(tmp_path, monkeypatch):
    env, ann, calls = make(tmp_path, monkeypatch)
    ann.tick(engine="codecraft/sim-cpp:aaaaaaaaaaaa", now=TUESDAY)   # the engine at start-up: remembered, not news
    ann.tick(engine="codecraft/sim-cpp:aaaaaaaaaaaa", now=TUESDAY)
    assert calls == []
    ann.tick(engine="codecraft/sim-cpp:bbbbbbbbbbbb", now=TUESDAY)
    ann.tick(engine="codecraft/sim-cpp:bbbbbbbbbbbb", now=TUESDAY)
    assert len(calls) == 1
    text = calls[0][1]["content"]
    assert text.startswith("The engine moved to version `bbbbbbbbbbbb`")
    assert "ghcr.io/format37/codecraft-runner:bbbbbbbbbbbb" in text and "https://codecraft.day/quickstart" in text
    assert json.loads((env.cfg.data_dir / announce.STATE_FILE).read_text())["engine"] == "codecraft/sim-cpp:bbbbbbbbbbbb"


def test_without_a_webhook_nothing_is_posted(tmp_path, monkeypatch):
    env, ann, calls = make(tmp_path, monkeypatch, webhook=None)
    assert not ann.enabled
    user, _ = env.user()
    (vid,) = env.built(user, "bot")
    ann.built(env.db.version(vid), env.db.strategy("bot"))
    ann.tick(engine="codecraft/sim-cpp:aaaaaaaaaaaa", now=MONDAY)
    ann.tick(engine="codecraft/sim-cpp:bbbbbbbbbbbb", now=MONDAY)
    assert calls == [] and not ann.post("anything")
