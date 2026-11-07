"""The coding-agent surface: tokens and the /agents page, the REST mirror, uploads, the tools, the MCP
endpoint (plain JSON-RPC over HTTP: stateless + JSON responses need no MCP client), log redaction."""
import hashlib
import inspect
import io
import json
import logging
import re
import shutil
import types
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codecraft import agents as A
from codecraft import monitoring
from codecraft.auth import Auth, RateLimiter, hash_token
from codecraft.builder import engine_source, source_version
from codecraft.config import Config
from codecraft.db import DB
from codecraft.web import COOKIE, create_app
from test_replay import make_battle_replay
from test_web import FakeMailer

REPO = Path(__file__).resolve().parents[2]
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def zip_bytes(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


class Env:
    """An app on a temporary root with one language (python), the rules and the quickstart bot."""

    def __init__(self, tmp_path: Path):
        (tmp_path / "clients" / "Python").mkdir(parents=True)
        (tmp_path / "clients" / "Python" / "Dockerfile").write_text("FROM python:3.8\n")
        (tmp_path / "templates").mkdir()
        (tmp_path / "docs").mkdir()
        for name in ("doc-en.md", "replay-format.md"):
            shutil.copy(REPO / "docs" / name, tmp_path / "docs" / name)
        cfg = Config(Config.load(tmp_path).data, tmp_path)
        cfg.data["auth"]["base_url"] = "http://testserver"
        cfg.data["auth"]["cookie_secure"] = False
        cfg.resolve_gpu(None)
        cfg.ensure_dirs()
        self.cfg, self.db = cfg, DB(tmp_path / "t.db")
        self.auth = Auth(cfg, self.db, FakeMailer())
        self.app = create_app(cfg, self.db, self.auth)
        self.ax: A.Agents = self.app.state.agents
        self.db.upsert_strategy(types.SimpleNamespace(name="quickstart", title="QuickStart", language=None, gpu=False,
                                                      builtin="QuickStart", enabled=True, limits=None), 1200.0)

    def user(self, email="alice@example.com", name="Alice") -> tuple[dict, str]:
        """A user and a fresh API token of theirs."""
        user = self.db.user_by_email(email) or self.db.create_user(email, name)
        return dict(user), self.ax.tokens.create(user["id"], "test")

    def built(self, user: dict, name="bot", n=1) -> list[int]:
        """`n` successfully built versions of a strategy of `user`; the last one is active."""
        ids = []
        for i in range(n):
            vid = A.submit(self.cfg, self.db, name=name, language="python", user=user,
                           zip_data=zip_bytes({"my_strategy.py": f"v{i}"}), note=f"v{i + 1}")
            self.db.finish_version(vid, "ok", image=f"img/{name}:{vid}", build_hash=str(vid))
            ids.append(vid)
        return ids

    def finished_game(self, seats: list[dict], status="finished") -> int:
        gid = self.db.queue_game("Finals" if len(seats) == 2 else "Round1", 7, None, seats, kind="sandbox", rated=False)
        self.db.finish_game(gid, status, None, 40.0, 7, [{**s, "score": 100 * (len(seats) - s["slot"])} for s in seats],
                            {"initial": 1200, "k": 32}, [8, 4, 2, 1], rated=False)
        return gid


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    with TestClient(e.app, follow_redirects=False) as client:   # the lifespan starts the MCP session manager
        e.client = client
        yield e


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def rpc(env, token, method, params=None, path=None, id_=1):
    body = {"jsonrpc": "2.0", "id": id_, "method": method, **({"params": params} if params is not None else {})}
    return env.client.post(path or f"/mcp/{token}/", json=body, headers=MCP_HEADERS)


# Tokens ---------------------------------------------------------------------------------------------------
def test_the_agents_page_creates_a_token_once_lists_and_revokes_it(env):
    assert env.client.get("/agents").status_code == 200                   # public part
    env.auth.request_code("alice@example.com", "1.1.1.1")
    code = re.search(r"\b(\d{6})\b", env.auth.mailer.sent[-1][1]).group(1)
    _, session = env.auth.login("alice@example.com", code, "1.1.1.1", "tests", name="Alice")
    env.client.cookies.set(COOKIE, session)
    r = env.client.post("/agents/tokens", data={"label": "laptop"})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    token = re.search(r'id="new-token">(cc_[A-Za-z0-9_-]+)<', r.text).group(1)
    assert len(token) == 43 and f"/mcp/{token}/" in r.text                  # the setup lines carry it
    row = env.db.one("SELECT * FROM api_tokens WHERE label='laptop'")
    assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest() and token not in str(dict(row))
    page = env.client.get("/agents").text
    assert token not in page and "laptop" in page and "&lt;token&gt;" in page
    for i in range(4):
        env.client.post("/agents/tokens", data={"label": f"t{i}"})
    r = env.client.post("/agents/tokens", data={"label": "sixth"})
    assert r.status_code == 303 and "revoke" in r.headers["location"]
    assert len(env.db.api_tokens(row["user_id"])) == 5
    r = env.client.post(f"/agents/tokens/{row['id']}/revoke")
    assert r.status_code == 303 and len(env.db.api_tokens(row["user_id"])) == 4
    env.client.cookies.clear()
    assert env.client.get("/api/v1/whoami", headers=bearer(token)).status_code == 401


def test_a_token_is_needed_and_the_cookie_does_not_count(env):
    user, token = env.user()
    r = env.client.get("/api/v1/whoami", headers=bearer(token))
    assert r.status_code == 200 and r.json()["user"]["name"] == "Alice"
    assert env.db.one("SELECT last_used_at FROM api_tokens")["last_used_at"] is not None
    assert rpc(env, token, "tools/list").status_code == 200                           # path form
    assert env.client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                           headers={**MCP_HEADERS, **bearer(token)}).status_code == 200  # header form
    for bad in ("cc_" + "x" * 40, "nonsense", ""):
        r = env.client.get("/api/v1/whoami", headers=bearer(bad))
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
        assert rpc(env, bad, "tools/list", path=f"/mcp/{bad}/" if bad.startswith("cc_") else "/mcp/").status_code == 401
    sid = "s" * 40                                                                   # a signed-in browser
    env.db.create_session(hash_token(sid), user["id"], 1, None, None)
    env.client.cookies.set(COOKIE, sid)
    assert env.client.get("/me").status_code == 200
    assert env.client.get("/api/v1/whoami").status_code == 401
    env.client.cookies.clear()
    env.db.update_user(user["id"], disabled=1)
    assert env.client.get("/api/v1/whoami", headers=bearer(token)).status_code == 401
    env.db.update_user(user["id"], disabled=0)
    env.db.revoke_api_token(user["id"], env.db.api_tokens(user["id"])[0]["id"])
    assert env.client.get("/api/v1/whoami", headers=bearer(token)).status_code == 401


def test_the_path_token_is_cut_out_before_the_mcp_app_and_the_limiter_answers_429(env):
    _, token = env.user()
    assert rpc(env, token, "tools/list", path=f"/mcp/{token}").status_code == 200   # no trailing slash
    assert rpc(env, token, "tools/list", path=f"/mcp/{token}/mcp").status_code == 404
    r = env.client.get(f"/mcp/{token}/", headers={"Accept": "text/event-stream"})      # no idle event stream
    assert r.status_code == 405 and r.headers["allow"] == "POST"
    assert env.client.post("/mcp/mcp", json={}, headers={**MCP_HEADERS, **bearer(token)}).status_code == 404
    env.ax.limiter = RateLimiter(3, 60)
    codes = [env.client.get("/api/v1/whoami", headers=bearer(token)).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


# REST ------------------------------------------------------------------------------------------------------
def test_whoami_mentions_gvisor_only_when_the_server_runs_it(env):
    user, token = env.user()

    def about():
        return env.client.post("/api/v1/whoami", headers=bearer(token)).json()["server"]["limits_about"]

    assert "gVisor" not in about()
    env.ax.cfg.data["docker"]["runtime"] = "runsc"
    assert "gVisor" in about()


def test_a_game_names_its_engine_and_whoami_the_current_one(env):
    """A game names the engine image it was played on, tagged with the version of its source (its page, game());
    whoami names the image of the current source, none while that cannot be read."""
    user, token = env.user()
    h = bearer(token)
    gid = env.finished_game([{"slot": 0, "strategy": "quickstart", "label": "quickstart"},
                             {"slot": 1, "strategy": "quickstart", "label": "quickstart"}])
    assert env.client.get(f"/api/v1/game?game_id={gid}", headers=h).json()["engine"] is None
    assert "engine <code" not in env.client.get(f"/games/{gid}").text
    env.db.set_game_engine(gid, "codecraft/sim-cpp:7390f3fc2f58")
    assert env.client.get(f"/api/v1/game?game_id={gid}", headers=h).json()["engine"] == "codecraft/sim-cpp:7390f3fc2f58"
    page = env.client.get(f"/games/{gid}").text
    assert "40 s · engine <code" in page and ">codecraft/sim-cpp:7390f3fc2f58</code>" in page
    server = env.client.post("/api/v1/whoami", headers=h).json()["server"]
    assert server["engine"] is None and "--version" in server["engine_about"]      # no sim-cpp in this root
    ctx = env.cfg.root / "sim-cpp"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM debian\n")
    server = env.client.post("/api/v1/whoami", headers=h).json()["server"]
    assert server["engine"] == f"codecraft/sim-cpp:{source_version(engine_source(ctx))}"


def test_rest_dispatcher_read_tools_errors_and_get_with_query_params(env):
    user, token = env.user()
    vids = env.built(user, "bot", 2)
    gid = env.finished_game([{"slot": 0, "strategy": "bot", "label": "bot", "image": f"img/bot:{vids[1]}"},
                             {"slot": 1, "strategy": "quickstart", "label": "quickstart"}])
    h = bearer(token)
    me = env.client.post("/api/v1/whoami", headers=h).json()
    assert me["strategies"][0]["name"] == "bot" and me["strategies"][0]["active_version"]["id"] == vids[1]
    assert me["quotas"]["versions_per_day"] == 20 and "python" in me["server"]["languages"]
    board = env.client.get("/api/v1/leaderboard?limit=1", headers=h).json()
    assert len(board["strategies"]) == 1 and board["total"] == 2
    s = env.client.get("/api/v1/strategy?name=bot", headers=h).json()
    assert [v["number"] for v in s["versions"]] == [2, 1] and s["versions"][0]["active"] and s["games"][0]["id"] == gid
    assert s["sandbox"]["games_per_build"] == 10 and s["limits"]["tick_timeout_sec"] == 1.0
    v = env.client.get(f"/api/v1/version?version_id={vids[0]}", headers=h).json()
    assert v["status"] == "ok" and v["active"] is False and "activate_version" in v["next"]
    g = env.client.get(f"/api/v1/game?game_id={gid}", headers=h).json()
    assert g["status"] == "finished" and g["my_slots"] == [0] and g["seats"][0]["place"] == 1
    assert g["seed"] == "7"                       # a string: 64-bit seeds do not survive a JSON number
    assert (g["seats"][0]["version_id"], g["seats"][0]["version"]) == (vids[1], 2)   # seated by name: from the image
    assert g["seats"][1]["version"] is None
    listed = env.client.post("/api/v1/games", json={"limit": 5}, headers=h).json()["games"]
    assert [x["id"] for x in listed] == [gid]
    r = env.client.get("/api/v1/nope", headers=h)
    assert r.status_code == 404 and "whoami" in r.json()["error"]
    r = env.client.get("/api/v1/game?game_id=abc", headers=h)
    assert r.status_code == 400 and "game_id" in r.json()["error"]
    r = env.client.post("/api/v1/game", json={"game_id": gid, "colour": "red"}, headers=h)
    assert r.status_code == 400 and "colour" in r.json()["error"]
    r = env.client.get("/api/v1/game?game_id=999", headers=h)
    assert r.status_code == 400 and r.json()["error"] == "no game #999"
    assert env.client.get("/api/v1/play", headers=h).status_code == 405
    assert env.client.post("/api/v1/game", content=b"[1]", headers=h).status_code == 400
    other, other_token = env.user("bob@example.com", "Bob")
    r = env.client.get(f"/api/v1/version?version_id={vids[0]}", headers=bearer(other_token))
    assert r.status_code == 400 and "another user's" in r.json()["error"]
    assert "versions" not in env.client.get("/api/v1/strategy?name=bot", headers=bearer(other_token)).json()


def test_rules_whole_and_by_section(env):
    _, token = env.user()
    whole = env.client.get("/api/v1/rules", headers=bearer(token))
    assert whole.status_code == 200 and whole.text.startswith("# About CodeCraft 2020")
    part = env.client.get("/api/v1/rules?section=entity properties", headers=bearer(token)).text
    assert part.startswith("## Entity properties values") and "## API description" not in part
    r = env.client.get("/api/v1/rules?section=chess", headers=bearer(token))
    assert r.status_code == 400 and "Game overview" in r.json()["error"]


def test_version_waits_while_pending(env, monkeypatch):
    user, token = env.user()
    vid = A.submit(env.cfg, env.db, name="bot", language="python", user=user, zip_data=zip_bytes({"a.py": "x"}))
    monkeypatch.setattr(A, "POLL_S", 0.05)
    log = env.cfg.build_dir / "logs" / "bot-v1.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("step 1\nstep 2\n")
    r = env.client.get(f"/api/v1/version?version_id={vid}&wait_s=1", headers=bearer(token)).json()
    assert r["status"] == "queued" and r["queued_ahead"] == 0 and r["log_tail"].endswith("step 2\n")


def test_a_failed_version_reads_without_colours_and_server_paths(env):
    """The live check of 2026-11-06: log_tail carried docker's terminal colours, the error a log path."""
    user, token = env.user()
    vid = A.submit(env.cfg, env.db, name="bot", language="python", user=user, zip_data=zip_bytes({"a.py": "x"}))
    log = env.cfg.build_dir / "logs" / "bot-v1.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("Step 8/9 : RUN bash compile.sh\n\x1b[91m  File \"main.py\", line 47\n\x1b[0mSyntaxError\n")
    env.db.finish_version(vid, "failed", error="docker build of img failed: \x1b[91mboom\x1b[0m\n"
                                              "(see /app/data/build/logs/bot-v1.log) in /app/data/build/repos/bot")
    r = env.client.get(f"/api/v1/version?version_id={vid}", headers=bearer(token)).json()
    assert r["status"] == "failed" and "\x1b" not in r["log_tail"] and 'File "main.py", line 47' in r["log_tail"]
    assert r["error"] == "docker build of img failed: boom in <clone>"
    s = env.client.get("/api/v1/strategy?name=bot", headers=bearer(token)).json()
    assert s["versions"][0]["error"] == r["error"]


# Uploads and submissions ------------------------------------------------------------------------------------
def upload(env, token, name, body):
    link = env.client.post("/api/v1/upload_link", headers=bearer(token)).json()
    assert link["curl"] == f"curl -sS -T strategy.zip {link['url']}" and link["max_files"] == 3
    return link, env.client.put(link["url"].replace("http://testserver", "") + name, content=body)


def test_upload_then_submit_consumes_the_upload(env):
    user, token = env.user()
    data = zip_bytes({"my_strategy.py": "print(1)"})
    link, r = upload(env, token, "strategy.zip", data)
    assert r.status_code == 201 and r.text.endswith("\n")
    rec = r.json()
    assert rec["sha256"] == hashlib.sha256(data).hexdigest() and rec["size"] == len(data) and "wrapped" not in rec
    stored = env.cfg.uploads_dir / rec["id"] / "strategy.zip"
    assert stored.read_bytes() == data and oct(stored.stat().st_mode & 0o777) == "0o600"
    out = env.client.post("/api/v1/submit_strategy", headers=bearer(token),
                          json={"name": "bot", "language": "python", "upload_id": rec["id"], "note": "first"}).json()
    v = env.db.version(out["version_id"])
    assert v["status"] == "queued" and v["note"] == "first" and out["pending_ahead"] == 0
    assert (env.cfg.data_dir / v["source"] / "my_strategy.py").read_text() == "print(1)"
    assert not stored.exists() and not (env.cfg.uploads_dir / f"{rec['id']}.json").exists()
    r = env.client.post("/api/v1/submit_strategy", headers=bearer(token),
                        json={"name": "bot", "language": "python", "git_url": "https://github.com/x/y", "git_ref": "abc123"})
    assert r.status_code == 400 and f"version id {out['version_id']}" in r.json()["error"]   # the quota names it
    env.db.finish_version(out["version_id"], "failed", error="boom")
    r = env.client.post("/api/v1/submit_strategy", headers=bearer(token),
                        json={"name": "bot", "language": "python", "git_url": "https://github.com/x/y", "git_ref": "abc123"})
    v2 = env.db.version(r.json()["version_id"])
    assert v2["source_kind"] == "git" and v2["git_ref"] == "abc123" and v2["number"] == 2
    r = env.client.post("/api/v1/submit_strategy", headers=bearer(token), json={"name": "x", "language": "python"})
    assert r.status_code == 400 and "exactly one source" in r.json()["error"]


def test_upload_limits_wrapping_and_expiry(env):
    user, token = env.user()
    _, r = upload(env, token, "my_strategy.py", b"print('hi')\n")
    rec = r.json()
    assert r.status_code == 201 and rec["wrapped"]
    with zipfile.ZipFile(env.cfg.uploads_dir / rec["id"] / "my_strategy.py.zip") as zf:
        assert zf.read("my_strategy.py") == b"print('hi')\n"
    _, r = upload(env, token, "notes.txt", b"hello")
    assert r.status_code == 400 and "zip" in r.json()["error"]
    env.ax.uploads.max_bytes = 1000
    _, r = upload(env, token, "big.zip", b"x" * 2000)
    assert r.status_code == 413
    assert sorted(p.name for p in env.cfg.uploads_dir.iterdir()) == sorted([rec["id"], rec["id"] + ".json"])
    link, _ = upload(env, token, "a.py", b"1")
    path = link["url"].replace("http://testserver", "")
    for t in env.ax.uploads.tickets.values():
        t.expires = 0
    r = env.client.put(path + "b.py", content=b"2")
    assert r.status_code == 404 and "upload_link" in r.json()["error"]
    assert env.client.get(path + "b.py").status_code == 405
    assert env.client.put("/mcp/upload/nope/x.zip", content=b"1").status_code == 404
    other, other_token = env.user("bob@example.com", "Bob")
    r = env.client.post("/api/v1/submit_strategy", headers=bearer(other_token),
                        json={"name": "bobbot", "language": "python", "upload_id": rec["id"]})
    assert r.status_code == 400 and "unknown or expired upload" in r.json()["error"]


def test_the_sweep_removes_expired_uploads_and_debris(env):
    _, token = env.user()
    _, r = upload(env, token, "a.py", b"1")
    rec = r.json()
    (env.cfg.uploads_dir / "u_000000000000.part").write_bytes(b"left over")
    env.ax.uploads.sweep()
    assert (env.cfg.uploads_dir / rec["id"]).is_dir() and not (env.cfg.uploads_dir / "u_000000000000.part").exists()
    env.ax.uploads.sweep(now=10 ** 12)
    assert list(env.cfg.uploads_dir.iterdir()) == []


# Custom games ------------------------------------------------------------------------------------------------
def test_play_seats_fill_versions_quota_and_seed(env):
    user, token = env.user()
    v1, v2 = env.built(user, "bot", 2)
    other, other_token = env.user("bob@example.com", "Bob")
    b1, b2 = env.built(other, "bobbot", 2)
    h = bearer(token)
    r = env.client.post("/api/v1/play", headers=h, json={"opponents": ["bobbot"], "seed": 42}).json()
    _, seats = env.db.game(r["game_id"])
    assert [s["strategy"] for s in seats] == ["bot", "bobbot", "quickstart", "quickstart"]
    assert r["rated"] is False and env.db.game(r["game_id"])[0]["seed"] == "42"
    r = env.client.post("/api/v1/play", headers=h,
                        json={"format": "Finals", "mine": f"bot@{v1}", "opponents": [f"bot@{v2}"]}).json()
    _, seats = env.db.game(r["game_id"])
    assert [(s["label"], s["version_id"]) for s in seats] == [("bot-v1", v1), ("bot", None)]  # @active = the name
    r = env.client.post("/api/v1/play", headers=h, json={"format": "Round2", "opponents": [], "fill": "empty"}).json()
    assert [s["label"] for s in r["seats"]] == ["bot", "empty", "empty", "empty"]
    r = env.client.post("/api/v1/play", headers=h, json={"opponents": ["quickstart"]})
    assert r.status_code == 400 and "games waiting" in r.json()["error"] and "#1" in r.json()["error"]
    oh = bearer(other_token)
    r = env.client.post("/api/v1/play", headers=oh, json={"opponents": [f"bot@{v1}"]})
    assert r.status_code == 400 and "not available" in r.json()["error"] and "owner only" in r.json()["error"]
    r = env.client.post("/api/v1/play", headers=oh, json={"mine": "bot", "opponents": []})
    assert r.status_code == 400 and "not one of your strategies" in r.json()["error"]
    r = env.client.post("/api/v1/play", headers=oh, json={"opponents": ["ghost"]})
    assert r.status_code == 400 and "no such strategy" in r.json()["error"]
    r = env.client.post("/api/v1/play", headers=oh, json={"opponents": [], "seed": '"43"'}).json()
    assert str(env.db.game(r["game_id"])[0]["seed"]) == "43"        # quotes sent along with the string are dropped
    for bad in (str(2 ** 64), -1, "x"):
        r = env.client.post("/api/v1/play", headers=oh, json={"opponents": [], "seed": bad})
        assert r.status_code == 400 and "unsigned 64-bit" in r.json()["error"]
    # A number above 2^53 may have been rounded by a JavaScript client on the way: refused, not played.
    n = len(env.db.games(limit=100))
    r = env.client.post("/api/v1/play", headers=oh, json={"opponents": [], "seed": 11249517513654704000})
    assert r.status_code == 400 and "as a string" in r.json()["error"]
    assert len(env.db.games(limit=100)) == n
    r = env.client.post("/api/v1/play", headers=oh, json={"format": "Finals", "opponents": ["bot", "quickstart"]})
    assert r.status_code == 400 and r.json()["error"] == "Finals has 2 seats: yours and at most 1 opponent"
    r = env.client.post("/api/v1/play", headers=oh, json={"format": "Round1", "opponents": ["bot"] * 4})
    assert r.status_code == 400 and r.json()["error"].endswith("at most 3 opponents")
    r = env.client.post("/api/v1/play", headers=oh, json={"opponents": [], "fill": "bogus"})
    assert r.status_code == 400 and r.json()["error"] == "fill is \"quickstart\" or \"empty\", not 'bogus'"
    env.client.post("/api/v1/set_strategy", headers=oh, json={"name": "bobbot", "enabled": False})
    r = env.client.post("/api/v1/play", headers=oh, json={"opponents": []})
    assert r.status_code == 400 and "paused" in r.json()["error"]
    env.client.post("/api/v1/set_strategy", headers=oh, json={"name": "bobbot", "enabled": True,
                                                              "activate_version": b1, "title": "Bob's"})
    s = env.db.strategy("bobbot")
    assert s["active_version"] == b1 and s["title"] == "Bob's" and s["enabled"] == 1
    big = str(2 ** 64 - 1)                        # the seed of a game, passed back as the string game() gives
    r = env.client.post("/api/v1/play", headers=oh, json={"opponents": [], "seed": big}).json()
    assert env.client.get(f"/api/v1/game?game_id={r['game_id']}", headers=oh).json()["seed"] == big
    r = env.client.post("/api/v1/set_strategy", headers=oh, json={"name": "bot", "enabled": False})
    assert r.status_code == 400 and env.db.strategy("bot")["enabled"] == 1


def test_the_custom_game_form_uses_the_same_rules(env):
    user, _ = env.user()
    env.built(user, "bot", 1)
    sid = "s" * 40
    env.db.create_session(hash_token(sid), user["id"], 1, None, None)
    env.client.cookies.set(COOKIE, sid)
    form = {"format": "Finals", "seat0": "bot", "seat1": "quickstart", "seed": ""}
    r = env.client.post("/games/new", data=form)
    assert r.status_code == 303 and r.headers["location"].startswith("/games/")
    r = env.client.post("/games/new", data={**form, "seat1": "ghost"})
    assert "not%20available" in r.headers["location"]
    r = env.client.post("/games/new", data={**form, "seat0": "quickstart"})
    assert "real%20strategy" in r.headers["location"]


# Game analysis ------------------------------------------------------------------------------------------------
def test_game_timeline_and_game_log(env):
    user, token = env.user()
    env.built(user, "bot", 1)
    gid = env.finished_game([{"slot": 0, "strategy": "bot", "label": "bot"},
                             {"slot": 1, "strategy": "quickstart", "label": "quickstart"}])
    gdir = env.ax.game_dir(gid)
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "replay.dat").write_bytes(make_battle_replay())
    (gdir / "engine.log").write_text("engine started\nengine done\n")
    (gdir / "player-0.log").write_text("".join(f"tick {i} ok\n" for i in range(3000)) + "Traceback: boom\n")
    h = bearer(token)
    tl = env.client.get(f"/api/v1/game_timeline?game_id={gid}&every=10", headers=h).json()
    assert [s["tick"] for s in tl["samples"]] == [0, 10, 20, 25] and tl["seats"][0]["mine"]
    assert tl["events"][0] == {"tick": 2, "event": "base_destroyed", "slot": 1, "base": "BUILDER_BASE", "by": 0}
    assert tl["replay_json"].endswith(f"/games/{gid}/replay.json") and "--compressed" in tl["replay_json_shape"]
    log = env.client.get(f"/api/v1/game_log?game_id={gid}&tail_kb=1", headers=h).text
    assert log.startswith(f"# player-0.log of game #{gid} (slot 0, bot)") and log.endswith("Traceback: boom\n")
    assert len(log) < 1200
    hits = env.client.get(f"/api/v1/game_log?game_id={gid}&grep=traceback", headers=h).text
    assert hits.splitlines()[1] == "3001: Traceback: boom"
    many = env.client.get(f"/api/v1/game_log?game_id={gid}&grep=ok", headers=h).text.splitlines()
    assert "3000 of 3001 lines match" in many[0] and "the first 200 shown" in many[0] and len(many) == 201
    assert env.client.get(f"/api/v1/game_log?game_id={gid}&grep=[unclosed", headers=h).status_code == 200
    _, other_token = env.user("bob@example.com", "Bob")
    ob = bearer(other_token)
    r = env.client.get(f"/api/v1/game_log?game_id={gid}&slot=0", headers=ob)
    assert r.status_code == 400 and "owner only" in r.json()["error"]
    r = env.client.get(f"/api/v1/game_log?game_id={gid}", headers=ob)
    assert r.status_code == 400 and "no seat" in r.json()["error"]
    assert env.client.get(f"/api/v1/game_log?game_id={gid}&slot=engine", headers=ob).text.endswith("engine done\n")
    files = env.client.get(f"/api/v1/game?game_id={gid}", headers=ob).json()["files"]
    assert "player-0.log" not in files and "engine.log" in files
    queued = env.db.queue_game("Finals", None, user["id"], [{"slot": 0, "strategy": "bot"}, {"slot": 1, "strategy": None}])
    r = env.client.get(f"/api/v1/game_timeline?game_id={queued}", headers=h)
    assert r.status_code == 400 and "wait" in r.json()["error"]


def test_game_log_grep_is_plain_text_with_alternatives(env):
    """Security review, finding 8: a regular expression from the caller ran over a log the caller wrote, and
    (a|aa)+$ on a long line held the GIL for hours. grep is plain text now; | still separates alternatives."""
    import time
    from urllib.parse import quote

    user, token = env.user()
    env.built(user, "bot", 1)
    gid = env.finished_game([{"slot": 0, "strategy": "bot", "label": "bot"},
                             {"slot": 1, "strategy": "quickstart", "label": "quickstart"}])
    gdir = env.ax.game_dir(gid)
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "player-0.log").write_text("tick 1 ok\nx.y\nTraceback: boom\n" + "a" * 5000 + "!\nERROR (a|aa)+$\n")
    h = bearer(token)

    def grep(pattern):
        return env.client.get(f"/api/v1/game_log?game_id={gid}&grep={quote(pattern)}", headers=h).text.splitlines()

    started = time.monotonic()
    hits = grep("(a|aa)+$")          # the alternatives "(a" and "aa)+$", taken literally
    assert time.monotonic() - started < 2
    assert hits[0].endswith(": 1 of 5 lines match '(a|aa)+$'") and hits[1:] == ["5: ERROR (a|aa)+$"]
    assert grep(".")[1:] == ["2: x.y"]                                  # a dot is a dot
    assert grep("error|TRACEBACK")[1:] == ["3: Traceback: boom", "5: ERROR (a|aa)+$"]
    assert "0 of 5 lines match" in grep("t.ck")[0]


# MCP ------------------------------------------------------------------------------------------------------
def test_mcp_initialize_list_call_and_tool_errors(env):
    user, token = env.user()
    r = rpc(env, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "pytest", "version": "1"}})
    init = r.json()["result"]
    assert r.status_code == 200 and init["serverInfo"]["name"] == "codecraft" and "whoami()" in init["instructions"]
    tools = rpc(env, token, "tools/list", id_=2).json()["result"]["tools"]
    assert sorted(t["name"] for t in tools) == sorted(A.TOOLS) and len(tools) == 13
    by_name = {t["name"]: t for t in tools}
    assert by_name["whoami"]["annotations"]["readOnlyHint"] and not by_name["play"]["annotations"]["readOnlyHint"]
    assert by_name["play"]["inputSchema"]["required"] == ["opponents"]
    r = rpc(env, token, "tools/call", {"name": "whoami", "arguments": {}}, id_=3).json()["result"]
    assert not r.get("isError") and '"name":"Alice"' in r["content"][0]["text"]
    r = rpc(env, token, "tools/call", {"name": "game", "arguments": {"game_id": 999}}, id_=4).json()["result"]
    assert r["isError"] and "no game #999" in r["content"][0]["text"]
    r = rpc(env, token, "tools/call", {"name": "rules", "arguments": {"section": "Round specific"}}, id_=5).json()
    assert r["result"]["content"][0]["text"].startswith("## Round specific rules")
    res = rpc(env, token, "resources/list", id_=6).json()["result"]["resources"]
    assert {x["uri"] for x in res} == {"codecraft://rules", "codecraft://replay-format"}
    r = rpc(env, token, "resources/read", {"uri": "codecraft://replay-format"}, id_=7).json()
    assert r["result"]["contents"][0]["text"].startswith("# Official replay format")


def test_tools_with_a_name_argument_work_over_mcp(env):
    """The live run of 2026-11-05: the wrappers' helper had a parameter `name` of its own, so every tool
    with a `name` argument failed over MCP while REST worked."""
    user, token = env.user()
    env.built(user, "bot", 1)

    def call(name, args, id_):
        return rpc(env, token, "tools/call", {"name": name, "arguments": args}, id_=id_).json()["result"]

    r = call("strategy", {"name": "bot"}, 1)
    assert not r.get("isError") and '"versions":' in r["content"][0]["text"]
    r = call("set_strategy", {"name": "bot", "title": "Bot"}, 2)
    assert not r.get("isError") and env.db.strategy("bot")["title"] == "Bot"
    r = call("submit_strategy", {"name": "bot", "language": "python", "git_url": "https://github.com/x/y"}, 3)
    assert not r.get("isError") and '"number":2' in r["content"][0]["text"]
    r = call("play", {"opponents": ["quickstart"], "format": "Finals"}, 4)
    assert not r.get("isError") and '"game_id":' in r["content"][0]["text"]


def test_play_takes_the_seed_as_a_string_over_mcp(env):
    """The live check of 2026-11-05: a seed passed as a JSON number lost its last digits in the client and
    the game ran on another map. The schema now asks for a string; a big number is refused with the reason."""
    user, token = env.user()
    env.built(user, "bot", 1)
    tools = rpc(env, token, "tools/list", {}, id_=1).json()["result"]["tools"]
    seed_schema = next(t for t in tools if t["name"] == "play")["inputSchema"]["properties"]["seed"]
    assert "integer" not in json.dumps(seed_schema) and "string" in json.dumps(seed_schema)

    def play(seed, id_):
        args = {"opponents": [], "format": "Finals", "seed": seed}
        return rpc(env, token, "tools/call", {"name": "play", "arguments": args}, id_=id_).json()["result"]

    r = play(11249517513654704000, 2)
    assert r.get("isError") and "as a string" in r["content"][0]["text"]
    for i, (sent, kept) in enumerate([("11249517513654704893", "11249517513654704893"), (7, "7")]):
        r = play(sent, 3 + i)
        assert not r.get("isError"), r
        game_id = json.loads(r["content"][0]["text"])["game_id"]
        assert str(env.db.game(game_id)[0]["seed"]) == kept


def test_strategy_lists_a_game_once_when_it_held_several_seats(env):
    """The live check of 2026-11-05: mcp-check played v2 against its own v1 and strategy() listed the game twice."""
    user, token = env.user()
    v1, v2 = env.built(user, "bot", 2)
    h = {"Authorization": f"Bearer {token}"}
    r = env.client.post("/api/v1/play", headers=h, json={"opponents": [f"bot@{v1}", "bot"], "format": "Round2"})
    assert r.status_code == 200, r.text
    games = env.client.get("/api/v1/strategy?name=bot", headers=h).json()["games"]
    assert [g["id"] for g in games] == [r.json()["game_id"]]
    assert [s["strategy"] for s in games[0]["seats"]][:3] == ["bot", "bot", "bot"]


def test_every_mcp_wrapper_matches_its_tool_function(env):
    for t in env.app.state.mcp._tool_manager.list_tools():
        mine = [(p.name, p.default) for p in inspect.signature(t.fn).parameters.values() if p.name != "ctx"]
        theirs = [(p.name, p.default) for p in list(inspect.signature(A.TOOLS[t.name].fn).parameters.values())[2:]]
        assert mine == theirs, t.name
        assert t.description and len(t.description) > 80, t.name


# Logs and error reports ---------------------------------------------------------------------------------------
def test_tokens_and_tickets_are_masked_in_logs_and_error_reports():
    token = "cc_" + "Ab3" * 13 + "x"
    event = monitoring.scrub_event({"request": {"url": f"https://h/mcp/{token}/"},
                                    "message": f"PUT /mcp/upload/abcDEF123ghi456JKL789mno012PQR3/strategy.zip and {token}"})
    assert event["request"]["url"] == "https://h/mcp/[token]/"
    assert event["message"] == "PUT /mcp/upload/[token]/strategy.zip and [token]"
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("1.2.3.4:5", "POST", f"/mcp/{token}/", "1.1", 200), None)
    monitoring.RedactFilter().filter(record)
    assert record.getMessage() == '1.2.3.4:5 - "POST /mcp/***/ HTTP/1.1" 200' and len(record.args) == 5
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "GET %s", ("/mcp/upload/T1ck3t/strategy.zip",), None)
    monitoring.RedactFilter().filter(record)
    assert record.getMessage() == "GET /mcp/upload/***/strategy.zip"
    assert monitoring.redact_url_secrets("POST /mcp/ HTTP/1.1") == "POST /mcp/ HTTP/1.1"
