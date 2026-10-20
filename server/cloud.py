"""Likelyread Cloud: accounts, Google sign-in, plans and Polar billing.

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
from datetime import datetime
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
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
  cancel_at_period_end INTEGER NOT NULL DEFAULT 0
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
"""

PLANS = ("pro_monthly", "pro_yearly", "lifetime")
# A paid period keeps working this long after it ends, to cover renewal delays.
GRACE_SECONDS = 3 * 86400
MAX_TOKENS_PER_ACCOUNT = 20
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
WEBHOOK_TOLERANCE = 300
EXTENSION_ID_RE = re.compile(r"^[a-p]{32}$")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _iso_ts(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class CloudSettings:
    def __init__(self, env: dict | None = None) -> None:
        e = os.environ if env is None else env
        self.public_url = (e.get("PUBLIC_URL") or "").rstrip("/")
        self.session_secret = e.get("SESSION_SECRET", "")
        self.google_client_id = e.get("GOOGLE_CLIENT_ID", "")
        self.google_client_secret = e.get("GOOGLE_CLIENT_SECRET", "")
        self.google_auth_url = e.get("GOOGLE_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth")
        self.google_token_url = e.get("GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token")
        self.polar_api_url = (e.get("POLAR_API_URL") or "https://api.polar.sh").rstrip("/")
        self.polar_access_token = e.get("POLAR_ACCESS_TOKEN", "")
        self.polar_webhook_secret = e.get("POLAR_WEBHOOK_SECRET", "")
        self.products = {
            "pro_monthly": e.get("POLAR_PRODUCT_PRO_MONTHLY", ""),
            "pro_yearly": e.get("POLAR_PRODUCT_PRO_YEARLY", ""),
            "lifetime": e.get("POLAR_PRODUCT_LIFETIME", ""),
        }
        self.prices = {
            "pro_monthly": e.get("PRICE_PRO_MONTHLY", "$5"),
            "pro_yearly": e.get("PRICE_PRO_YEARLY", "$36"),
            "lifetime": e.get("PRICE_LIFETIME", "$59"),
        }
        self.lifetime_cap = int(e.get("LIFETIME_CAP", "500"))
        self.admin_emails = {a.strip().lower() for a in e.get("ADMIN_EMAILS", "").split(",") if a.strip()}

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
        return bool(self.polar_access_token and self.polar_webhook_secret and all(self.products.values()))

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
            "SELECT a.*, t.hash, t.last_used FROM tokens t JOIN accounts a ON a.id = t.account_id WHERE t.hash = ?",
            (token_hash(token),),
        )
        if not rows:
            return None
        row = rows[0]
        now = time.time()
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
            "SELECT COUNT(*) AS n FROM emails WHERE account_id = ? AND sent_at >= ?", (row["id"], time.time() - 86400)
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

    # ---- Polar -----------------------------------------------------------

    def _polar(self, path: str, payload: dict) -> tuple[int, dict]:
        return self.http(
            "POST",
            f"{self.cfg.polar_api_url}{path}",
            {"Authorization": f"Bearer {self.cfg.polar_access_token}", "Content-Type": "application/json"},
            json.dumps(payload).encode(),
        )

    def verify_webhook(self, headers: Any, body: bytes, now: float | None = None) -> bool:
        """Standard Webhooks signature (what Polar uses). Polar signs with the raw
        secret, so the HMAC key is the secret's bytes."""
        msg_id = headers.get("webhook-id", "")
        ts = headers.get("webhook-timestamp", "")
        sigs = headers.get("webhook-signature", "")
        if not (msg_id and ts and sigs and self.cfg.polar_webhook_secret):
            return False
        try:
            if abs((time.time() if now is None else now) - int(ts)) > WEBHOOK_TOLERANCE:
                return False
        except ValueError:
            return False
        signed = f"{msg_id}.{ts}.".encode() + body
        want = base64.b64encode(hmac.new(self.cfg.polar_webhook_secret.encode(), signed, hashlib.sha256).digest())
        for part in sigs.split():
            version, _, sig = part.partition(",")
            if version == "v1" and hmac.compare_digest(sig.encode(), want):
                return True
        return False

    def _account_from(self, data: dict) -> str | None:
        customer = data.get("customer") or {}
        meta = data.get("metadata") or {}
        acct = customer.get("external_id") or meta.get("account_id")
        if acct and self.account(str(acct)) is not None:
            return str(acct)
        return None

    def _product_id(self, data: dict) -> str | None:
        return data.get("product_id") or (data.get("product") or {}).get("id")

    def handle_event(self, event: dict) -> str:
        kind = event.get("type", "")
        data = event.get("data") or {}
        acct = self._account_from(data)
        if acct is None:
            return "ignored: unknown account"
        row = self.account(acct)
        lifetime = self.cfg.products["lifetime"]
        if kind == "order.paid":
            if lifetime and self._product_id(data) == lifetime:
                self.db.execute("UPDATE accounts SET plan = 'lifetime', plan_until = NULL WHERE id = ?", (acct,))
                return "lifetime"
            return "ignored: subscription order"
        if kind == "order.refunded":
            if lifetime and self._product_id(data) == lifetime and row["plan"] == "lifetime":
                self.db.execute("UPDATE accounts SET plan = 'free' WHERE id = ?", (acct,))
                return "lifetime refunded"
            return "ignored"
        if kind.startswith("subscription."):
            status = data.get("status") or ""
            sub_id = data.get("id")
            until = _iso_ts(data.get("current_period_end")) or _iso_ts(data.get("ends_at"))
            cancel = 1 if data.get("cancel_at_period_end") else 0
            if row["plan"] == "lifetime":
                return "ignored: lifetime"
            if kind == "subscription.revoked" or status in ("revoked", "incomplete_expired"):
                self.db.execute(
                    "UPDATE accounts SET plan = 'free', plan_until = NULL, subscription_status = ?, "
                    "cancel_at_period_end = 0 WHERE id = ?",
                    (status or "revoked", acct),
                )
                return "free"
            if status in ("active", "trialing"):
                self.db.execute(
                    "UPDATE accounts SET plan = 'pro', plan_until = ?, subscription_id = ?, subscription_status = ?, "
                    "cancel_at_period_end = ? WHERE id = ?",
                    (until, sub_id, status, cancel, acct),
                )
                return "pro"
            # past_due, canceled, unpaid: keep access until the paid period (plus grace) ends.
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
            ext = ext if EXTENSION_ID_RE.match(ext) else ""
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
            ext = ext if EXTENSION_ID_RE.match(ext) else ""
            token = self.create_token(acct, "Chrome extension" if ext else "Dashboard")
            resp = RedirectResponse(f"{cfg.public_url}/#connect={token}{'&ext=' + ext if ext else ''}", status_code=303)
            resp.delete_cookie("lr_oauth", path="/auth")
            return resp

        @app.delete("/api/me")
        def delete_me(p: Principal = Depends(principal)):
            self.delete_account(p.account_id or "")
            return {"ok": True}

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
            if body.plan == "lifetime" and self.lifetime_left() <= 0:
                raise HTTPException(status_code=410, detail="lifetime deals are sold out")
            status, data = self._polar(
                "/v1/checkouts/",
                {
                    "products": [cfg.products[body.plan]],
                    "external_customer_id": row["id"],
                    "customer_email": row["email"],
                    "success_url": f"{cfg.public_url}/#/account?checkout=success",
                    "return_url": f"{cfg.public_url}/#/account",
                    "metadata": {"account_id": row["id"], "plan": body.plan},
                },
            )
            if status not in (200, 201) or not data.get("url"):
                print(f"polar checkout failed: {status} {str(data)[:300]}")
                raise HTTPException(status_code=502, detail="could not start checkout")
            return {"url": data["url"]}

        @app.post("/api/billing/portal")
        def portal(p: Principal = Depends(principal)):
            if not cfg.billing_enabled:
                raise HTTPException(status_code=503, detail="billing is not configured")
            status, data = self._polar(
                "/v1/customer-sessions/",
                {"external_customer_id": p.account_id, "return_url": f"{cfg.public_url}/#/account"},
            )
            if status not in (200, 201) or not data.get("customer_portal_url"):
                raise HTTPException(status_code=404, detail="no billing account yet")
            return {"url": data["customer_portal_url"]}

        @app.post("/webhooks/polar")
        async def polar_webhook(request: Request):
            body = await request.body()
            if not self.verify_webhook(request.headers, body):
                return JSONResponse({"error": "invalid signature"}, status_code=403)
            msg_id = request.headers["webhook-id"]
            with self.db.lock:
                seen = self.db.conn.execute("SELECT 1 FROM webhook_events WHERE id = ?", (msg_id,)).fetchone()
                if seen:
                    return {"ok": True, "duplicate": True}
                self.db.conn.execute("INSERT INTO webhook_events(id, received_at) VALUES (?, ?)", (msg_id, time.time()))
            try:
                result = self.handle_event(json.loads(body))
            except (ValueError, TypeError) as exc:
                result = f"ignored: {exc}"
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
