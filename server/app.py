"""Likelyread server: email open tracking for Gmail.

Two modes, one codebase:
- selfhost (default): one user, one bearer token, every feature. See SPEC.md.
- cloud (MODE=cloud): many accounts with Google sign-in, Free and Pro plans and
  Stripe billing. The cloud parts live in cloud.py.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import os
import re
import sqlite3
import statistics
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


class Settings:
    def __init__(self) -> None:
        self.mode = (os.environ.get("MODE") or "selfhost").strip().lower()
        if self.mode not in ("selfhost", "cloud"):
            raise RuntimeError("MODE must be selfhost or cloud.")
        self.token = os.environ.get("LIKELYREAD_TOKEN", "")
        self.db_path = os.environ.get("DB_PATH", "/data/likelyread.db")
        self.grace_seconds = _env_float("GRACE_SECONDS", 20)
        self.suppress_window_seconds = _env_float("SUPPRESS_WINDOW_SECONDS", 60)
        self.store_ip = _env_bool("STORE_IP", True)
        self.retention_days = _env_float("RETENTION_DAYS", 0)
        self.allowed_origins = [
            o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()
        ]
        self.dashboard = _env_bool("DASHBOARD", True)
        self.pixel_rate_per_min = int(_env_float("PIXEL_RATE_PER_MIN", 60))
        if self.mode == "selfhost":
            if not self.token:
                raise RuntimeError("LIKELYREAD_TOKEN is required. Generate one with: openssl rand -hex 32")
            if len(self.token) < 16:
                raise RuntimeError("LIKELYREAD_TOKEN is too short (minimum 16 characters).")


# 43-byte transparent 1x1 GIF.
PIXEL_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
    b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)
assert len(PIXEL_GIF) == 43

PIXEL_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, private, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
APPLE_MPP_UA = "Mozilla/5.0"
# Gmail's image proxy. Older UA: "... (via ggpht.com GoogleImageProxy)". Since about 2023 it sends
# a fixed, outdated Chrome/Edge string instead, so also match that string and Google's ranges.
GMAIL_PROXY_UA_MARKERS = ("GoogleImageProxy", "Chrome/42.0.2311.135 Safari/537.36 Edge/12.246")
GOOGLE_PROXY_NETS = tuple(
    ipaddress.ip_network(n)
    for n in ("66.102.0.0/20", "66.249.64.0/19", "209.85.128.0/17", "72.14.192.0/18", "74.125.0.0/16", "64.233.160.0/19")
)
# Apple Inc. (AS714). Mail Privacy Protection loads images from here. Apple Mail
# sends the same bare "Mozilla/5.0" user agent with or without Privacy Protection,
# so the network is what tells a prefetch from a real open on the device.
APPLE_NETS = tuple(ipaddress.ip_network(n) for n in ("17.0.0.0/8", "2620:149::/32", "2a01:b740::/32"))
UA_MAX = 512
MAX_AGE_SECONDS = 30 * 86400
# Hits for ids that were never registered are dropped after this long. The
# extension retries registration for 7 days, so 8 leaves a margin.
ORPHAN_HIT_SECONDS = 8 * 86400
NO_CAMPAIGN = "__none__"

DASHBOARD_DIR = Path(__file__).parent / "dashboard"

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
  id          TEXT PRIMARY KEY,
  subject     TEXT,
  recipients  TEXT,
  sent_at     REAL,
  campaign    TEXT,
  sender      TEXT,
  account_id  TEXT,
  created_at  REAL
);
CREATE TABLE IF NOT EXISTS hits (
  id        INTEGER PRIMARY KEY,
  email_id  TEXT,
  ts        REAL,
  ip        TEXT NULL,
  ua        TEXT,
  net       TEXT NULL   -- apple | google | other, kept even when STORE_IP=false
);
CREATE TABLE IF NOT EXISTS suppressions (
  email_id    TEXT,
  ts          REAL,
  account_id  TEXT
);
CREATE TABLE IF NOT EXISTS sender_settings (
  acct         TEXT NOT NULL,     -- account id, '' in self-host mode
  sender       TEXT NOT NULL,     -- lower-cased address
  established  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (acct, sender)
);
CREATE INDEX IF NOT EXISTS idx_hits_email ON hits(email_id);
CREATE INDEX IF NOT EXISTS idx_hits_ts ON hits(ts);
CREATE INDEX IF NOT EXISTS idx_supp_email ON suppressions(email_id);
CREATE INDEX IF NOT EXISTS idx_emails_sent ON emails(sent_at);
"""

# --------------------------------------------------------------------------
# Plans. Self-hosting has everything; the cloud has Free and Pro.
# --------------------------------------------------------------------------

FEATURES: dict[str, dict] = {
    "selfhost": {"campaigns": True, "stats": True, "senders": True, "history_days": None, "daily_limit": None},
    "pro": {"campaigns": True, "stats": True, "senders": True, "history_days": None, "daily_limit": 5000},
    "free": {"campaigns": False, "stats": False, "senders": False, "history_days": 30, "daily_limit": 500},
}


@dataclass(frozen=True)
class Principal:
    """Who is calling. account_id is None in self-host mode."""

    account_id: str | None
    plan: str  # selfhost | free | pro
    email: str | None = None

    @property
    def features(self) -> dict:
        return FEATURES[self.plan]

    def since(self, now: float | None = None) -> float | None:
        days = self.features["history_days"]
        return None if days is None else (time.time() if now is None else now) - days * 86400


def pro_required(feature: str) -> HTTPException:
    return HTTPException(status_code=402, detail={"error": "pro_required", "feature": feature})


def need(p: Principal, feature: str) -> None:
    if not p.features.get(feature):
        raise pro_required(feature)


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------


class DB:
    """A single SQLite connection guarded by a lock."""

    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(emails)")}
            if "campaign" not in cols:  # databases created by a pre-campaign build
                self.conn.execute("ALTER TABLE emails ADD COLUMN campaign TEXT")
            if "sender" not in cols:  # databases created before the sender address was stored
                self.conn.execute("ALTER TABLE emails ADD COLUMN sender TEXT")
            if "account_id" not in cols:  # databases created before accounts existed
                self.conn.execute("ALTER TABLE emails ADD COLUMN account_id TEXT")
            if "created_at" not in cols:  # server time of first registration (sent_at is client-derived)
                self.conn.execute("ALTER TABLE emails ADD COLUMN created_at REAL")
            hcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(hits)")}
            if "net" not in hcols:
                self.conn.execute("ALTER TABLE hits ADD COLUMN net TEXT")
            scols = {r["name"] for r in self.conn.execute("PRAGMA table_info(suppressions)")}
            if "account_id" not in scols:
                self.conn.execute("ALTER TABLE suppressions ADD COLUMN account_id TEXT")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_campaign ON emails(campaign)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_account ON emails(account_id, sent_at)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_created ON emails(account_id, created_at)")

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self.lock:
            self.conn.close()


# --------------------------------------------------------------------------
# Classification (computed at read time, never stored)
# --------------------------------------------------------------------------


def explain_hit(
    ts: float,
    ua: str | None,
    sent_at: float | None,
    suppressions: list[float],
    cfg: Settings,
    via: str = "direct",
    net: str | None = None,
) -> tuple[str, str]:
    """Return (kind, reason). The first rule that matches wins."""
    if sent_at is not None and ts < sent_at - 10:
        # Agents can embed the pixel in a draft and register it once sent. Nobody
        # else can load it before then, so an earlier hit is a preview.
        return "self", "Before the email was sent: a draft preview."
    if sent_at is not None and sent_at - 10 <= ts <= sent_at + cfg.grace_seconds:
        return "self", f"Within {cfg.grace_seconds:g} s of sending: your own compose window."
    for s in suppressions:
        if s - 5 <= ts <= s + cfg.suppress_window_seconds:
            return "self", f"{max(0, ts - s):.0f} s after you opened it yourself."
    if (ua or "").strip() == APPLE_MPP_UA:
        if net in ("other", "google"):
            # Without Privacy Protection, Apple Mail loads images on the device itself.
            # This also stops a recipient hiding a real open by faking Apple's agent.
            return "open", "Apple Mail on the device: user agent Mozilla/5.0 from outside Apple's network."
        if net == "apple":
            return "prefetch", "From Apple's network with user agent Mozilla/5.0: Mail Privacy Protection loading images on delivery."
        return "prefetch", "User agent is exactly Mozilla/5.0: Apple Mail loading images on delivery."
    if via == "gmail_proxy":
        return "open", "Loaded through Gmail's image proxy when the email was opened."
    return "open", "Loaded directly by a mail app."


def classify_hit(ts: float, ua: str | None, sent_at: float | None, suppressions: list[float], cfg: Settings) -> str:
    return explain_hit(ts, ua, sent_at, suppressions, cfg)[0]


def _is_google_ip(ip: str | None) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in net for net in GOOGLE_PROXY_NETS)


def network_of(ip: str | None) -> str | None:
    """apple, google or other for a valid address; None when unknown."""
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if any(addr in n for n in APPLE_NETS):
        return "apple"
    if any(addr in n for n in GOOGLE_PROXY_NETS):
        return "google"
    return "other"


def hit_via(ua: str | None, ip: str | None = None, net: str | None = None) -> str:
    ua = ua or ""
    if any(m in ua for m in GMAIL_PROXY_UA_MARKERS) or net == "google" or _is_google_ip(ip):
        return "gmail_proxy"
    return "direct"


def match_suppressions(hits: list, sent_at: float | None, supps: list[float], cfg: Settings) -> dict[int, float]:
    """Pair each self-view ping with at most one hit: the nearest one in its window
    that is not already explained by the send time. One view loads the image once,
    so a ping can never hide more than one hit. A forged ping (for example from a
    spoofed message showing the pixel) therefore only covers the load it caused."""
    taken: dict[int, float] = {}
    for s in sorted(supps):
        best = None
        for i, h in enumerate(hits):
            ts = h["ts"]
            if i in taken or not (s - 5 <= ts <= s + cfg.suppress_window_seconds):
                continue
            if sent_at is not None and ts <= sent_at + cfg.grace_seconds:
                continue  # already self by the send-time rules
            if best is None or abs(ts - s) < abs(hits[best]["ts"] - s):
                best = i
        if best is not None:
            taken[best] = s
    return taken


def status_of(summary: dict) -> str:
    if summary["opens"] > 0:
        return "opened"
    if summary["prefetch"] > 0:
        return "prefetch_only"
    return "not_opened"


class Store:
    """Read/write helpers on top of DB, including classification. Every query is
    scoped to one account (account_id IS NULL in self-host mode)."""

    def __init__(self, db: DB, cfg: Settings) -> None:
        self.db = db
        self.cfg = cfg

    # ---- loading -------------------------------------------------------

    def _emails(
        self,
        acct: str | None,
        campaign: str | None = None,
        q: str | None = None,
        sender: str | None = None,
        since: float | None = None,
    ) -> list[sqlite3.Row]:
        sql = "SELECT id, subject, recipients, sent_at, campaign, sender FROM emails"
        where: list[str] = ["account_id IS ?"]
        params: list[object] = [acct]
        if campaign == NO_CAMPAIGN:
            where.append("(campaign IS NULL OR campaign = '')")
        elif campaign:
            where.append("campaign = ?")
            params.append(campaign)
        if sender:
            where.append("lower(sender) = lower(?)")
            params.append(sender)
        if q:
            where.append("(subject LIKE ? OR recipients LIKE ? OR campaign LIKE ? OR sender LIKE ?)")
            like = f"%{q}%"
            params += [like, like, like, like]
        if since is not None:
            where.append("sent_at >= ?")
            params.append(since)
        sql += " WHERE " + " AND ".join(where) + " ORDER BY sent_at DESC"
        return self.db.query(sql, params)

    def _hits_and_supps(
        self, ids: list[str], acct: str | None
    ) -> tuple[dict[str, list[sqlite3.Row]], dict[str, list[float]]]:
        hits: dict[str, list[sqlite3.Row]] = defaultdict(list)
        supps: dict[str, list[float]] = defaultdict(list)
        if not ids:
            return hits, supps
        # Chunk to stay under SQLite's parameter limit.
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            for r in self.db.query(
                f"SELECT email_id, ts, ip, ua, net FROM hits WHERE email_id IN ({marks}) ORDER BY ts", chunk
            ):
                hits[r["email_id"]].append(r)
            # Only the owner's own suppress pings count, so another account cannot
            # hide someone's opens by pinging a pixel id it has seen.
            for r in self.db.query(
                f"SELECT email_id, ts FROM suppressions WHERE email_id IN ({marks}) AND account_id IS ?",
                [*chunk, acct],
            ):
                supps[r["email_id"]].append(r["ts"])
        return hits, supps

    def _classified(self, email: sqlite3.Row, hits: list[sqlite3.Row], supps: list[float]) -> list[dict]:
        out = []
        matched = match_suppressions(hits, email["sent_at"], supps, self.cfg)
        for i, h in enumerate(hits):
            net = h["net"] or network_of(h["ip"])
            via = hit_via(h["ua"], h["ip"], net)
            ping = [matched[i]] if i in matched else []
            kind, reason = explain_hit(h["ts"], h["ua"], email["sent_at"], ping, self.cfg, via, net)
            out.append({"ts": h["ts"], "ip": h["ip"], "ua": h["ua"], "kind": kind, "via": via, "reason": reason})
        return out

    @staticmethod
    def _summary(email: sqlite3.Row, classified: list[dict]) -> dict:
        opens = [h["ts"] for h in classified if h["kind"] == "open"]
        summary = {
            "id": email["id"],
            "subject": email["subject"],
            "recipients": json.loads(email["recipients"] or "[]"),
            "sent_at": email["sent_at"],
            "campaign": email["campaign"] or None,
            "sender": email["sender"] or None,
            "opens": len(opens),
            "first_open": min(opens) if opens else None,
            "last_open": max(opens) if opens else None,
            "prefetch": sum(1 for h in classified if h["kind"] == "prefetch"),
            "self": sum(1 for h in classified if h["kind"] == "self"),
        }
        summary["status"] = status_of(summary)
        return summary

    def summaries(
        self,
        acct: str | None,
        campaign: str | None = None,
        q: str | None = None,
        sender: str | None = None,
        since: float | None = None,
    ) -> list[tuple[dict, list[dict]]]:
        emails = self._emails(acct, campaign, q, sender, since)
        hits, supps = self._hits_and_supps([e["id"] for e in emails], acct)
        result = []
        for e in emails:
            classified = self._classified(e, hits.get(e["id"], []), supps.get(e["id"], []))
            result.append((self._summary(e, classified), classified))
        return result

    def one(self, acct: str | None, email_id: str, since: float | None = None) -> dict | None:
        rows = self.db.query(
            "SELECT id, subject, recipients, sent_at, campaign, sender FROM emails WHERE id = ? AND account_id IS ?",
            (email_id, acct),
        )
        if not rows:
            return None
        e = rows[0]
        if since is not None and (e["sent_at"] or 0) < since:
            raise pro_required("history")
        hits, supps = self._hits_and_supps([email_id], acct)
        classified = self._classified(e, hits.get(email_id, []), supps.get(email_id, []))
        summary = self._summary(e, classified)
        summary["hits"] = classified
        return summary


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------


def _clean_campaign(v: str | None) -> str | None:
    if v is None:
        return None
    v = " ".join(v.split())[:100]
    if v == NO_CAMPAIGN:  # reserved filter value for "uncategorized"
        return None
    return v or None


class EmailIn(BaseModel):
    id: str
    subject: str = Field(default="", max_length=2000)
    recipients: list[str] = Field(default_factory=list, max_length=500)
    age: float = Field(default=0, ge=0, le=MAX_AGE_SECONDS)
    campaign: str | None = Field(default=None, max_length=200)
    sender: str | None = Field(default=None, max_length=320)

    @field_validator("id")
    @classmethod
    def _uuid(cls, v: str) -> str:
        v = v.lower()
        if not UUID4_RE.match(v):
            raise ValueError("id must be a UUIDv4")
        return v

    @field_validator("recipients")
    @classmethod
    def _recipients(cls, v: list[str]) -> list[str]:
        return [r.strip()[:320] for r in v if r and r.strip()]

    @field_validator("campaign")
    @classmethod
    def _campaign(cls, v: str | None) -> str | None:
        return _clean_campaign(v)

    @field_validator("sender")
    @classmethod
    def _sender(cls, v: str | None) -> str | None:
        v = (v or "").strip().lower()
        return v if "@" in v and " " not in v else None


class EmailPatch(BaseModel):
    campaign: str | None = Field(default=None, max_length=200)

    @field_validator("campaign")
    @classmethod
    def _campaign(cls, v: str | None) -> str | None:
        return _clean_campaign(v)


class SenderSettingsIn(BaseModel):
    established: bool


class CampaignRename(BaseModel):
    name: str | None = Field(default=None, max_length=200)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        return _clean_campaign(v)


# --------------------------------------------------------------------------
# Rate limiting for the public pixel endpoint
# --------------------------------------------------------------------------


class RateLimiter:
    """Sliding one-minute window per key. Over-limit hits are not logged."""

    MAX_KEYS = 50_000

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.windows: dict[str, deque] = {}
        self.lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        if self.per_minute <= 0:
            return True
        now = time.time() if now is None else now
        with self.lock:
            if key not in self.windows and len(self.windows) >= self.MAX_KEYS:
                # Drop only expired windows. Never reset live counters.
                for k in [k for k, w in self.windows.items() if not w or w[-1] <= now - 60]:
                    del self.windows[k]
                if len(self.windows) >= self.MAX_KEYS:
                    return False  # under a wide flood, new sources are answered but not logged
            w = self.windows.setdefault(key, deque())
            while w and w[0] <= now - 60:
                w.popleft()
            if len(w) >= self.per_minute:
                return False
            w.append(now)
            return True


def limiter_key(ip: str | None, email_id: str) -> str:
    """Gmail's proxy serves many recipients from few addresses: limit it per email,
    so one busy proxy address never hides other people's opens."""
    ip = ip or "?"
    return f"{ip}|{email_id}" if _is_google_ip(ip) else ip


# --------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------


def _day_key(ts: float, tz_minutes: int) -> str:
    return datetime.fromtimestamp(ts + tz_minutes * 60, tz=timezone.utc).strftime("%Y-%m-%d")


def _hour(ts: float, tz_minutes: int) -> int:
    return datetime.fromtimestamp(ts + tz_minutes * 60, tz=timezone.utc).hour


def purge_old(db: DB, cfg: Settings, now: float | None = None) -> int:
    """Daily maintenance. Returns the number of hits deleted."""
    now = time.time() if now is None else now
    # Hits for ids never registered (late registration gave up, or junk traffic).
    n = db.execute(
        "DELETE FROM hits WHERE ts < ? AND email_id NOT IN (SELECT id FROM emails)", (now - ORPHAN_HIT_SECONDS,)
    ).rowcount
    db.execute(
        "DELETE FROM suppressions WHERE ts < ? AND email_id NOT IN (SELECT id FROM emails)",
        (now - ORPHAN_HIT_SECONDS,),
    )
    if cfg.retention_days > 0:
        cutoff = now - cfg.retention_days * 86400
        n += db.execute("DELETE FROM hits WHERE ts < ?", (cutoff,)).rowcount
        db.execute("DELETE FROM suppressions WHERE ts < ?", (cutoff,))
        # Subjects and recipients are personal data too.
        db.execute("DELETE FROM emails WHERE sent_at < ?", (cutoff,))
    return n


def create_app(cfg: Settings | None = None) -> FastAPI:
    cfg = cfg or Settings()
    db = DB(cfg.db_path)
    store = Store(db, cfg)
    limiter = RateLimiter(cfg.pixel_rate_per_min)
    # Gmail proxy hits are keyed per (proxy ip, email) and live in their own table,
    # so a burst of them can never fill the table that direct hits rely on.
    proxy_limiter = RateLimiter(cfg.pixel_rate_per_min)
    import health

    dns_cache = health.DnsCache()
    cloud = None
    if cfg.mode == "cloud":
        import cloud as cloud_mod  # only loaded in cloud mode

        cloud = cloud_mod.Cloud(db, cloud_mod.CloudSettings())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def retention_loop():
            while True:
                try:
                    await asyncio.to_thread(purge_old, db, cfg)
                    if cloud is not None:
                        await asyncio.to_thread(cloud.purge)
                except Exception as exc:  # keep the loop alive
                    print(f"retention purge failed: {exc}")
                await asyncio.sleep(86400)

        task = asyncio.create_task(retention_loop())
        yield
        task.cancel()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.db = db
    app.state.cfg = cfg
    app.state.limiter = limiter
    app.state.proxy_limiter = proxy_limiter
    app.state.cloud = cloud
    app.state.dns_cache = dns_cache

    if cfg.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.allowed_origins,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
            expose_headers=["X-Total-Count"],
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if not request.url.path.startswith("/p/"):
            response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith("/api/") or request.url.path.startswith("/auth/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    token_bytes = cfg.token.encode()

    def bearer(request: Request) -> str:
        scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
        return supplied.strip() if scheme.lower() == "bearer" else ""

    def unauthorized() -> HTTPException:
        return HTTPException(status_code=401, detail="invalid token", headers={"WWW-Authenticate": "Bearer"})

    def principal(request: Request) -> Principal:
        supplied = bearer(request)
        if cloud is None:
            if not supplied or not hmac.compare_digest(supplied.encode(), token_bytes):
                raise unauthorized()
            return Principal(account_id=None, plan="selfhost")
        found = cloud.authenticate(supplied) if supplied else None
        if found is None:
            raise unauthorized()
        return found

    auth = [Depends(principal)]

    # ---- public ------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/api/config")
    def config():
        """Public: lets the dashboard and extension adapt to the server's mode."""
        out = {"mode": cfg.mode}
        if cloud is not None:
            out.update(cloud.public_config())
        return out

    @app.get("/p/{name}")
    def pixel(name: str, request: Request):
        email_id = name[:-4].lower() if name.endswith(".gif") else ""
        ip = request.client.host if request.client else None
        key = limiter_key(ip, email_id)
        bucket = proxy_limiter if "|" in key else limiter
        if UUID4_RE.match(email_id) and bucket.allow(key):
            ua = (request.headers.get("user-agent") or "")[:UA_MAX]
            try:
                db.execute(
                    "INSERT INTO hits(email_id, ts, ip, ua, net) VALUES (?, ?, ?, ?, ?)",
                    (email_id, time.time(), ip if cfg.store_ip else None, ua, network_of(ip)),
                )
            except sqlite3.Error as exc:  # never break the image
                print(f"hit insert failed: {exc}")
        return Response(content=PIXEL_GIF, media_type="image/gif", headers=PIXEL_HEADERS)

    # ---- authenticated API ---------------------------------------------

    @app.get("/api/ping")
    def ping(p: Principal = Depends(principal)):
        return {"ok": True, "time": time.time(), "plan": p.plan}

    @app.get("/api/me")
    def me(p: Principal = Depends(principal)):
        if cloud is not None:
            return cloud.me(p)
        return {"mode": "selfhost", "plan": "selfhost", "email": None, "features": p.features}

    @app.post("/api/emails")
    def register(body: EmailIn, p: Principal = Depends(principal)):
        now = time.time()
        sent_at = now - body.age
        acct = p.account_id
        with db.lock:
            existing = db.conn.execute("SELECT account_id FROM emails WHERE id = ?", (body.id,)).fetchone()
            if existing is not None and existing["account_id"] != acct:
                # Pixel ids are random; one belonging to another account is never reused.
                raise HTTPException(status_code=409, detail="id belongs to another account")
            limit = p.features["daily_limit"]
            if existing is None and limit is not None:
                # created_at is server time: a client-chosen age cannot dodge the limit.
                n = db.conn.execute(
                    "SELECT COUNT(*) FROM emails WHERE account_id IS ? AND created_at >= ?", (acct, now - 86400)
                ).fetchone()[0]
                if n >= limit:
                    raise HTTPException(status_code=429, detail={"error": "daily_limit", "limit": limit})
            # Upsert: a retried registration carries the same data; a re-send after a
            # failed or undone send updates the record to the real send.
            db.conn.execute(
                """INSERT INTO emails(id, subject, recipients, sent_at, campaign, sender, account_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET subject=excluded.subject, recipients=excluded.recipients,
                   sent_at=excluded.sent_at, campaign=COALESCE(excluded.campaign, emails.campaign),
                   sender=COALESCE(excluded.sender, emails.sender)
                   WHERE emails.account_id IS excluded.account_id""",
                (body.id, body.subject, json.dumps(body.recipients), sent_at, body.campaign, body.sender, acct, now),
            )
        return {"ok": True, "id": body.id, "sent_at": sent_at}

    @app.post("/api/emails/{email_id}/suppress")
    def suppress(email_id: str, p: Principal = Depends(principal)):
        email_id = email_id.lower()
        if not UUID4_RE.match(email_id):
            raise HTTPException(status_code=422, detail="id must be a UUIDv4")
        if p.account_id is not None:
            # Cloud: only for the caller's own emails. (Self-host keeps pings for ids
            # whose registration is still in flight.)
            if not db.query("SELECT 1 FROM emails WHERE id = ? AND account_id = ?", (email_id, p.account_id)):
                return {"ok": True}
        db.execute(
            "INSERT INTO suppressions(email_id, ts, account_id) VALUES (?, ?, ?)", (email_id, time.time(), p.account_id)
        )
        return {"ok": True}

    @app.get("/api/emails")
    def list_emails(
        response: Response,
        p: Principal = Depends(principal),
        limit: int = Query(50, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        campaign: str | None = Query(None, max_length=200),
        q: str | None = Query(None, max_length=200),
        status: str | None = Query(None, pattern="^(opened|not_opened|prefetch_only)$"),
        sender: str | None = Query(None, max_length=320),
    ):
        if campaign:
            need(p, "campaigns")
        if sender:
            need(p, "senders")
        items = [s for s, _ in store.summaries(p.account_id, campaign, q, sender, p.since())]
        if status:
            items = [s for s in items if s["status"] == status]
        response.headers["X-Total-Count"] = str(len(items))
        return items[offset : offset + limit]

    @app.get("/api/emails/{email_id}")
    def get_email(email_id: str, p: Principal = Depends(principal)):
        found = store.one(p.account_id, email_id.lower(), p.since())
        if not found:
            raise HTTPException(status_code=404, detail="not found")
        return found

    @app.patch("/api/emails/{email_id}")
    def patch_email(email_id: str, body: EmailPatch, p: Principal = Depends(principal)):
        email_id = email_id.lower()
        if store.one(p.account_id, email_id, p.since()) is None:  # raises 402 past the free history
            raise HTTPException(status_code=404, detail="not found")
        cur = db.execute(
            "UPDATE emails SET campaign = ? WHERE id = ? AND account_id IS ?", (body.campaign, email_id, p.account_id)
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="not found")
        return store.one(p.account_id, email_id, p.since())

    @app.delete("/api/emails/{email_id}")
    def delete_email(email_id: str, p: Principal = Depends(principal)):
        email_id = email_id.lower()
        cur = db.execute("DELETE FROM emails WHERE id = ? AND account_id IS ?", (email_id, p.account_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="not found")
        db.execute("DELETE FROM hits WHERE email_id = ?", (email_id,))
        db.execute("DELETE FROM suppressions WHERE email_id = ?", (email_id,))
        return {"ok": True}

    # ---- campaigns and dashboard stats (Pro in the cloud) ----------------

    @app.get("/api/campaigns")
    def campaigns(p: Principal = Depends(principal)):
        need(p, "campaigns")
        groups: dict[str | None, list[dict]] = defaultdict(list)
        for s, _ in store.summaries(p.account_id):
            groups[s["campaign"]].append(s)
        out = []
        for name, items in groups.items():
            opened = [s for s in items if s["opens"] > 0]
            last_opens = [s["last_open"] for s in opened]
            ttfo = [s["first_open"] - s["sent_at"] for s in opened if s["sent_at"] is not None]
            sent = [s["sent_at"] for s in items if s["sent_at"] is not None]
            out.append(
                {
                    "name": name,
                    "emails": len(items),
                    "opened": len(opened),
                    "open_rate": len(opened) / len(items) if items else 0,
                    "opens": sum(s["opens"] for s in items),
                    "prefetch_only": sum(1 for s in items if s["status"] == "prefetch_only"),
                    "first_sent": min(sent) if sent else None,
                    "last_sent": max(sent) if sent else None,
                    "last_open": max(last_opens) if last_opens else None,
                    "median_time_to_open": statistics.median(ttfo) if ttfo else None,
                }
            )
        out.sort(key=lambda c: c["last_sent"] or 0, reverse=True)
        return out

    @app.get("/api/campaign-names")
    def campaign_names(p: Principal = Depends(principal)):
        """Names only, for the compose picker. Every plan can tag emails."""
        rows = db.query(
            "SELECT campaign, MAX(sent_at) AS last FROM emails WHERE account_id IS ? AND campaign IS NOT NULL "
            "AND campaign != '' GROUP BY campaign ORDER BY last DESC LIMIT 50",
            (p.account_id,),
        )
        return [r["campaign"] for r in rows]

    @app.get("/api/senders")
    def senders(p: Principal = Depends(principal)):
        need(p, "senders")
        rows = db.query(
            "SELECT lower(sender) AS s, COUNT(*) AS n, MAX(sent_at) AS last FROM emails "
            "WHERE account_id IS ? AND sender IS NOT NULL AND sender != '' GROUP BY lower(sender) ORDER BY last DESC",
            (p.account_id,),
        )
        return [{"sender": r["s"], "emails": r["n"], "last_sent": r["last"]} for r in rows]

    # ---- sender health (Pro in the cloud) ---------------------------------

    @app.get("/api/senders/health")
    def senders_health(
        p: Principal = Depends(principal),
        sender: str | None = Query(None, max_length=320),
        dns: bool = Query(True, description="false skips the DNS checks (fast, for the extension)"),
    ):
        need(p, "senders")
        now = time.time()
        groups: dict[str, list[dict]] = defaultdict(list)
        for s, _ in store.summaries(p.account_id, since=now - 35 * 86400):
            addr = (s["sender"] or "").lower()
            if addr and "@" in addr and (not sender or addr == sender.strip().lower()):
                groups[addr].append(s)
        acct_key = p.account_id or ""
        # The ramp starts at the first tracked email ever, not only the last 35 days.
        firsts = {
            r["s"]: r["first"]
            for r in db.query(
                "SELECT lower(sender) AS s, MIN(sent_at) AS first FROM emails WHERE account_id IS ? "
                "AND sender IS NOT NULL GROUP BY lower(sender)",
                (p.account_id,),
            )
        }
        if sender and not groups and sender.strip().lower() in firsts:
            groups[sender.strip().lower()] = []
        established = {
            r["sender"]
            for r in db.query("SELECT sender FROM sender_settings WHERE acct = ? AND established = 1", (acct_key,))
        }
        dns_results = dns_cache.many([a.split("@", 1)[1] for a in groups]) if dns else {}
        out = []
        for addr, emails in groups.items():
            rep_ = health.report(addr, emails, dns_results.get(addr.split("@", 1)[1]), addr in established, now)
            first = firsts.get(addr)
            if first is not None and not rep_["established"]:
                rep_["ramp"] = health.ramp(first, rep_["ramp"]["sent_24h"], False, now)
                rep_["first_sent"] = first
            out.append(rep_)
        order = {"bad": 0, "warn": 1, "ok": 2}
        out.sort(key=lambda r: (order[r["status"]], -r["emails"]))
        return out

    @app.put("/api/senders/health/{sender}")
    def set_sender(sender: str, body: SenderSettingsIn, p: Principal = Depends(principal)):
        need(p, "senders")
        addr = sender.strip().lower()
        if "@" not in addr or len(addr) > 320:
            raise HTTPException(status_code=422, detail="not an email address")
        db.execute(
            "INSERT INTO sender_settings(acct, sender, established) VALUES (?, ?, ?) "
            "ON CONFLICT(acct, sender) DO UPDATE SET established = excluded.established",
            (p.account_id or "", addr, 1 if body.established else 0),
        )
        return {"ok": True, "sender": addr, "established": body.established}

    @app.patch("/api/campaigns/{name}")
    def rename_campaign(name: str, body: CampaignRename, p: Principal = Depends(principal)):
        need(p, "campaigns")
        if name == NO_CAMPAIGN:
            cur = db.execute(
                "UPDATE emails SET campaign = ? WHERE account_id IS ? AND (campaign IS NULL OR campaign = '')",
                (body.name, p.account_id),
            )
        else:
            cur = db.execute(
                "UPDATE emails SET campaign = ? WHERE account_id IS ? AND campaign = ?", (body.name, p.account_id, name)
            )
        return {"ok": True, "updated": cur.rowcount}

    @app.get("/api/stats")
    def stats(
        p: Principal = Depends(principal),
        campaign: str | None = Query(None, max_length=200),
        sender: str | None = Query(None, max_length=320),
        days: int = Query(30, ge=1, le=365),
        tz: int = Query(0, ge=-14 * 60, le=14 * 60, description="minutes east of UTC"),
    ):
        need(p, "stats")
        pairs = store.summaries(p.account_id, campaign, sender=sender)
        items = [s for s, _ in pairs]
        now = time.time()
        today = datetime.fromtimestamp(now + tz * 60, tz=timezone.utc).date()
        keys = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
        sent_by_day = dict.fromkeys(keys, 0)
        opens_by_day = dict.fromkeys(keys, 0)
        opens_by_hour = [0] * 24
        recent: list[dict] = []
        for s, hits in pairs:
            if s["sent_at"] is not None:
                k = _day_key(s["sent_at"], tz)
                if k in sent_by_day:
                    sent_by_day[k] += 1
            for h in hits:
                if h["kind"] != "open":
                    continue
                k = _day_key(h["ts"], tz)
                if k in opens_by_day:  # the hour chart honours the same window as the timeline
                    opens_by_day[k] += 1
                    opens_by_hour[_hour(h["ts"], tz)] += 1
                recent.append(
                    {
                        "email_id": s["id"],
                        "subject": s["subject"],
                        "campaign": s["campaign"],
                        "sender": s["sender"],
                        "recipients": s["recipients"],
                        "ts": h["ts"],
                        "via": h["via"],
                    }
                )
        recent.sort(key=lambda r: r["ts"], reverse=True)
        opened = [s for s in items if s["opens"] > 0]
        ttfo = [s["first_open"] - s["sent_at"] for s in opened if s["sent_at"] is not None]
        return {
            "emails": len(items),
            "opened": len(opened),
            "open_rate": len(opened) / len(items) if items else 0,
            "opens": sum(s["opens"] for s in items),
            "prefetch_only": sum(1 for s in items if s["status"] == "prefetch_only"),
            "self": sum(s["self"] for s in items),
            "opens_last_7d": sum(1 for r in recent if r["ts"] >= now - 7 * 86400),
            "median_time_to_open": statistics.median(ttfo) if ttfo else None,
            "timeline": [{"date": k, "sent": sent_by_day[k], "opens": opens_by_day[k]} for k in keys],
            "opens_by_hour": opens_by_hour,
            "recent_opens": recent[:20],
        }

    if cloud is not None:
        cloud.register_routes(app, principal)

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PATCH", "DELETE"], dependencies=auth)
    def api_not_found(rest: str):
        raise HTTPException(status_code=404, detail="not found")

    # ---- dashboard (static, data only through the authenticated API) -----

    if cfg.dashboard and DASHBOARD_DIR.is_dir():
        dashboard_csp = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        files = {p.name: p for p in DASHBOARD_DIR.iterdir() if p.is_file()}

        # Explicit types: the slim image's mimetypes table lacks woff2 and svg can vary.
        types = {".woff2": "font/woff2", ".svg": "image/svg+xml", ".css": "text/css", ".js": "text/javascript"}

        def _serve(name: str) -> FileResponse:
            resp = FileResponse(files[name], media_type=types.get(Path(name).suffix))
            resp.headers["Content-Security-Policy"] = dashboard_csp
            resp.headers["Cache-Control"] = "no-cache"
            return resp

        @app.get("/", include_in_schema=False)
        def dashboard_index():
            return _serve("index.html")

        @app.get("/assets/{name}", include_in_schema=False)
        def dashboard_asset(name: str):
            if name not in files or name == "index.html":
                raise HTTPException(status_code=404)
            return _serve(name)

    return app


# Run with: uvicorn app:create_app --factory
# create_app() raises when LIKELYREAD_TOKEN is missing in self-host mode, so the server refuses to start.
