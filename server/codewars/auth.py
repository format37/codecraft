"""Passwordless sign-in: a six-digit code mailed to the address, typed back (or opened through the
link in the mail, which signs an existing account in at once), then a session cookie. A new account is
asked for its name after the code is proven, so the link never needs a form. Modelled on Phonyx's door: nothing on these routes says
whether an address has an account, a code lives ten minutes and dies after a few wrong answers,
and the session lives in the database so that logout and admin revocation really invalidate it."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import smtplib
import time
import urllib.request
from collections import defaultdict, deque
from email.message import EmailMessage
from html import escape
from urllib.parse import quote, urlencode

from .config import Config
from .db import DB

log = logging.getLogger("codewars.auth")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CODE_DIGITS = 6


class AuthError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class RateLimiter:
    """Sliding window per key, in process memory."""

    def __init__(self, limit: int, window_s: float):
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and hits[0] < now - self.window_s:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        if len(self._hits) > 4096:
            for k in [k for k, h in self._hits.items() if not h or h[-1] < now - self.window_s]:
                del self._hits[k]
        return True


def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 200:
        raise AuthError("bad_email", "that does not look like an e-mail address")
    return email


def normalize_code(code: str) -> str:
    return "".join(ch for ch in (code or "") if ch.isdigit())


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def code_hash(email: str, code: str) -> str:
    return hashlib.sha256(f"{email}\0{code}".encode()).hexdigest()


# Mail ---------------------------------------------------------------------------------------
class Mailer:
    def __init__(self, cfg: Config):
        m = cfg["mail"]
        provider = m.get("provider") or "auto"
        if provider == "auto":
            if m.get("resend_api_key"):
                provider = "resend"
            elif (m.get("smtp") or {}).get("host"):
                provider = "smtp"
            else:
                provider = "console"
        self.provider = provider
        self.sender = m.get("from") or "codewars@localhost"
        self.api_key = m.get("resend_api_key")
        self.smtp = m.get("smtp") or {}
        if provider == "resend" and not self.api_key:
            raise ValueError("mail.provider is resend but no API key is configured (CW_RESEND_API_KEY)")
        if provider == "smtp" and not self.smtp.get("host"):
            raise ValueError("mail.provider is smtp but mail.smtp.host is not set")

    def send(self, to: str, subject: str, text: str, html: str) -> None:
        if self.provider == "console":
            log.info("mail(console) to=%s subject=%r\n%s", to, subject, text.strip())
            return
        if self.provider == "resend":
            payload = json.dumps({"from": self.sender, "to": [to], "subject": subject, "html": html, "text": text})
            req = urllib.request.Request("https://api.resend.com/emails", data=payload.encode(), method="POST",
                                         headers={"Authorization": f"Bearer {self.api_key}",
                                                  "Content-Type": "application/json",
                                                  "User-Agent": "codewars/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status >= 300:
                    raise RuntimeError(f"resend answered {resp.status}")
            return
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
        host, port = self.smtp["host"], int(self.smtp.get("port") or 587)
        with smtplib.SMTP(host, port, timeout=20) as s:
            if self.smtp.get("starttls", True):
                s.starttls()
            if self.smtp.get("user"):
                s.login(self.smtp["user"], self.smtp.get("password") or "")
            s.send_message(msg)


def render_code_mail(title: str, code: str, minutes: int, link: str) -> tuple[str, str, str]:
    subject = f"{code} is your {title} sign-in code"
    text = (f"Your {title} sign-in code:\n\n{code}\n\nIt is valid for {minutes} minutes.\n"
            f"Or open this link to sign in at once:\n{link}\n\n"
            "If you did not ask for it, ignore this message.")
    html = (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.6;'
        'color:#1a1a1d;max-width:520px">'
        f"<p>Your {escape(title)} sign-in code:</p>"
        f"<p style='font-size:32px;font-weight:700;letter-spacing:0.25em;font-family:Menlo,Consolas,monospace'>{code}</p>"
        f"<p>It is valid for {minutes} minutes.</p>"
        f"<p><a href=\"{escape(link)}\" style='display:inline-block;background:#2457d6;color:#fff;text-decoration:none;"
        "padding:10px 18px;border-radius:4px;font-weight:600'>Sign in</a></p>"
        "<p style='color:#5e5e65;font-size:13px'>The button signs you in at once; a new account is asked for its name.<br>"
        "If you did not ask for it, ignore this message.</p></div>"
    )
    return subject, text, html


# The door ------------------------------------------------------------------------------------
class Auth:
    def __init__(self, cfg: Config, db: DB, mailer: Mailer | None = None):
        self.cfg = cfg
        self.db = db
        self.mailer = mailer or Mailer(cfg)
        a = cfg["auth"]
        self.ttl_minutes = int(a.get("code_ttl_minutes") or 10)
        self.max_attempts = int(a.get("code_attempts") or 5)
        self.session_days = int(a.get("session_ttl_days") or 90)
        self.admins = {e.strip().lower() for e in (a.get("admins") or [])}
        self.code_ip_limiter = RateLimiter(10, 900)
        self.code_email_limiter = RateLimiter(5, 900)
        self.login_limiter = RateLimiter(15, 300)

    @property
    def registration(self) -> str:
        return str(self.cfg["auth"].get("registration") or "open")

    def code_link(self, email: str, code: str, invite: str | None = None) -> str:
        base = str(self.cfg["auth"].get("base_url") or "").rstrip("/")
        path = f"/login?invite={quote(invite)}" if invite else "/login"
        return f"{base}{path}#" + urlencode({"email": email, "code": code}, safe="@", quote_via=quote)

    def request_code(self, email: str, ip: str, invite: str | None = None) -> None:
        """Mail a code. Silent about whether the address has an account."""
        email = normalize_email(email)
        if not self.code_ip_limiter.allow(ip) or not self.code_email_limiter.allow(email) \
                or self.db.codes_issued_recently(email, hours=1) >= 5:
            raise AuthError("rate_limited", "too many codes requested, try again later")
        if self.registration == "closed" and self.db.user_by_email(email) is None and email not in self.admins:
            log.info("auth: no code for %s (registration closed)", email)
            return
        code = f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"
        self.db.issue_code(email, code_hash(email, code), self.ttl_minutes)
        subject, text, html = render_code_mail(str(self.cfg["web"].get("title") or "CodeWars"), code,
                                               self.ttl_minutes, self.code_link(email, code, invite))
        try:
            self.mailer.send(email, subject, text, html)
        except Exception as exc:  # noqa: BLE001 - the code row exists; the user can ask again
            log.error("auth: could not mail the code to %s: %s", email, exc)
            raise AuthError("mail_failed", "the code could not be sent; try again in a minute")

    def login(self, email: str, code: str, ip: str, user_agent: str | None, invite: str | None = None,
              name: str | None = None) -> tuple[dict, str]:
        """Prove the code; create the account on first sign-in. Returns (user, session token).
        A new account needs `name` (an empty string takes the mailbox name): with `name=None` the call
        raises `name_required` and leaves the code valid, so the caller can ask and come back."""
        if not self.login_limiter.allow(ip):
            raise AuthError("rate_limited", "too many attempts, try again later")
        email = normalize_email(email)
        code = normalize_code(code)
        if len(code) != CODE_DIGITS or not self.db.check_code(email, code_hash(email, code), self.max_attempts,
                                                              spend=False):
            raise AuthError("code_invalid", "wrong or expired code")
        user = self.db.user_by_email(email)
        is_admin = email in self.admins
        if user is None:
            if self.registration == "closed" and not is_admin:
                raise AuthError("closed", "registration is closed")
            if self.registration == "invite" and not is_admin:
                inv = self.db.invite(invite) if invite else None
                if inv is None or inv["used_by"] is not None:
                    raise AuthError("invite_required", "an invitation is required to create an account")
            if name is None:
                raise AuthError("name_required", "new account: a name is needed")
            self.db.check_code(email, code_hash(email, code), self.max_attempts, spend=True)
            user = self.db.create_user(email, (name or "").strip()[:60] or email.split("@")[0],
                                       "admin" if is_admin else "user")
            if self.registration == "invite" and not is_admin and invite:
                self.db.use_invite(invite, user["id"])
            log.info("auth: new account %s (%s)", email, user["role"])
        else:
            if user["disabled"]:
                raise AuthError("code_invalid", "wrong or expired code")
            self.db.check_code(email, code_hash(email, code), self.max_attempts, spend=True)
            self.db.touch_login(user["id"], "admin" if is_admin and user["role"] != "admin" else None)
            user = self.db.user(user["id"])
        token = secrets.token_urlsafe(32)
        self.db.create_session(hash_token(token), user["id"], self.session_days, (user_agent or "")[:300] or None, ip)
        return dict(user), token

    def user_from_token(self, token: str | None):
        if not token:
            return None
        row = self.db.session_user(hash_token(token))
        return dict(row) if row else None

    def logout(self, token: str | None) -> None:
        if token:
            self.db.delete_session(hash_token(token))

    def make_invite(self, created_by: int | None, note: str | None = None) -> str:
        code = secrets.token_urlsafe(12)
        self.db.create_invite(code, created_by, note)
        return code
