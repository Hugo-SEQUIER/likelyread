"""A password sign-in for one reviewer account, for the Claude and ChatGPT directory
reviews: their reviewers sign in to a test account and cannot get past Google's 2-step
verification. Off unless REVIEW_EMAIL and REVIEW_PASSWORD are set (cloud mode only). It
signs in that one address and no other, on Pro, with sample emails the first time. While
it is on, an app's sign-in (oauth.py) shows a page with Google and this form instead of
going straight to Google. Unset both once the reviews are over.
"""

from __future__ import annotations

import hmac
import html
import json
import time
import uuid
from typing import Mapping

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response

from app import RateLimiter
from cloud import Cloud
from oauth import origin_of, page, read_form

MIN_PASSWORD = 16
ATTEMPTS_PER_MIN = 10  # per IP, right or wrong
PLAN_DAYS = 365  # the account stays on Pro this long after each sign-in
SUB = "review:"  # google_sub of the reviewer's account: Google's are digits, so never a clash

# Sample emails: subject, recipient, campaign, days ago, and hits as (seconds after the
# send, user agent, network). Recipients use the reserved .example domain. A hit within
# 20 s of sending is the sender's own compose window (a self-view), as the first one shows.
GMAIL = "Mozilla/5.0 (Windows NT 5.1; rv:11.0) Gecko Firefox/11.0 (via ggpht.com GoogleImageProxy)"
OUTLOOK = "Microsoft Office/16.0 (Windows NT 10.0; Microsoft Outlook 16.0.17928; Pro)"
APPLE = "Mozilla/5.0"  # Apple Mail: a prefetch from Apple's network, an open from elsewhere
SAMPLE = [
    ("Proposal for Harbourline", "maya@harbourline.example", "Q4 outreach", 19,
     [(8, OUTLOOK, "other"), (2520, GMAIL, "google"), (86400, GMAIL, "google")]),
    ("Quick question about your Q4 roadmap", "lee@northwind.example", "Q4 outreach", 18, [(48, APPLE, "apple")]),
    ("Intro: Likelyread x Fernhill", "sam@fernhill.example", "Q4 outreach", 16, [(5400, GMAIL, "google")]),
    ("Following up on the proposal", "maya@harbourline.example", "Q4 outreach", 12,
     [(900, OUTLOOK, "other"), (3600, OUTLOOK, "other")]),
    ("Pilot pricing for your team", "ines@corbel.example", "Q4 outreach", 9, []),
    ("Thanks for the call today", "lee@northwind.example", "Partner follow-ups", 15, [(1800, APPLE, "other")]),
    ("Next steps on the integration", "dana@quarry.example", "Partner follow-ups", 11,
     [(65, APPLE, "apple"), (7200, APPLE, "other")]),
    ("Shared folder for the launch", "omar@tidewater.example", "Partner follow-ups", 6, [(600, GMAIL, "google")]),
    ("Partner newsletter, October", "team@quarry.example", "Partner follow-ups", 4, []),
    ("October investor update", "nora@bluepine.example", "Investor update", 8,
     [(2400, GMAIL, "google"), (9000, OUTLOOK, "other")]),
    ("October investor update", "arjun@kestrel.example", "Investor update", 8, [(90, APPLE, "apple")]),
    ("Invoice 2026-041", "billing@corbel.example", None, 3, [(14400, OUTLOOK, "other")]),
    ("Coffee next week?", "jules@fernhill.example", None, 1, []),
]


def settings(env: Mapping[str, str]) -> tuple[str, str] | None:
    """(email, password), or None when the reviewer sign-in is off. Half a setting, or a
    short password, stops the server rather than leaving a weak way in."""
    email = (env.get("REVIEW_EMAIL") or "").strip().lower()
    password = env.get("REVIEW_PASSWORD") or ""
    if not email and not password:
        return None
    if "@" not in email or len(password) < MIN_PASSWORD:
        raise RuntimeError(f"the reviewer sign-in needs REVIEW_EMAIL and a REVIEW_PASSWORD of {MIN_PASSWORD}+ characters")
    return email, password


class Review:
    def __init__(self, cloud: Cloud, email: str, password: str) -> None:
        if email in cloud.cfg.admin_emails:
            raise RuntimeError("REVIEW_EMAIL must not be in ADMIN_EMAILS: reviewers would see every account")
        self.cloud = cloud
        self.email = email
        self.password = password
        self.limiter = RateLimiter(ATTEMPTS_PER_MIN)

    def form(self, google_url: str, error: str = "", status: int = 200) -> Response:
        """The sign-in page: Google, or the reviewer's email and password."""
        err = f'\n      <p class="error" role="alert">{html.escape(error)}</p>' if error else ""
        body = f"""<h1 class="login-title">Sign in</h1>
      <a class="btn primary google" href="{html.escape(google_url)}">Continue with Google</a>
      <form method="post" action="/auth/review" class="review-form">
        <p class="muted small">App reviewers: sign in with the test account.</p>
        <label for="review-email">Email</label>
        <input id="review-email" name="email" type="email" autocomplete="username" required>
        <label for="review-password">Password</label>
        <input id="review-password" name="password" type="password" autocomplete="current-password" required>
        <button class="btn" type="submit">Sign in</button>
      </form>{err}"""
        resp = page("Sign in", body, status)
        # With no-referrer, the default, browsers send "Origin: null" with the form; this
        # policy lets them send this site's origin, which review_sign_in checks.
        resp.headers["Referrer-Policy"] = "same-origin"
        return resp

    def account(self) -> str:
        """The reviewer's account, on Pro, with the sample emails when it has none."""
        db, now = self.cloud.db, time.time()
        with db.lock:
            row = db.conn.execute("SELECT id FROM accounts WHERE google_sub = ?", (SUB + self.email,)).fetchone()
            if row:
                acct = row["id"]
                db.conn.execute("UPDATE accounts SET email = ?, plan = 'pro', plan_until = ? WHERE id = ?",
                                (self.email, now + PLAN_DAYS * 86400, acct))
            else:
                acct = str(uuid.uuid4())
                db.conn.execute(
                    "INSERT INTO accounts(id, email, google_sub, created_at, plan, plan_until) VALUES (?, ?, ?, ?, 'pro', ?)",
                    (acct, self.email, SUB + self.email, now, now + PLAN_DAYS * 86400),
                )
            if not db.conn.execute("SELECT 1 FROM emails WHERE account_id = ? LIMIT 1", (acct,)).fetchone():
                self._seed(acct, now)
        return acct

    def _seed(self, acct: str, now: float) -> None:
        conn = self.cloud.db.conn
        for i, (subject, to, campaign, days, hits) in enumerate(SAMPLE):
            email_id, sent = str(uuid.uuid4()), now - days * 86400 - (i % 5) * 3700
            conn.execute(
                "INSERT INTO emails(id, subject, recipients, sent_at, campaign, sender, account_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (email_id, subject, json.dumps([to]), sent, campaign, self.email, acct, sent),
            )
            for after, ua, net in hits:
                conn.execute("INSERT INTO hits(email_id, ts, ip, ua, net) VALUES (?, ?, NULL, ?, ?)",
                             (email_id, sent + after, ua, net))

    def register_routes(self, app: FastAPI) -> None:
        cloud = self.cloud

        @app.get("/auth/review")
        def review_form():
            return self.form("/auth/google")

        @app.post("/auth/review")
        async def review_sign_in(request: Request):
            origin = request.headers.get("origin")
            if origin and origin != origin_of(cloud.cfg.public_url):
                return self.form("/auth/google", "Sign in from Likelyread's own page.", 403)
            saved = cloud._unsign(request.cookies.get("lr_oauth", ""))
            # An app's sign-in (oauth.py) carries on to its consent page, as after Google.
            app_request = saved.get("app") if saved and cloud.oauth is not None else None

            def again(error: str, status: int) -> Response:
                if app_request:  # a new Google link: the old one's state went with the cookie
                    return cloud.google_redirect(app=app_request, error=error, status=status)
                return self.form("/auth/google", error, status)

            if not self.limiter.allow(request.client.host if request.client else ""):
                return again("Too many attempts. Wait a minute and try again.", 429)
            form = await read_form(request) or {}
            right = hmac.compare_digest(form.get("email", "").strip().lower().encode(), self.email.encode())
            right &= hmac.compare_digest(form.get("password", "").encode(), self.password.encode())
            if not right:
                return again("Wrong email or password.", 401)
            acct = self.account()
            if app_request:
                resp = cloud.oauth.after_sign_in(app_request, (acct, self.email))
            else:
                token = cloud.create_token(acct, "Dashboard")
                resp = RedirectResponse(f"{cloud.cfg.public_url}/#connect={token}", status_code=303)
            resp.delete_cookie("lr_oauth", path="/auth")
            return resp
