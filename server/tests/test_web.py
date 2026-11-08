"""The web app through FastAPI's TestClient: the pages a signed-in user opens must render."""
import json
import re

from fastapi.testclient import TestClient

from codecraft.auth import Auth
from codecraft.config import Config
from codecraft.db import DB
from codecraft.web import COOKIE, create_app


class FakeMailer:
    def __init__(self):
        self.sent = []

    def send(self, to, subject, text, html):
        self.sent.append((to, subject, text))


def make(tmp_path):
    cfg = Config(Config.load(tmp_path).data, tmp_path)
    cfg.data["auth"]["base_url"] = "http://testserver"
    cfg.data["auth"]["cookie_secure"] = False
    cfg.resolve_gpu(None)                       # no docker in the tests
    cfg.ensure_dirs()
    db = DB(tmp_path / "t.db")
    auth = Auth(cfg, db, FakeMailer())
    return TestClient(create_app(cfg, db, auth), follow_redirects=False), auth, db


def sign_in(client, auth, email="alice@example.com"):
    auth.request_code(email, "1.1.1.1")
    code = re.search(r"\b(\d{6})\b", auth.mailer.sent[-1][1]).group(1)
    user, token = auth.login(email, code, "1.1.1.1", "tests", name="Alice")
    client.cookies.set(COOKIE, token)
    return user


def test_public_pages_render_and_submit_needs_a_sign_in(tmp_path):
    client, auth, db = make(tmp_path)
    assert client.get("/leaderboard").status_code == 200
    assert client.get("/login").status_code == 200
    r = client.get("/submit")
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")


def test_submit_form_renders_with_a_suggested_name(tmp_path):
    """The live 500 of 2026-11-05: the template variable `name` collided with render()'s own parameter."""
    client, auth, db = make(tmp_path)
    sign_in(client, auth)
    r = client.get("/submit")
    assert r.status_code == 200 and 'name="name" value=""' in r.text
    r = client.get("/submit?name=my-bot")
    assert r.status_code == 200 and 'name="name" value="my-bot"' in r.text
    assert client.get("/me").status_code == 200


# Hardening (security review, findings 10 and the Low ones) -------------------------------------------------------
def test_next_stays_on_the_site(tmp_path):
    from codecraft.web import safe_next

    for good in ("/", "/games/5", "/strategies/x?tab=1", "/a%2F/b"):
        assert safe_next(good) == good
    for bad in ("//evil.com", "///evil.com", "/\\evil.com", "https://evil.com", "evil.com", "/\t/evil.com", "", None):
        assert safe_next(bad) == "/"
    client, auth, db = make(tmp_path)
    sign_in(client, auth)
    assert client.get("/login?next=//evil.com").headers["location"] == "/"
    assert client.get("/login?next=/games").headers["location"] == "/games"
    # the code form keeps a sanitised target, and the sign-in itself redirects there
    client.cookies.clear()
    r = client.post("/login/code", data={"email": "bob@example.com", "next": "//evil.com"})
    assert r.status_code == 303 and "next=/&" in r.headers["location"]
    code = re.search(r"\b(\d{6})\b", auth.mailer.sent[-1][1]).group(1)
    r = client.post("/login", data={"email": "bob@example.com", "code": code, "next": "///evil.com", "name": "Bob"})
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_security_headers_on_pages_refusals_and_the_agent_routes(tmp_path):
    client, auth, db = make(tmp_path)
    expected = {"x-content-type-options": "nosniff", "referrer-policy": "same-origin", "x-frame-options": "DENY"}
    for r in (client.get("/leaderboard"),
              client.post("/logout", headers={"origin": "https://evil.example"}),     # the cross-site refusal
              client.get("/api/v1/whoami"),                                            # no token: 401
              client.get("/static/does-not-exist.css")):
        assert {k: r.headers.get(k) for k in expected} == expected, r.status_code
        assert "strict-transport-security" not in r.headers                 # http base URL: no HSTS
    cfg = Config(Config.load(tmp_path).data, tmp_path)
    cfg.data["auth"]["cookie_secure"] = True
    cfg.resolve_gpu(None)
    https = TestClient(create_app(cfg, db, auth), follow_redirects=False)
    assert https.get("/leaderboard").headers["strict-transport-security"] == "max-age=31536000"


def test_the_client_address_is_the_peer_unless_a_trusted_proxy_forwards_it(tmp_path):
    """A sign-in from anywhere used to record (and rate-limit) whatever X-Forwarded-For said."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    client, auth, db = make(tmp_path)

    def sign_in_over_http(c, email):
        c.post("/login/code", data={"email": email}, headers={"x-forwarded-for": "6.6.6.6"})
        code = re.search(r"\b(\d{6})\b", auth.mailer.sent[-1][1]).group(1)
        r = c.post("/login", data={"email": email, "code": code, "name": "X"}, headers={"x-forwarded-for": "6.6.6.6"})
        assert r.status_code == 303 and COOKIE in r.cookies
        return db.one("SELECT s.ip FROM sessions s JOIN users u ON u.id = s.user_id WHERE u.email=?", email)["ip"]

    assert sign_in_over_http(client, "carol@example.com") == "testclient"
    # behind the configured proxy (what uvicorn installs for web.trusted_proxies) the forwarded address counts
    app = client.app
    proxied = TestClient(ProxyHeadersMiddleware(app, trusted_hosts=["testclient"]), follow_redirects=False)
    assert sign_in_over_http(proxied, "dave@example.com") == "6.6.6.6"
    untrusted = TestClient(ProxyHeadersMiddleware(app, trusted_hosts=["10.66.0.1"]), follow_redirects=False)
    assert sign_in_over_http(untrusted, "erin@example.com") == "testclient"


def test_trusted_proxies_come_from_the_config_or_the_environment(tmp_path, monkeypatch):
    assert Config.load(tmp_path)["web"]["trusted_proxies"] == []
    monkeypatch.setenv("CW_TRUSTED_PROXIES", " 10.66.0.1, 10.0.0.0/8 ,")
    assert Config.load(tmp_path)["web"]["trusted_proxies"] == ["10.66.0.1", "10.0.0.0/8"]


# The replay viewer: the local runner's, framed by the game page and the cover ------------------------------------
def test_a_replay_plays_in_the_local_runners_viewer(tmp_path):
    """/games/<id>/viewer is the runner's own page (sim-cpp/viewer/viewer.html) with the game's data in place of the
    runner's null; the data is inert in its <script> whatever the labels hold; this origin may frame it, and the game
    page and the cover do."""
    from test_replay import make_battle_replay

    from codecraft.web import VIEWER, VIEWER_SLOT

    client, auth, db = make(tmp_path)
    seats = [{"slot": 0, "strategy": "bot", "label": "</script><b>bot"}, {"slot": 1, "strategy": "quickstart",
                                                                           "label": "quickstart"}]
    gid = db.queue_game("Finals", 7, None, seats, kind="sandbox", rated=False)
    db.finish_game(gid, "finished", None, 40.0, 7, [{**s, "score": 10} for s in seats], {"initial": 1200, "k": 32},
                   [8, 4, 2, 1], rated=False)
    db.set_game_engine(gid, "codecraft/sim-cpp:0123456789ab")
    assert client.get(f"/games/{gid}/viewer").status_code == 404             # no replay yet
    gdir = tmp_path / "data" / "games" / f"{gid:06d}"
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / "replay.dat").write_bytes(make_battle_replay())

    r = client.get(f"/games/{gid}/viewer?autoplay=true&speed=20")
    assert r.status_code == 200 and r.headers["x-frame-options"] == "SAMEORIGIN"
    page = r.text
    runner = VIEWER.read_text()
    assert VIEWER_SLOT in runner and VIEWER_SLOT not in page
    data = re.search(r'<script id="cc-site" type="application/json">(.*?)</script>', page).group(1)
    assert "<" not in data and json.loads(data) == {
        "game": gid, "page": f"/games/{gid}", "replay": f"/games/{gid}/replay.json",
        "download": f"/games/{gid}/files/replay.dat", "labels": ["</script><b>bot", "quickstart"], "seed": "7",
        "engine": "codecraft/sim-cpp:0123456789ab", "autoplay": True, "speed": 20}
    assert "Engine version <code>0123456789ab</code>" in page
    assert json.loads(client.get(f"/games/{gid}/viewer?speed=7").text.split('type="application/json">')[1]
                      .split("</script>")[0])["speed"] is None                      # not one of the viewer's speeds

    # what the viewer reads of replay.json (loadReplay)
    rep = client.get(f"/games/{gid}/replay.json").json()
    assert {"map_size", "fog_of_war", "max_tick_count", "properties", "players", "entities", "actions",
            "ticks"} <= rep.keys()
    assert rep["ticks"][0]["t"] == 1 and [p[1] for p in rep["players"]] == [0, 1]

    game = client.get(f"/games/{gid}").text
    assert f'<iframe class="viewer" src="/games/{gid}/viewer"' in game and "viewer.js" not in game
    cover = client.get("/").text
    assert f'src="/games/{gid}/viewer?autoplay=true&amp;speed=20"' in cover
    assert client.get(f"/games/{gid + 1}/viewer").status_code == 404
