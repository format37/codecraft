"""Error monitoring (Sentry), optional: on when `sentry.dsn` / `CW_SENTRY_DSN` is set, otherwise the SDK
is never imported. Only unhandled exceptions and ERROR-level log records become events, from the `web`
and the `worker` process alike (`cli._context` inits it); tracing is off. `send_default_pii=False` keeps
cookies, headers and client addresses out, request bodies are never attached (a sign-in form carries the
address and the code, a submission a zip), and what the code itself puts into a message or a local
variable is scrubbed on the way out by `scrub_event` / `scrub_breadcrumb`: an e-mail address becomes
`[email]`, a sign-in code, session token or API key `[token]`. A coding agent's mistake (`agent_mistake`)
is not sent at all. The filters are pure functions with no SDK import, so the tests need no DSN. Modelled
on Phonyx's `monitoring.py`."""
from __future__ import annotations

import logging
import subprocess
import re
from pathlib import Path
from typing import Any

log = logging.getLogger("codecraft.monitoring")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# `code=…` / `token=…` in a query string or a URL fragment (the mail link), whatever the length.
PARAM_RE = re.compile(r"(?i)(\b(?:code|token|password|api_key)=)[^&\s\"'#]+")
# A six-digit sign-in code right after the word that introduces it ("code 123456", "code: 123456").
CODE_RE = re.compile(r"(?i)(\bcode\b\W{0,3})\d{6}\b")
# Any other opaque secret: 32+ URL-safe characters with a digit and a letter - a session token
# (`token_urlsafe(32)`), a sha256 code hash, a Resend key. A UUID is kept (an identifier); a docker
# image hash is scrubbed with the tokens, which is the safe direction.
OPAQUE_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# A coding agent's API token (`cc_` + 40 characters, agents.py) wherever it appears, and an upload ticket,
# which rides in the path /mcp/upload/<ticket>/ and is caught by its position. OPAQUE_RE would probably
# catch both; these make it certain.
API_TOKEN_RE = re.compile(r"\bcc_[A-Za-z0-9_-]{20,}")
TICKET_RE = re.compile(r"(/upload/)[^/\s\"'?#]+")
# For log lines: /mcp/<token>/ by position, whatever the token looks like (upload/ is a route, not a token).
MCP_PATH_RE = re.compile(r"(/mcp/)(?!upload/)[^/\s\"'?#]+")
# Local variables and dict keys whose value is a secret whatever its shape (the SDK's own scrubber
# already masks `token`, `password`, `secret`, `api_key`, `session`, …; these are ours).
SECRET_KEYS = {"code", "code_hash", "resend_api_key", "smtp_password", "cookie", "cookies", "authorization"}


def _opaque(match: re.Match) -> str:
    run = match.group(0)
    if UUID_RE.fullmatch(run) or not (any(c.isdigit() for c in run) and any(c.isalpha() for c in run)):
        return run
    return "[token]"


def scrub_text(text: str) -> str:
    text = EMAIL_RE.sub("[email]", text)
    text = PARAM_RE.sub(r"\1[token]", text)
    text = CODE_RE.sub(r"\1[code]", text)
    text = API_TOKEN_RE.sub("[token]", text)
    text = TICKET_RE.sub(r"\1[token]", text)
    return OPAQUE_RE.sub(_opaque, text)


# Log lines ------------------------------------------------------------------------------------------------
def redact_url_secrets(text: str) -> str:
    """A log line with the secrets of agent URLs masked: /mcp/<token>/ -> /mcp/***/, /upload/<ticket>/ ->
    /upload/***/, and a `cc_` token anywhere else."""
    text = TICKET_RE.sub(r"\1***", text)
    text = MCP_PATH_RE.sub(r"\1***", text)
    return API_TOKEN_RE.sub("cc_***", text)


def _redact_arg(arg: Any) -> Any:
    if isinstance(arg, str):
        return redact_url_secrets(arg)
    if arg is None or isinstance(arg, (int, float, bytes)):
        return arg
    text = str(arg)                      # a URL object, a Request: replaced only when it held a secret
    masked = redact_url_secrets(text)
    return arg if masked == text else masked


class RedactFilter(logging.Filter):
    """Masks agent secrets in log records; uvicorn's access log carries the request path. The message and
    each argument are edited in place, so uvicorn's access formatter still finds its five arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact_url_secrets(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(_redact_arg(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: _redact_arg(v) for k, v in record.args.items()}
        except Exception:  # noqa: BLE001 - a log filter must never break logging
            pass
        return True


def install_log_redaction() -> RedactFilter:
    """On uvicorn's access and error loggers (their records do not reach the root handlers) and on every
    root handler. Logger filters survive uvicorn's own logging config, which replaces handlers only."""
    redact = RedactFilter()
    for name in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).addFilter(redact)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redact)
    return redact


def _scrub_tree(value: Any, key: str | None = None) -> Any:
    """Every string inside a nested dict/list, in place where possible; a secret-named key whole."""
    if key is not None and key.lower() in SECRET_KEYS and value is not None:
        return "[token]"
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        for k, item in value.items():
            value[k] = _scrub_tree(item, k if isinstance(k, str) else None)
        return value
    if isinstance(value, list):
        for i, item in enumerate(value):
            value[i] = _scrub_tree(item)
        return value
    return value


# The event subtrees that carry text from the running program. The rest (`event_id`, `timestamp`,
# `release`, `sdk`, `contexts`) is the SDK's bookkeeping; `event_id` is 32 hex chars OPAQUE_RE would eat.
_TEXT_FIELDS = ("message", "logentry", "extra", "tags", "user")


def _scrub_frames(stacktrace: Any) -> None:
    if not isinstance(stacktrace, dict):
        return
    for frame in stacktrace.get("frames") or []:
        if isinstance(frame, dict) and "vars" in frame:
            frame["vars"] = _scrub_tree(frame["vars"])


def scrub_event(event: dict, hint: Any = None) -> dict:
    """`before_send`: the event with its text fields scrubbed, never None (dropping is not its job)."""
    for field in _TEXT_FIELDS:
        if field in event:
            event[field] = _scrub_tree(event[field])
    for section in ("exception", "threads"):
        for item in (event.get(section) or {}).get("values") or []:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("value"), str):
                item["value"] = scrub_text(item["value"])
            _scrub_frames(item.get("stacktrace"))
    _scrub_frames(event.get("stacktrace"))
    request = event.get("request")
    if isinstance(request, dict):
        for key in ("url", "query_string", "data"):
            if key in request:
                request[key] = _scrub_tree(request[key])
    crumbs = (event.get("breadcrumbs") or {}).get("values")
    if isinstance(crumbs, list):
        event["breadcrumbs"]["values"] = [c for c in (scrub_breadcrumb(c) for c in crumbs) if c is not None]
    return event


# What FastMCP raises itself for a call to a tool or resource it does not have (mcp 1.29).
UNKNOWN_CALLS = ("Unknown tool: ", "Unknown resource: ")


def agent_mistake(hint: Any) -> bool:
    """True when an event's exception is a coding agent's mistake, not the server's error. Sentry's MCP
    integration reports every exception out of a tool call or a resource read as unhandled, though the agent
    gets it as an error to act on: a call refused for its input, quotas or rights (an exception with a true
    `agent_mistake`, as `mcp_server.Refused`), arguments that do not fit the tool (FastMCP's model of them is
    `<tool>Arguments`), a tool or resource that does not exist. FastMCP wraps a tool's exception in a
    ToolError of its own, so the causes are followed; its refusal of an unknown name is never wrapped."""
    exc = ((hint or {}).get("exc_info") or (None, None, None))[1]
    if type(exc).__name__ in ("ToolError", "ValueError") and str(exc).startswith(UNKNOWN_CALLS):
        return True
    for _ in range(8):                                  # a chain of causes: short, and never a cycle
        if exc is None:
            return False
        if getattr(exc, "agent_mistake", False) is True:
            return True
        if type(exc).__name__ == "ValidationError" and str(getattr(exc, "title", "")).endswith("Arguments"):
            return True
        exc = exc.__cause__
    return False


def before_send(event: dict, hint: Any = None) -> dict | None:
    """`before_send`: an agent's mistake dropped, any other event scrubbed."""
    return None if agent_mistake(hint) else scrub_event(event, hint)


def scrub_breadcrumb(crumb: dict, hint: Any = None) -> dict | None:
    """`before_breadcrumb`: message and data scrubbed, category and level kept. The console mailer's
    log line (the whole sign-in mail, code included) is dropped rather than scrubbed."""
    if not isinstance(crumb, dict):
        return crumb
    message = crumb.get("message")
    if crumb.get("category") == "codecraft.auth" and isinstance(message, str) and message.startswith("mail("):
        return None
    if isinstance(message, str):
        crumb["message"] = scrub_text(message)
    if "data" in crumb:
        crumb["data"] = _scrub_tree(crumb["data"])
    return crumb


def release(root: Path) -> str:
    """`codecraft@<short sha>` of the checkout (the repository is mounted into the containers)."""
    try:
        sha = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(root), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        sha = ""
    return f"codecraft@{sha or 'unknown'}"


def init(cfg) -> bool:
    """Start the SDK when a DSN is configured; False, with nothing imported, otherwise."""
    section = cfg.data.get("sentry") or {}
    dsn = str(section.get("dsn") or "").strip()
    if not dsn:
        return False
    import sentry_sdk

    environment = str(section.get("environment") or "production").strip()
    version = release(cfg.root)
    sentry_sdk.init(dsn=dsn, environment=environment, release=version,
                    send_default_pii=False, max_request_body_size="never", traces_sample_rate=0.0,
                    before_send=before_send, before_breadcrumb=scrub_breadcrumb)
    log.info("monitoring: sentry on (%s, %s)", environment, version)
    return True
