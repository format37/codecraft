"""What an error report may carry out of the building: `monitoring.scrub_*` are what `init` hands Sentry
as `before_send` / `before_breadcrumb`. Pure functions, so no DSN is needed here."""
import uuid
from pathlib import Path

from codecraft import monitoring
from codecraft.config import Config


def test_an_address_a_code_and_a_token_are_scrubbed_an_identifier_is_not():
    assert monitoring.scrub_text("auth: could not mail the code to alice@example.com: down") == (
        "auth: could not mail the code to [email]: down")
    assert monitoring.scrub_text("/login#email=a@b.io&code=123456&x=1") == "/login#email=[email]&code=[token]&x=1"
    assert monitoring.scrub_text("your code: 123456 (ten minutes)") == "your code: [code] (ten minutes)"
    assert monitoring.scrub_text("game 123456 seed 987654321") == "game 123456 seed 987654321"
    token = "Zm9vYmFyMTIzNDU2Nzg5MGFiY2RlZmdoaWprbG1ub3A"   # token_urlsafe(32)-shaped
    assert monitoring.scrub_text(f"cookie {token} rejected") == "cookie [token] rejected"
    assert monitoring.scrub_text("a" * 32 + "1" * 32) == "[token]"
    sid = str(uuid.uuid4())
    assert monitoring.scrub_text(f"session {sid} stuck") == f"session {sid} stuck"
    word = "supercalifragilisticexpialidocious_and_then_some"
    assert monitoring.scrub_text(word) == word


def test_an_event_is_scrubbed_in_its_text_fields_frames_and_request():
    event = {
        "event_id": "0123456789abcdef0123456789abcdef",
        "message": "auth: new account bob@example.com (user)",
        "exception": {"values": [{
            "type": "RuntimeError", "value": "mail to bob@example.com failed",
            "stacktrace": {"frames": [{"vars": {"email": "bob@example.com", "code": "123456",
                                                "resend_api_key": "re_x", "game_id": 7}}]}}]},
        "request": {"url": "https://x/login?code=123456", "query_string": "code=123456", "method": "POST"},
        "breadcrumbs": {"values": [
            {"category": "codecraft.auth", "message": "mail(console) to=bob@example.com subject='Your code'"},
            {"category": "codecraft.auth", "message": "auth: no code for bob@example.com (registration closed)"},
        ]},
    }
    out = monitoring.scrub_event(event)
    assert out["event_id"] == "0123456789abcdef0123456789abcdef"
    assert out["message"] == "auth: new account [email] (user)"
    exc = out["exception"]["values"][0]
    assert exc["value"] == "mail to [email] failed"
    assert exc["stacktrace"]["frames"][0]["vars"] == {"email": "[email]", "code": "[token]",
                                                      "resend_api_key": "[token]", "game_id": 7}
    assert out["request"]["url"] == "https://x/login?code=[token]"
    assert out["request"]["query_string"] == "code=[token]"
    # the console mailer's line (the whole mail, code included) is dropped, the other one scrubbed
    assert [c["message"] for c in out["breadcrumbs"]["values"]] == [
        "auth: no code for [email] (registration closed)"]


def test_init_is_off_without_a_dsn_and_configures_the_sdk_with_one(tmp_path, monkeypatch):
    cfg = Config(Config.load(tmp_path).data, tmp_path)
    assert cfg.data["sentry"]["dsn"] is None
    assert monitoring.init(cfg) is False

    import sentry_sdk
    calls = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: calls.append(kw))
    monkeypatch.setenv("CW_SENTRY_DSN", " https://public@o0.ingest.sentry.io/1 ")
    monkeypatch.setenv("CW_SENTRY_ENVIRONMENT", "staging")
    cfg = Config(Config.load(tmp_path).data, tmp_path)
    assert monitoring.init(cfg) is True
    (kw,) = calls
    assert kw["dsn"] == "https://public@o0.ingest.sentry.io/1" and kw["environment"] == "staging"
    assert kw["release"] == "codecraft@unknown"           # tmp_path is no git checkout
    assert kw["send_default_pii"] is False and kw["max_request_body_size"] == "never"
    assert kw["traces_sample_rate"] == 0.0
    assert kw["before_send"] is monitoring.before_send and kw["before_breadcrumb"] is monitoring.scrub_breadcrumb


def test_an_agents_mistake_is_not_sent_any_other_event_is_scrubbed():
    from mcp.server.fastmcp.exceptions import ToolError

    from codecraft.mcp_server import Refused

    def hint(exc):
        return {"exc_info": (type(exc), exc, None)}

    def wrapped(cause):                                 # as FastMCP raises a tool's exception
        outer = ToolError(f"Error executing tool play: {cause}")
        outer.__cause__ = cause
        return outer

    def sent(h):
        event = monitoring.before_send({"message": "for ann@example.com"}, h)
        assert event is None or event["message"] == "for [email]"
        return event is not None

    assert not sent(hint(wrapped(Refused("you own 2 strategies; say which one plays: mine=<name>"))))
    assert not sent(hint(ToolError("Unknown tool: nope")))
    assert not sent(hint(ValueError("Unknown resource: codecraft://nope")))
    assert sent(hint(wrapped(KeyError("status"))))      # a bug in a tool
    assert sent(hint(wrapped(ValueError("Unknown resource: x"))))   # only FastMCP's own, unwrapped
    assert sent(None) and sent({"exc_info": None}) and sent({})     # a log line
    looped = ToolError("x")
    looped.__cause__ = looped
    assert sent(hint(looped))


def test_release_names_the_checkout():
    repo = Path(__file__).resolve().parents[2]
    assert monitoring.release(Path("/nonexistent")) == "codecraft@unknown"
    assert (repo / ".git").exists() and monitoring.release(repo) != "codecraft@unknown"
