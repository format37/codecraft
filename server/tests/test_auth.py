import re

import pytest

from codecraft.auth import Auth, AuthError, Mailer
from codecraft.config import Config
from codecraft.db import DB


class FakeMailer:
    def __init__(self):
        self.sent = []

    def send(self, to, subject, text, html):
        self.sent.append((to, subject, text))

    def last_code(self):
        return re.search(r"\b(\d{6})\b", self.sent[-1][1]).group(1)


def make(tmp_path, **auth):
    cfg = Config(Config.load(tmp_path).data, tmp_path)
    cfg.data["auth"].update(auth)
    db = DB(tmp_path / "t.db")
    mailer = FakeMailer()
    return Auth(cfg, db, mailer), db, mailer


def test_code_flow_creates_account_and_session(tmp_path):
    auth, db, mailer = make(tmp_path, admins=["Boss@Example.com"])
    auth.request_code("Alice@Example.com ", "1.1.1.1")
    assert mailer.sent[-1][0] == "alice@example.com"
    code = mailer.last_code()
    with pytest.raises(AuthError):
        auth.login("alice@example.com", "000000", "1.1.1.1", None)
    user, token = auth.login("alice@example.com", code, "1.1.1.1", "ua", name="Alice")
    assert user["role"] == "user" and user["name"] == "Alice"
    assert auth.user_from_token(token)["email"] == "alice@example.com"
    # the code is spent
    with pytest.raises(AuthError):
        auth.login("alice@example.com", code, "1.1.1.1", None)
    auth.logout(token)
    assert auth.user_from_token(token) is None
    # configured admins get the role on sign-in
    auth.request_code("boss@example.com", "1.1.1.2")
    boss, _ = auth.login("boss@example.com", mailer.last_code(), "1.1.1.2", None, name="")
    assert boss["role"] == "admin" and boss["name"] == "boss"


def test_new_account_is_asked_for_a_name_but_an_existing_one_is_not(tmp_path):
    """The mail link submits the code without a name: a new address gets the name step (the code
    stays valid), a known address is signed in at once."""
    auth, db, mailer = make(tmp_path)
    auth.request_code("new@b.co", "1.1.1.1")
    code = mailer.last_code()
    with pytest.raises(AuthError) as exc:
        auth.login("new@b.co", code, "1.1.1.1", None)
    assert exc.value.code == "name_required" and db.user_by_email("new@b.co") is None
    user, _ = auth.login("new@b.co", code, "1.1.1.1", None, name="  Newbie ")
    assert user["name"] == "Newbie"
    auth.request_code("new@b.co", "1.1.1.1")
    again, token = auth.login("new@b.co", mailer.last_code(), "1.1.1.1", None)
    assert again["id"] == user["id"] and auth.user_from_token(token)["name"] == "Newbie"


def test_wrong_answers_kill_the_code(tmp_path):
    auth, db, mailer = make(tmp_path, code_attempts=2)
    auth.request_code("a@b.co", "1.1.1.1")
    code = mailer.last_code()
    for _ in range(2):
        with pytest.raises(AuthError):
            auth.login("a@b.co", "123456" if code != "123456" else "654321", "1.1.1.1", None)
    with pytest.raises(AuthError):  # right code, but dead
        auth.login("a@b.co", code, "1.1.1.1", None)


def test_invite_mode(tmp_path):
    auth, db, mailer = make(tmp_path, registration="invite")
    auth.request_code("new@b.co", "1.1.1.1")
    code = mailer.last_code()
    with pytest.raises(AuthError) as exc:
        auth.login("new@b.co", code, "1.1.1.1", None)
    assert exc.value.code == "invite_required"
    invite = auth.make_invite(None, "friend")
    user, _ = auth.login("new@b.co", code, "1.1.1.1", None, invite=invite, name="")
    assert user["email"] == "new@b.co"
    assert db.invite(invite)["used_by"] == user["id"]
    # a used invite does not admit a second person
    auth.request_code("other@b.co", "1.1.1.1")
    with pytest.raises(AuthError):
        auth.login("other@b.co", mailer.last_code(), "1.1.1.1", None, invite=invite, name="")


def test_closed_registration_mails_nothing_to_strangers(tmp_path):
    auth, db, mailer = make(tmp_path, registration="closed")
    auth.request_code("stranger@b.co", "1.1.1.1")
    assert mailer.sent == []


def test_rate_limit_per_address(tmp_path):
    auth, db, mailer = make(tmp_path)
    for i in range(5):
        auth.request_code("x@b.co", f"1.1.1.{i}")
    with pytest.raises(AuthError) as exc:
        auth.request_code("x@b.co", "9.9.9.9")
    assert exc.value.code == "rate_limited"


def test_bad_email_is_refused(tmp_path):
    auth, db, mailer = make(tmp_path)
    with pytest.raises(AuthError):
        auth.request_code("not an address", "1.1.1.1")


def test_mailer_defaults_to_console_without_keys(tmp_path):
    cfg = Config(Config.load(tmp_path).data, tmp_path)
    assert Mailer(cfg).provider == "console"
