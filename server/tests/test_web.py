"""The web app through FastAPI's TestClient: the pages a signed-in user opens must render."""
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
