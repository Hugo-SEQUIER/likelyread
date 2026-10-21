"""Likelyread Cloud: accounts, Google sign-in, plans and Stripe billing.

Loaded only when MODE=cloud. Every request is tied to an account through a
bearer token (one per signed-in browser or extension), and app.py scopes all
data to that account.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from app import DB, FEATURES, Principal

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id                   TEXT PRIMARY KEY,
  email                TEXT NOT NULL,
  google_sub           TEXT UNIQUE,
  created_at           REAL NOT NULL,
  plan                 TEXT NOT NULL DEFAULT 'free',   -- free | pro | lifetime
  plan_until           REAL,                           -- end of the paid period (pro)
  subscription_id      TEXT,
  subscription_status  TEXT,
  cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
  customer_id          TEXT,                           -- Stripe customer, created at first checkout
  lifetime_payment     TEXT                            -- Stripe PaymentIntent of the lifetime purchase
);
CREATE TABLE IF NOT EXISTS tokens (
  hash        TEXT PRIMARY KEY,   -- sha256 of the token; the token itself is never stored
  account_id  TEXT NOT NULL,
  label       TEXT,
  created_at  REAL NOT NULL,
  last_used   REAL
);
CREATE INDEX IF NOT EXISTS idx_tokens_account ON tokens(account_id);
CREATE TABLE IF NOT EXISTS webhook_events (
  id           TEXT PRIMARY KEY,
  received_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deleted_accounts (
  id          TEXT PRIMARY KEY,   -- so late billing events for a deleted account are recognised
  deleted_at  REAL NOT NULL
);
"""

PLANS = ("pro_monthly", "pro_yearly", "lifetime")
# A paid period keeps working this long after it ends, to cover renewal delays.
GRACE_SECONDS = 3 * 86400
MAX_TOKENS_PER_ACCOUNT = 20
# A token unused for this long stops working (each use refreshes it).
TOKEN_IDLE_SECONDS = 180 * 86400
# Dashboard tokens live in the browser's localStorage, so they lapse sooner.
DASHBOARD_TOKEN_IDLE_SECONDS = 30 * 86400
# Fallback paid period when a subscription event carries no readable period end.
DEFAULT_PERIOD_SECONDS = 32 * 86400
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
WEBHOOK_TOLERANCE = 300
EXTENSION_ID_RE = re.compile(r"^[a-p]{32}$")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _period_end(sub: dict) -> float | None:
    """When a Stripe subscription's paid period ends. API versions before
    2025-03-31 put it on the subscription, later ones on each item."""
    items = (sub.get("items") or {}).get("data") or []
    ends = [sub.get("current_period_end")] + [i.get("current_period_end") for i in items]
    ends = [float(e) for e in ends if isinstance(e, (int, float)) and e > 0]
    return max(ends) if ends else None


def stripe_form(value: Any, name: str = "") -> list[tuple[str, str]]:
    """Stripe's form encoding: nested dicts and lists become a[b][0][c]=v."""
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in stripe_form(v, f"{name}[{k}]" if name else str(k))]
    if isinstance(value, (list, tuple)):
        return [p for i, v in enumerate(value) for p in stripe_form(v, f"{name}[{i}]")]
    if value is None:
        return []
    return [(name, "true" if value is True else "false" if value is False else str(value))]


class CloudSettings:
    def __init__(self, env: dict | None = None) -> None:
        e = os.environ if env is None else env
        self.public_url = (e.get("PUBLIC_URL") or "").rstrip("/")
        self.session_secret = e.get("SESSION_SECRET", "")
        self.google_client_id = e.get("GOOGLE_CLIENT_ID", "")
        self.google_client_secret = e.get("GOOGLE_CLIENT_SECRET", "")
        self.google_auth_url = e.get("GOOGLE_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth")
        self.google_token_url = e.get("GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token")
        self.stripe_api_url = (e.get("STRIPE_API_URL") or "https://api.stripe.com").rstrip("/")
        self.stripe_secret_key = e.get("STRIPE_SECRET_KEY", "")
        self.stripe_webhook_secret = e.get("STRIPE_WEBHOOK_SECRET", "")
        self.price_ids = {
            "pro_monthly": e.get("STRIPE_PRICE_PRO_MONTHLY", ""),
            "pro_yearly": e.get("STRIPE_PRICE_PRO_YEARLY", ""),
            "lifetime": e.get("STRIPE_PRICE_LIFETIME", ""),
        }
        # Stripe Tax adds VAT and sales tax at checkout; off only while it is not set up.
        self.stripe_tax = e.get("STRIPE_TAX", "true").strip().lower() not in ("0", "false", "no", "off")
        self.prices = {
            "pro_monthly": e.get("PRICE_PRO_MONTHLY", "€5"),
            "pro_yearly": e.get("PRICE_PRO_YEARLY", "€36"),
            "lifetime": e.get("PRICE_LIFETIME", "€59"),
        }
        self.lifetime_cap = int(e.get("LIFETIME_CAP", "500"))
        self.admin_emails = {a.strip().lower() for a in e.get("ADMIN_EMAILS", "").split(",") if a.strip()}
        # Chrome extension ids allowed to receive the token after sign-in (the store
        # id, plus an unpacked id while developing). Any other ?ext= is ignored.
        self.extension_ids = {x.strip() for x in e.get("EXTENSION_IDS", "").split(",") if EXTENSION_ID_RE.match(x.strip())}

        problems = []
        if not (self.public_url.startswith("https://") or self.public_url.startswith(("http://localhost", "http://127.0.0.1"))):
            problems.append("PUBLIC_URL (https://...)")
        if len(self.session_secret) < 32:
            problems.append("SESSION_SECRET (32+ characters: openssl rand -hex 32)")
        if not self.google_client_id or not self.google_client_secret:
            problems.append("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET")
        if problems:
            raise RuntimeError("MODE=cloud needs " + ", ".join(problems))

    @property
    def billing_enabled(self) -> bool:
        return bool(self.stripe_secret_key and self.stripe_webhook_secret and all(self.price_ids.values()))

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url}/auth/google/callback"


def http_json(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, dict]:
    """Tiny JSON-over-HTTPS client. Replaced in tests."""
    req = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": "likelyread-cloud", **headers})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except ValueError:
            return e.code, {"raw": raw[:300].decode(errors="replace")}


class CheckoutIn(BaseModel):
    plan: str


class Cloud:
    def __init__(self, db: DB, cfg: CloudSettings) -> None:
        self.db = db
        self.cfg = cfg
        self.http: Callable[[str, str, dict, bytes | None], tuple[int, dict]] = http_json
        with db.lock:
            db.conn.executescript(SCHEMA)
            # Databases created before Stripe billing lack its two columns.
            have = {r[1] for r in db.conn.execute("PRAGMA table_info(accounts)")}
            for col in ("customer_id", "lifetime_payment"):
                if col not in have:
                    db.conn.execute(f"ALTER TABLE accounts ADD COLUMN {col} TEXT")
            db.conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_customer ON accounts(customer_id)")

    # ---- accounts and tokens -------------------------------------------

    def effective_plan(self, row: Any, now: float | None = None) -> str:
        now = time.time() if now is None else now
        if row["plan"] == "lifetime":
            return "pro"
        if row["plan"] == "pro" and row["plan_until"] and row["plan_until"] + GRACE_SECONDS > now:
            return "pro"
        return "free"

    def authenticate(self, token: str) -> Principal | None:
        rows = self.db.query(
            "SELECT a.*, t.hash, t.last_used, t.label, t.created_at AS created_at_t FROM tokens t JOIN accounts a ON a.id = t.account_id WHERE t.hash = ?",
            (token_hash(token),),
        )
        if not rows:
            return None
        row = rows[0]
        now = time.time()
        idle = DASHBOARD_TOKEN_IDLE_SECONDS if row["label"] == "Dashboard" else TOKEN_IDLE_SECONDS
        if (row["last_used"] or row["created_at_t"]) < now - idle:
            self.db.execute("DELETE FROM tokens WHERE hash = ?", (row["hash"],))
            return None
        if not row["last_used"] or row["last_used"] < now - 3600:
            self.db.execute("UPDATE tokens SET last_used = ? WHERE hash = ?", (now, row["hash"]))
        return Principal(account_id=row["id"], plan=self.effective_plan(row, now), email=row["email"])

    def upsert_google_account(self, sub: str, email: str) -> str:
        email = email.strip().lower()
        with self.db.lock:
            row = self.db.conn.execute("SELECT id, email FROM accounts WHERE google_sub = ?", (sub,)).fetchone()
            if row:
                if row["email"] != email:
                    self.db.conn.execute("UPDATE accounts SET email = ? WHERE id = ?", (email, row["id"]))
                return row["id"]
            acct = str(uuid.uuid4())
            self.db.conn.execute(
                "INSERT INTO accounts(id, email, google_sub, created_at) VALUES (?, ?, ?, ?)",
                (acct, email, sub, time.time()),
            )
            return acct

    def create_token(self, account_id: str, label: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.db.lock:
            self.db.conn.execute(
                "INSERT INTO tokens(hash, account_id, label, created_at) VALUES (?, ?, ?, ?)",
                (token_hash(token), account_id, label, time.time()),
            )
            # Keep the newest few: every sign-in makes one.
            self.db.conn.execute(
                "DELETE FROM tokens WHERE account_id = ? AND hash NOT IN "
                "(SELECT hash FROM tokens WHERE account_id = ? ORDER BY created_at DESC LIMIT ?)",
                (account_id, account_id, MAX_TOKENS_PER_ACCOUNT),
            )
        return token

    def account(self, account_id: str) -> Any:
        rows = self.db.query("SELECT * FROM accounts WHERE id = ?", (account_id,))
        return rows[0] if rows else None

    def lifetime_left(self) -> int:
        n = self.db.query("SELECT COUNT(*) AS n FROM accounts WHERE plan = 'lifetime'")[0]["n"]
        return max(0, self.cfg.lifetime_cap - n)

    def public_config(self) -> dict:
        return {
            "google": True,
            "billing": self.cfg.billing_enabled,
            "prices": self.cfg.prices,
            "lifetime_left": self.lifetime_left(),
            "free": FEATURES["free"],
            "pro": FEATURES["pro"],
        }

    def me(self, p: Principal) -> dict:
        row = self.account(p.account_id or "")
        if row is None:
            raise HTTPException(status_code=401, detail="invalid token")
        today = self.db.query(
            "SELECT COUNT(*) AS n FROM emails WHERE account_id = ? AND created_at >= ?", (row["id"], time.time() - 86400)
        )[0]["n"]
        return {
            "mode": "cloud",
            "email": row["email"],
            "plan": p.plan,
            "billing_plan": row["plan"],
            "plan_until": row["plan_until"],
            "cancel_at_period_end": bool(row["cancel_at_period_end"]),
            "features": p.features,
            "usage": {"today": today, "daily_limit": p.features["daily_limit"]},
            "billing": self.cfg.billing_enabled,
            "prices": self.cfg.prices,
            "lifetime_left": self.lifetime_left(),
            "admin": row["email"] in self.cfg.admin_emails,
        }

    def delete_account(self, account_id: str) -> None:
        with self.db.lock:
            c = self.db.conn
            ids = [r[0] for r in c.execute("SELECT id FROM emails WHERE account_id = ?", (account_id,))]
            for i in range(0, len(ids), 500):
                chunk = ids[i : i + 500]
                marks = ",".join("?" * len(chunk))
                c.execute(f"DELETE FROM hits WHERE email_id IN ({marks})", chunk)
            c.execute("DELETE FROM suppressions WHERE account_id = ?", (account_id,))
            c.execute("DELETE FROM emails WHERE account_id = ?", (account_id,))
            c.execute("DELETE FROM tokens WHERE account_id = ?", (account_id,))
            c.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            c.execute("INSERT OR REPLACE INTO deleted_accounts(id, deleted_at) VALUES (?, ?)", (account_id, time.time()))

    def purge(self, now: float | None = None) -> None:
        """Daily: forget old webhook ids and idle tokens."""
        now = time.time() if now is None else now
        self.db.execute("DELETE FROM webhook_events WHERE received_at < ?", (now - 30 * 86400,))
        self.db.execute(
            "DELETE FROM tokens WHERE COALESCE(last_used, created_at) < ?", (now - TOKEN_IDLE_SECONDS,)
        )
        self.db.execute(
            "DELETE FROM tokens WHERE label = 'Dashboard' AND COALESCE(last_used, created_at) < ?",
            (now - DASHBOARD_TOKEN_IDLE_SECONDS,),
        )

    # ---- signed short-lived cookie for the OAuth round trip --------------

    def _sign(self, payload: dict) -> str:
        body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
        mac = hmac.new(self.cfg.session_secret.encode(), body.encode(), hashlib.sha256).digest()
        return f"{body}.{_b64url(mac)}"

    def _unsign(self, value: str) -> dict | None:
        body, _, mac = (value or "").partition(".")
        if not body or not mac:
            return None
        want = hmac.new(self.cfg.session_secret.encode(), body.encode(), hashlib.sha256).digest()
        try:
            if not hmac.compare_digest(_b64url_decode(mac), want):
                return None
            data = json.loads(_b64url_decode(body))
        except (ValueError, TypeError):
            return None
        return data if data.get("exp", 0) > time.time() else None

    # ---- Google ----------------------------------------------------------

    def google_identity(self, code: str, verifier: str) -> tuple[str, str]:
        """Exchange the code for an ID token and return (sub, email). The ID token
        comes straight from Google's token endpoint over TLS, which OpenID Connect
        accepts in place of a signature check (Core 1.0, section 3.1.3.7)."""
        form = urllib.parse.urlencode(
            {
                "code": code,
                "client_id": self.cfg.google_client_id,
                "client_secret": self.cfg.google_client_secret,
                "redirect_uri": self.cfg.redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            }
        ).encode()
        status, data = self.http(
            "POST", self.cfg.google_token_url, {"Content-Type": "application/x-www-form-urlencoded"}, form
        )
        if status != 200 or "id_token" not in data:
            raise ValueError(f"token exchange failed ({status})")
        parts = str(data["id_token"]).split(".")
        if len(parts) != 3:
            raise ValueError("malformed id_token")
        claims = json.loads(_b64url_decode(parts[1]))
        aud = claims.get("aud")
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise ValueError("wrong issuer")
        if aud != self.cfg.google_client_id and not (isinstance(aud, list) and self.cfg.google_client_id in aud):
            raise ValueError("wrong audience")
        if float(claims.get("exp", 0)) < time.time():
            raise ValueError("expired id_token")
        if not claims.get("sub") or not claims.get("email") or claims.get("email_verified") not in (True, "true"):
            raise ValueError("no verified email")
        return str(claims["sub"]), str(claims["email"])

    # ---- Stripe ----------------------------------------------------------

    def _stripe(
        self, path: str, params: dict | None = None, method: str = "POST", idempotency_key: str = ""
    ) -> tuple[int, dict]:
        headers = {"Authorization": f"Bearer {self.cfg.stripe_secret_key}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        body = None
        if params is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            body = urllib.parse.urlencode(stripe_form(params)).encode()
        return self.http(method, f"{self.cfg.stripe_api_url}{path}", headers, body)

    def _customer_for(self, row: Any) -> str:
        """The account's Stripe customer, created on its first checkout. The
        idempotency key makes a double click create one customer, not two. It
        changes every 10 minutes: Stripe replays a key's first result for a day,
        failures included, and a failed attempt must not block the next one."""
        if row["customer_id"]:
            return row["customer_id"]
        status, data = self._stripe(
            "/v1/customers",
            {"email": row["email"], "metadata": {"account_id": row["id"]}},
            idempotency_key=f"likelyread-customer-{row['id']}-{int(time.time() // 600)}",
        )
        if status != 200 or not data.get("id"):
            print(f"stripe customer failed: {status} {str(data)[:300]}")
            raise HTTPException(status_code=502, detail="could not start checkout")
        self.db.execute("UPDATE accounts SET customer_id = ? WHERE id = ?", (data["id"], row["id"]))
        return str(data["id"])

    def has_live_subscription(self, row: Any, now: float | None = None) -> bool:
        return row["plan"] == "pro" and self.effective_plan(row, now) == "pro" and bool(row["subscription_id"])

    def verify_webhook(self, headers: Any, body: bytes, now: float | None = None) -> bool:
        """Stripe's signature: Stripe-Signature carries t=<unix time> and a v1=<hex
        HMAC-SHA256 of "t.body"> per signing secret (two while one is rolled)."""
        secret = self.cfg.stripe_webhook_secret
        parts = [p.strip().partition("=") for p in headers.get("stripe-signature", "").split(",")]
        ts = next((v for k, _, v in parts if k == "t"), "")
        sigs = [v for k, _, v in parts if k == "v1"]
        if not (secret and ts and sigs):
            return False
        try:
            if abs((time.time() if now is None else now) - int(ts)) > WEBHOOK_TOLERANCE:
                return False
        except ValueError:
            return False
        want = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        return any(hmac.compare_digest(want, sig) for sig in sigs)

    def _account_from(self, obj: dict) -> str | None:
        """Checkout puts the account id in metadata (and client_reference_id); any
        other object still names the Stripe customer created for the account."""
        meta = obj.get("metadata") or {}
        acct = meta.get("account_id") or obj.get("client_reference_id")
        if acct and self.account(str(acct)) is not None:
            return str(acct)
        customer = obj.get("customer")
        if isinstance(customer, str) and customer:
            rows = self.db.query("SELECT id FROM accounts WHERE customer_id = ?", (customer,))
            if rows:
                return rows[0]["id"]
        return None

    def handle_event(self, event: dict) -> str:
        kind = event.get("type", "")
        obj = (event.get("data") or {}).get("object") or {}
        acct = self._account_from(obj)
        if acct is None:
            ext = (obj.get("metadata") or {}).get("account_id") or obj.get("client_reference_id")
            if ext and self.db.query("SELECT 1 FROM deleted_accounts WHERE id = ?", (str(ext),)):
                print(f"billing event {kind} for deleted account {ext}: check Stripe")
                return "ignored: deleted account"
            return "ignored: unknown account"
        row = self.account(acct)
        if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            if obj.get("mode") != "payment" or (obj.get("metadata") or {}).get("plan") != "lifetime":
                return "ignored: subscription checkout"
            if obj.get("payment_status") != "paid":
                # A delayed method such as a bank debit: async_payment_succeeded follows.
                return "ignored: payment pending"
            # The cap is enforced at checkout. Concurrent checkouts can still pass
            # it; a paying customer always gets what they paid for, so only warn.
            over = self.lifetime_left() <= 0 and row["plan"] != "lifetime"
            if over:
                print(f"lifetime cap exceeded by account {acct}")
            self.db.execute(
                "UPDATE accounts SET plan = 'lifetime', plan_until = NULL, lifetime_payment = ? WHERE id = ?",
                (obj.get("payment_intent"), acct),
            )
            return "lifetime (over cap)" if over else "lifetime"
        if kind == "charge.refunded":
            # Only a full refund of the lifetime payment itself ends lifetime access.
            pi = obj.get("payment_intent")
            if row["plan"] == "lifetime" and obj.get("refunded") and pi and pi == row["lifetime_payment"]:
                self.db.execute("UPDATE accounts SET plan = 'free' WHERE id = ?", (acct,))
                return "lifetime refunded"
            return "ignored"
        if kind.startswith("customer.subscription."):
            status = obj.get("status") or ""
            sub_id = obj.get("id")
            until = _period_end(obj)
            # Cancelling in the customer portal ends the subscription at the period
            # end (cancel_at_period_end, or cancel_at in newer API versions).
            cancel = 1 if obj.get("cancel_at_period_end") or obj.get("cancel_at") else 0
            if row["plan"] == "lifetime":
                return "ignored: lifetime"
            current = row["subscription_id"]
            if current and sub_id and sub_id != current and self.has_live_subscription(row):
                # An older or parallel subscription: never let it change the live one.
                if status in ("active", "trialing"):
                    print(f"second live subscription {sub_id} for account {acct} (current {current})")
                return "ignored: not the current subscription"
            if kind == "customer.subscription.deleted" or status in ("canceled", "incomplete_expired"):
                # The subscription has ended: canceled is final in Stripe.
                self.db.execute(
                    "UPDATE accounts SET plan = 'free', plan_until = NULL, subscription_status = ?, "
                    "cancel_at_period_end = 0 WHERE id = ?",
                    (status or "canceled", acct),
                )
                return "free"
            if status in ("active", "trialing"):
                if until is None:
                    print(f"subscription {sub_id}: no period end in event, assuming one month")
                    until = time.time() + DEFAULT_PERIOD_SECONDS
                self.db.execute(
                    "UPDATE accounts SET plan = 'pro', plan_until = ?, subscription_id = ?, subscription_status = ?, "
                    "cancel_at_period_end = ? WHERE id = ?",
                    (until, sub_id, status, cancel, acct),
                )
                return "pro"
            # incomplete, past_due, unpaid, paused: keep access until the paid period (plus grace) ends.
            self.db.execute(
                "UPDATE accounts SET subscription_status = ?, cancel_at_period_end = ? WHERE id = ?",
                (status, cancel, acct),
            )
            return f"status {status}"
        return "ignored"

    # ---- routes ----------------------------------------------------------

    def register_routes(self, app: FastAPI, principal: Callable[..., Principal]) -> None:
        cfg = self.cfg
        secure = cfg.public_url.startswith("https://")

        @app.get("/auth/google")
        def google_start(ext: str = ""):
            # ext is the Chrome extension's id when the extension started the sign-in.
            ext = ext if ext in cfg.extension_ids else ""
            state = secrets.token_urlsafe(24)
            verifier = secrets.token_urlsafe(48)
            challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
            params = urllib.parse.urlencode(
                {
                    "client_id": cfg.google_client_id,
                    "redirect_uri": cfg.redirect_uri,
                    "response_type": "code",
                    "scope": "openid email",
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "prompt": "select_account",
                }
            )
            resp = RedirectResponse(f"{cfg.google_auth_url}?{params}", status_code=302)
            cookie = self._sign({"state": state, "verifier": verifier, "ext": ext, "exp": time.time() + 600})
            resp.set_cookie("lr_oauth", cookie, max_age=600, httponly=True, secure=secure, samesite="lax", path="/auth")
            return resp

        @app.get("/auth/google/callback")
        def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
            fail = RedirectResponse(f"{cfg.public_url}/?signin=failed", status_code=303)
            fail.delete_cookie("lr_oauth", path="/auth")
            saved = self._unsign(request.cookies.get("lr_oauth", ""))
            if error or not code or not saved or not hmac.compare_digest(str(saved.get("state", "")), state):
                return fail
            try:
                sub, email = self.google_identity(code, str(saved["verifier"]))
            except (ValueError, KeyError) as exc:
                print(f"google sign-in failed: {exc}")
                return fail
            acct = self.upsert_google_account(sub, email)
            ext = str(saved.get("ext") or "")
            ext = ext if ext in cfg.extension_ids else ""
            token = self.create_token(acct, "Chrome extension" if ext else "Dashboard")
            resp = RedirectResponse(f"{cfg.public_url}/#connect={token}{'&ext=' + ext if ext else ''}", status_code=303)
            resp.delete_cookie("lr_oauth", path="/auth")
            return resp

        @app.delete("/api/me")
        def delete_me(p: Principal = Depends(principal)):
            row = self.account(p.account_id or "")
            if row is not None and row["subscription_id"] and row["plan"] == "pro" and cfg.billing_enabled:
                # Stop billing before the account disappears.
                status, _ = self._stripe(f"/v1/subscriptions/{row['subscription_id']}", method="DELETE")
                # 404: already gone. Anything else (403 = the restricted key lacks
                # Subscriptions write) means billing continues, so refuse to delete.
                if status not in (200, 204, 404):
                    raise HTTPException(
                        status_code=502,
                        detail="Could not cancel your subscription. Cancel it under Manage subscription, then try again.",
                    )
            self.delete_account(p.account_id or "")
            return {"ok": True}

        @app.get("/api/me/export")
        def export_me(p: Principal = Depends(principal)):
            """Everything we hold for the account, as JSON (data portability)."""
            row = self.account(p.account_id or "")
            emails = [dict(r) for r in self.db.query(
                "SELECT id, subject, recipients, sent_at, campaign, sender FROM emails WHERE account_id = ? ORDER BY sent_at",
                (row["id"],),
            )]
            hits: dict[str, list] = {}
            ids = [e["id"] for e in emails]
            for i in range(0, len(ids), 500):
                chunk = ids[i : i + 500]
                marks = ",".join("?" * len(chunk))
                for h in self.db.query(f"SELECT email_id, ts, ip, ua FROM hits WHERE email_id IN ({marks}) ORDER BY ts", chunk):
                    hits.setdefault(h["email_id"], []).append({"ts": h["ts"], "ip": h["ip"], "ua": h["ua"]})
            for e in emails:
                e["recipients"] = json.loads(e["recipients"] or "[]")
                e["hits"] = hits.get(e["id"], [])
            account = {k: row[k] for k in ("email", "created_at", "plan", "plan_until", "subscription_status")}
            body = json.dumps({"exported_at": time.time(), "account": account, "emails": emails}, indent=1)
            return Response(
                content=body,
                media_type="application/json",
                headers={"Content-Disposition": 'attachment; filename="likelyread-export.json"'},
            )

        @app.post("/api/me/signout-all")
        def signout_all(p: Principal = Depends(principal)):
            n = self.db.execute("DELETE FROM tokens WHERE account_id = ?", (p.account_id,)).rowcount
            return {"ok": True, "revoked": n}

        @app.post("/api/billing/checkout")
        def checkout(body: CheckoutIn, p: Principal = Depends(principal)):
            if not cfg.billing_enabled:
                raise HTTPException(status_code=503, detail="billing is not configured")
            if body.plan not in PLANS:
                raise HTTPException(status_code=422, detail="unknown plan")
            row = self.account(p.account_id or "")
            if row["plan"] == "lifetime":
                raise HTTPException(status_code=409, detail="you already have lifetime access")
            if body.plan != "lifetime" and self.has_live_subscription(row):
                raise HTTPException(status_code=409, detail="You already have Pro. Change your plan under Manage subscription.")
            if body.plan == "lifetime" and self.lifetime_left() <= 0:
                raise HTTPException(status_code=410, detail="lifetime deals are sold out")
            lifetime = body.plan == "lifetime"
            meta = {"account_id": row["id"], "plan": body.plan}
            params: dict[str, Any] = {
                "mode": "payment" if lifetime else "subscription",
                "customer": self._customer_for(row),
                "client_reference_id": row["id"],
                "line_items": [{"price": cfg.price_ids[body.plan], "quantity": 1}],
                "success_url": f"{cfg.public_url}/#/account?checkout=success",
                "cancel_url": f"{cfg.public_url}/#/account",
                "metadata": meta,
                "automatic_tax": {"enabled": cfg.stripe_tax},
                # Stripe Tax needs the buyer's address; a business can add its VAT number.
                "customer_update": {"address": "auto", "name": "auto"},
                "tax_id_collection": {"enabled": True},
                # Buyers accept the terms of sale before paying. Stripe takes the terms
                # link from the account's public details and refuses the session without it.
                "consent_collection": {"terms_of_service": "required"},
            }
            if lifetime:
                params["invoice_creation"] = {"enabled": True}  # subscriptions are invoiced anyway
            else:
                params["subscription_data"] = {"metadata": meta}
            status, data = self._stripe("/v1/checkout/sessions", params)
            if status != 200 or not data.get("url"):
                print(f"stripe checkout failed: {status} {str(data)[:300]}")
                raise HTTPException(status_code=502, detail="could not start checkout")
            return {"url": data["url"]}

        @app.post("/api/billing/portal")
        def portal(p: Principal = Depends(principal)):
            if not cfg.billing_enabled:
                raise HTTPException(status_code=503, detail="billing is not configured")
            row = self.account(p.account_id or "")
            if row is None or not row["customer_id"]:
                raise HTTPException(status_code=404, detail="no billing account yet")
            status, data = self._stripe(
                "/v1/billing_portal/sessions",
                {"customer": row["customer_id"], "return_url": f"{cfg.public_url}/#/account"},
            )
            if status != 200 or not data.get("url"):
                # Usually the customer portal has not been saved in Stripe's settings yet.
                print(f"stripe portal failed: {status} {str(data)[:300]}")
                raise HTTPException(status_code=502, detail="could not open the billing portal")
            return {"url": data["url"]}

        @app.post("/webhooks/stripe")
        async def stripe_webhook(request: Request):
            body = await request.body()
            if not self.verify_webhook(request.headers, body):
                return JSONResponse({"error": "invalid signature"}, status_code=403)
            try:
                event = json.loads(body)
            except ValueError:
                return {"ok": True, "result": "ignored: not JSON"}
            event_id = str(event.get("id") or "") if isinstance(event, dict) else ""
            if not event_id:
                return {"ok": True, "result": "ignored: no event id"}
            # One lock for check, apply and record: an event is applied once, and a
            # failure (500) leaves no record, so Stripe's retry applies it.
            with self.db.lock:
                if self.db.conn.execute("SELECT 1 FROM webhook_events WHERE id = ?", (event_id,)).fetchone():
                    return {"ok": True, "duplicate": True}
                result = self.handle_event(event)
                self.db.conn.execute(
                    "INSERT INTO webhook_events(id, received_at) VALUES (?, ?)", (event_id, time.time())
                )
            return {"ok": True, "result": result}

        @app.get("/api/admin/stats")
        def admin_stats(p: Principal = Depends(principal)):
            if (p.email or "") not in cfg.admin_emails:
                raise HTTPException(status_code=404, detail="not found")
            now = time.time()
            q = lambda sql, args=(): self.db.query(sql, args)[0][0]  # noqa: E731
            plans = {r["plan"]: r["n"] for r in self.db.query("SELECT plan, COUNT(*) AS n FROM accounts GROUP BY plan")}
            active_pro = sum(
                1 for r in self.db.query("SELECT plan, plan_until FROM accounts WHERE plan != 'free'")
                if self.effective_plan(r, now) == "pro"
            )
            return {
                "accounts": q("SELECT COUNT(*) FROM accounts"),
                "plans": plans,
                "paying_now": active_pro,
                "signups_7d": q("SELECT COUNT(*) FROM accounts WHERE created_at >= ?", (now - 7 * 86400,)),
                "active_7d": q(
                    "SELECT COUNT(DISTINCT account_id) FROM emails WHERE account_id IS NOT NULL AND sent_at >= ?",
                    (now - 7 * 86400,),
                ),
                "emails_24h": q("SELECT COUNT(*) FROM emails WHERE sent_at >= ?", (now - 86400,)),
                "hits_24h": q("SELECT COUNT(*) FROM hits WHERE ts >= ?", (now - 86400,)),
                "lifetime_left": self.lifetime_left(),
            }
