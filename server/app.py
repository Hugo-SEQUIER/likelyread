"""likelyread server: self-hosted email open tracking for a single Gmail user.

One process, one SQLite file, one bearer token. See SPEC.md for the contract.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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
  sender      TEXT
);
CREATE TABLE IF NOT EXISTS hits (
  id        INTEGER PRIMARY KEY,
  email_id  TEXT,
  ts        REAL,
  ip        TEXT NULL,
  ua        TEXT
);
CREATE TABLE IF NOT EXISTS suppressions (
  email_id  TEXT,
  ts        REAL
);
CREATE INDEX IF NOT EXISTS idx_hits_email ON hits(email_id);
CREATE INDEX IF NOT EXISTS idx_hits_ts ON hits(ts);
CREATE INDEX IF NOT EXISTS idx_supp_email ON suppressions(email_id);
CREATE INDEX IF NOT EXISTS idx_emails_sent ON emails(sent_at);
"""

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------


class DB:
    """A single SQLite connection guarded by a lock. Plenty for one user."""

    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(emails)")}
            if "campaign" not in cols:  # databases created by a pre-campaign build
                self.conn.execute("ALTER TABLE emails ADD COLUMN campaign TEXT")
            if "sender" not in cols:  # databases created before the sender address was stored
                self.conn.execute("ALTER TABLE emails ADD COLUMN sender TEXT")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_campaign ON emails(campaign)")

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
    ts: float, ua: str | None, sent_at: float | None, suppressions: list[float], cfg: Settings, via: str = "direct"
) -> tuple[str, str]:
    """Return (kind, reason). The first rule that matches wins."""
    if sent_at is not None and sent_at - 10 <= ts <= sent_at + cfg.grace_seconds:
        return "self", f"Within {cfg.grace_seconds:g} s of sending: your own compose window."
    for s in suppressions:
        if s - 5 <= ts <= s + cfg.suppress_window_seconds:
            return "self", f"{max(0, ts - s):.0f} s after you opened it yourself."
    if (ua or "").strip() == APPLE_MPP_UA:
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
    return any(addr in net for net in GOOGLE_PROXY_NETS)


def hit_via(ua: str | None, ip: str | None = None) -> str:
    ua = ua or ""
    if any(m in ua for m in GMAIL_PROXY_UA_MARKERS) or _is_google_ip(ip):
        return "gmail_proxy"
    return "direct"


def status_of(summary: dict) -> str:
    if summary["opens"] > 0:
        return "opened"
    if summary["prefetch"] > 0:
        return "prefetch_only"
    return "not_opened"


class Store:
    """Read/write helpers on top of DB, including classification."""

    def __init__(self, db: DB, cfg: Settings) -> None:
        self.db = db
        self.cfg = cfg

    # ---- loading -------------------------------------------------------

    def _emails(self, campaign: str | None = None, q: str | None = None, sender: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT id, subject, recipients, sent_at, campaign, sender FROM emails"
        where, params = [], []
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
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY sent_at DESC"
        return self.db.query(sql, params)

    def _hits_and_supps(self, ids: list[str]) -> tuple[dict[str, list[sqlite3.Row]], dict[str, list[float]]]:
        hits: dict[str, list[sqlite3.Row]] = defaultdict(list)
        supps: dict[str, list[float]] = defaultdict(list)
        if not ids:
            return hits, supps
        # Chunk to stay under SQLite's parameter limit.
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            for r in self.db.query(
                f"SELECT email_id, ts, ip, ua FROM hits WHERE email_id IN ({marks}) ORDER BY ts", chunk
            ):
                hits[r["email_id"]].append(r)
            for r in self.db.query(f"SELECT email_id, ts FROM suppressions WHERE email_id IN ({marks})", chunk):
                supps[r["email_id"]].append(r["ts"])
        return hits, supps

    def _classified(self, email: sqlite3.Row, hits: list[sqlite3.Row], supps: list[float]) -> list[dict]:
        out = []
        for h in hits:
            via = hit_via(h["ua"], h["ip"])
            kind, reason = explain_hit(h["ts"], h["ua"], email["sent_at"], supps, self.cfg, via)
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
        self, campaign: str | None = None, q: str | None = None, sender: str | None = None
    ) -> list[tuple[dict, list[dict]]]:
        emails = self._emails(campaign, q, sender)
        hits, supps = self._hits_and_supps([e["id"] for e in emails])
        result = []
        for e in emails:
            classified = self._classified(e, hits.get(e["id"], []), supps.get(e["id"], []))
            result.append((self._summary(e, classified), classified))
        return result

    def one(self, email_id: str) -> dict | None:
        rows = self.db.query("SELECT id, subject, recipients, sent_at, campaign, sender FROM emails WHERE id = ?", (email_id,))
        if not rows:
            return None
        e = rows[0]
        hits, supps = self._hits_and_supps([email_id])
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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def retention_loop():
            while True:
                try:
                    await asyncio.to_thread(purge_old, db, cfg)
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
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    token_bytes = cfg.token.encode()

    def require_token(request: Request) -> None:
        header = request.headers.get("authorization", "")
        scheme, _, supplied = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip().encode(), token_bytes):
            raise HTTPException(status_code=401, detail="invalid token", headers={"WWW-Authenticate": "Bearer"})

    auth = [Depends(require_token)]

    # ---- public ------------------------------------------------------

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/p/{name}")
    def pixel(name: str, request: Request):
        email_id = name[:-4].lower() if name.endswith(".gif") else ""
        ip = request.client.host if request.client else None
        if UUID4_RE.match(email_id) and limiter.allow(ip or "?"):
            ua = (request.headers.get("user-agent") or "")[:UA_MAX]
            try:
                db.execute(
                    "INSERT INTO hits(email_id, ts, ip, ua) VALUES (?, ?, ?, ?)",
                    (email_id, time.time(), ip if cfg.store_ip else None, ua),
                )
            except sqlite3.Error as exc:  # never break the image
                print(f"hit insert failed: {exc}")
        return Response(content=PIXEL_GIF, media_type="image/gif", headers=PIXEL_HEADERS)

    # ---- authenticated API ---------------------------------------------

    @app.get("/api/ping", dependencies=auth)
    def ping():
        return {"ok": True, "time": time.time()}

    @app.post("/api/emails", dependencies=auth)
    def register(body: EmailIn):
        sent_at = time.time() - body.age
        # Upsert: a retried registration carries the same data; a re-send after a
        # failed or undone send updates the record to the real send.
        db.execute(
            """INSERT INTO emails(id, subject, recipients, sent_at, campaign, sender) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET subject=excluded.subject, recipients=excluded.recipients,
               sent_at=excluded.sent_at, campaign=COALESCE(excluded.campaign, emails.campaign),
               sender=COALESCE(excluded.sender, emails.sender)""",
            (body.id, body.subject, json.dumps(body.recipients), sent_at, body.campaign, body.sender),
        )
        return {"ok": True, "id": body.id, "sent_at": sent_at}

    @app.post("/api/emails/{email_id}/suppress", dependencies=auth)
    def suppress(email_id: str):
        email_id = email_id.lower()
        if not UUID4_RE.match(email_id):
            raise HTTPException(status_code=422, detail="id must be a UUIDv4")
        db.execute("INSERT INTO suppressions(email_id, ts) VALUES (?, ?)", (email_id, time.time()))
        return {"ok": True}

    @app.get("/api/emails", dependencies=auth)
    def list_emails(
        response: Response,
        limit: int = Query(50, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        campaign: str | None = Query(None, max_length=200),
        q: str | None = Query(None, max_length=200),
        status: str | None = Query(None, pattern="^(opened|not_opened|prefetch_only)$"),
        sender: str | None = Query(None, max_length=320),
    ):
        items = [s for s, _ in store.summaries(campaign, q, sender)]
        if status:
            items = [s for s in items if s["status"] == status]
        response.headers["X-Total-Count"] = str(len(items))
        return items[offset : offset + limit]

    @app.get("/api/emails/{email_id}", dependencies=auth)
    def get_email(email_id: str):
        found = store.one(email_id.lower())
        if not found:
            raise HTTPException(status_code=404, detail="not found")
        return found

    @app.patch("/api/emails/{email_id}", dependencies=auth)
    def patch_email(email_id: str, body: EmailPatch):
        email_id = email_id.lower()
        cur = db.execute("UPDATE emails SET campaign = ? WHERE id = ?", (body.campaign, email_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="not found")
        return store.one(email_id)

    @app.delete("/api/emails/{email_id}", dependencies=auth)
    def delete_email(email_id: str):
        email_id = email_id.lower()
        cur = db.execute("DELETE FROM emails WHERE id = ?", (email_id,))
        db.execute("DELETE FROM hits WHERE email_id = ?", (email_id,))
        db.execute("DELETE FROM suppressions WHERE email_id = ?", (email_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="not found")
        return {"ok": True}

    # ---- campaigns and dashboard stats ----------------------------------

    @app.get("/api/campaigns", dependencies=auth)
    def campaigns():
        groups: dict[str | None, list[dict]] = defaultdict(list)
        for s, _ in store.summaries():
            groups[s["campaign"]].append(s)
        out = []
        for name, items in groups.items():
            opened = [s for s in items if s["opens"] > 0]
            last_opens = [s["last_open"] for s in opened]
            ttfo = [s["first_open"] - s["sent_at"] for s in opened if s["sent_at"] is not None]
            out.append(
                {
                    "name": name,
                    "emails": len(items),
                    "opened": len(opened),
                    "open_rate": len(opened) / len(items) if items else 0,
                    "opens": sum(s["opens"] for s in items),
                    "prefetch_only": sum(1 for s in items if s["status"] == "prefetch_only"),
                    "first_sent": min(s["sent_at"] for s in items),
                    "last_sent": max(s["sent_at"] for s in items),
                    "last_open": max(last_opens) if last_opens else None,
                    "median_time_to_open": statistics.median(ttfo) if ttfo else None,
                }
            )
        out.sort(key=lambda c: c["last_sent"], reverse=True)
        return out

    @app.get("/api/senders", dependencies=auth)
    def senders():
        rows = db.query(
            "SELECT lower(sender) AS s, COUNT(*) AS n, MAX(sent_at) AS last FROM emails "
            "WHERE sender IS NOT NULL AND sender != '' GROUP BY lower(sender) ORDER BY last DESC"
        )
        return [{"sender": r["s"], "emails": r["n"], "last_sent": r["last"]} for r in rows]

    @app.patch("/api/campaigns/{name}", dependencies=auth)
    def rename_campaign(name: str, body: CampaignRename):
        if name == NO_CAMPAIGN:
            cur = db.execute("UPDATE emails SET campaign = ? WHERE campaign IS NULL OR campaign = ''", (body.name,))
        else:
            cur = db.execute("UPDATE emails SET campaign = ? WHERE campaign = ?", (body.name, name))
        return {"ok": True, "updated": cur.rowcount}

    @app.get("/api/stats", dependencies=auth)
    def stats(
        campaign: str | None = Query(None, max_length=200),
        sender: str | None = Query(None, max_length=320),
        days: int = Query(30, ge=1, le=365),
        tz: int = Query(0, ge=-14 * 60, le=14 * 60, description="minutes east of UTC"),
    ):
        pairs = store.summaries(campaign, sender=sender)
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
                if k in opens_by_day:
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

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PATCH", "DELETE"], dependencies=auth)
    def api_not_found(rest: str):
        raise HTTPException(status_code=404, detail="not found")

    # ---- dashboard (static, data only through the authenticated API) -----

    if cfg.dashboard and DASHBOARD_DIR.is_dir():
        dashboard_csp = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        )
        files = {p.name: p for p in DASHBOARD_DIR.iterdir() if p.is_file()}

        def _serve(name: str) -> FileResponse:
            resp = FileResponse(files[name])
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
# create_app() raises when LIKELYREAD_TOKEN is missing, so the server refuses to start.
