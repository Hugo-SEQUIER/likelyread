import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient

import app as likelyread

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
APPLE_UA = "Mozilla/5.0"
GMAIL_UA = "Mozilla/5.0 (Windows NT 5.1; rv:11.0) Gecko Firefox/11.0 (via ggpht.com GoogleImageProxy)"
DIRECT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko)"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LIKELYREAD_TOKEN", TOKEN)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("PIXEL_RATE_PER_MIN", "1000")
    return monkeypatch


@pytest.fixture
def clock(monkeypatch):
    state = {"now": 1_800_000_000.0}
    monkeypatch.setattr(likelyread.time, "time", lambda: state["now"])
    return state


@pytest.fixture
def client(env, clock):
    return TestClient(likelyread.create_app())


def new_id() -> str:
    return str(uuid.uuid4())


def register(client, eid, **kw):
    body = {"id": eid, "subject": kw.get("subject", "Hello"), "recipients": kw.get("recipients", ["a@x.com"]), "age": kw.get("age", 0)}
    if "campaign" in kw:
        body["campaign"] = kw["campaign"]
    r = client.post("/api/emails", json=body, headers=AUTH)
    assert r.status_code == 200, r.text
    return r


def hit(client, eid, ua=DIRECT_UA):
    r = client.get(f"/p/{eid}.gif", headers={"User-Agent": ua})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/gif"
    return r


# ---- startup and auth --------------------------------------------------------


def test_refuses_to_start_without_token(tmp_path, monkeypatch):
    monkeypatch.delenv("LIKELYREAD_TOKEN", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "x.db"))
    with pytest.raises(RuntimeError):
        likelyread.create_app()


def test_healthz_public(client):
    assert client.get("/healthz").json() == {"ok": True}


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/ping"),
        ("get", "/api/emails"),
        ("post", "/api/emails"),
        ("get", f"/api/emails/{uuid.uuid4()}"),
        ("delete", f"/api/emails/{uuid.uuid4()}"),
        ("post", f"/api/emails/{uuid.uuid4()}/suppress"),
        ("get", "/api/campaigns"),
        ("get", "/api/stats"),
        ("get", "/api/does-not-exist"),
    ],
)
def test_api_requires_token(client, method, path):
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert getattr(client, method)(path, headers={"Authorization": TOKEN}).status_code == 401


def test_ping_ok(client):
    assert client.get("/api/ping", headers=AUTH).json()["ok"] is True


def test_docs_disabled(client):
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(p).status_code == 404


# ---- pixel -------------------------------------------------------------------


def test_pixel_is_43_byte_gif_with_no_cache(client):
    r = hit(client, new_id())
    assert len(r.content) == 43 and r.content.startswith(b"GIF89a")
    assert "no-store" in r.headers["cache-control"]
    assert r.headers["pragma"] == "no-cache"
    assert r.headers["expires"] == "0"


@pytest.mark.parametrize("name", ["not-a-uuid.gif", "x", "1234.png", f"{uuid.uuid1()}.gif", "%00.gif"])
def test_pixel_invalid_ids_still_return_gif_but_are_not_logged(client, name):
    r = client.get(f"/p/{name}")
    assert r.status_code == 200 and len(r.content) == 43
    assert client.app.state.db.query("SELECT COUNT(*) c FROM hits")[0]["c"] == 0


def test_hits_for_unknown_ids_are_kept_until_registration(client, clock):
    eid = new_id()
    clock["now"] += 100
    hit(client, eid)  # arrives before registration
    register(client, eid, age=200)  # actually sent 200s ago
    data = client.get(f"/api/emails/{eid}", headers=AUTH).json()
    assert data["opens"] == 1


def test_store_ip_false(env, clock):
    env.setenv("STORE_IP", "false")
    c = TestClient(likelyread.create_app())
    eid = new_id()
    register(c, eid)
    clock["now"] += 100
    hit(c, eid)
    assert c.get(f"/api/emails/{eid}", headers=AUTH).json()["hits"][0]["ip"] is None


def test_pixel_rate_limit(env, clock):
    env.setenv("PIXEL_RATE_PER_MIN", "3")
    c = TestClient(likelyread.create_app())
    eid = new_id()
    for _ in range(5):
        hit(c, eid)
    assert c.app.state.db.query("SELECT COUNT(*) c FROM hits")[0]["c"] == 3
    clock["now"] += 61
    hit(c, eid)
    assert c.app.state.db.query("SELECT COUNT(*) c FROM hits")[0]["c"] == 4


def test_ua_truncated(client):
    eid = new_id()
    hit(client, eid, ua="A" * 2000)
    assert len(client.app.state.db.query("SELECT ua FROM hits")[0]["ua"]) == 512


# ---- classification ----------------------------------------------------------


def test_classification_self_prefetch_open(client, clock):
    eid = new_id()
    register(client, eid)
    clock["now"] += 5
    hit(client, eid)  # compose preview, inside grace window -> self
    clock["now"] += 100
    hit(client, eid, ua=APPLE_UA)  # Apple MPP -> prefetch
    clock["now"] += 100
    hit(client, eid, ua=GMAIL_UA)  # real open through Gmail proxy
    clock["now"] += 100
    client.post(f"/api/emails/{eid}/suppress", headers=AUTH)
    clock["now"] += 2
    hit(client, eid, ua=GMAIL_UA)  # my own view right after suppress -> self
    clock["now"] += 100
    hit(client, eid)  # later direct open

    d = client.get(f"/api/emails/{eid}", headers=AUTH).json()
    kinds = [h["kind"] for h in d["hits"]]
    vias = [h["via"] for h in d["hits"]]
    assert kinds == ["self", "prefetch", "open", "self", "open"]
    assert vias == ["direct", "direct", "gmail_proxy", "gmail_proxy", "direct"]
    assert (d["opens"], d["prefetch"], d["self"], d["status"]) == (2, 1, 2, "opened")
    assert d["first_open"] < d["last_open"]


def test_suppress_window_edges(client, clock):
    eid = new_id()
    register(client, eid)
    clock["now"] += 1000
    client.post(f"/api/emails/{eid}/suppress", headers=AUTH)
    clock["now"] -= 4  # 4s before suppress: self (5s lead tolerance)
    hit(client, eid)
    clock["now"] += 4 + 60  # exactly at window end: self
    hit(client, eid)
    clock["now"] += 1  # past window: open
    hit(client, eid)
    kinds = [h["kind"] for h in client.get(f"/api/emails/{eid}", headers=AUTH).json()["hits"]]
    assert kinds == ["self", "self", "open"]


def test_prefetch_only_status(client, clock):
    eid = new_id()
    register(client, eid)
    clock["now"] += 300
    hit(client, eid, ua=APPLE_UA)
    s = client.get("/api/emails", headers=AUTH).json()[0]
    assert s["status"] == "prefetch_only" and s["opens"] == 0


def test_classification_reads_current_config(env, clock):
    """Rules are computed at read time: changing GRACE_SECONDS reclassifies old hits."""
    c = TestClient(likelyread.create_app())
    eid = new_id()
    register(c, eid)
    clock["now"] += 30
    hit(c, eid)
    assert c.get(f"/api/emails/{eid}", headers=AUTH).json()["opens"] == 1
    env.setenv("GRACE_SECONDS", "60")
    c2 = TestClient(likelyread.create_app())
    assert c2.get(f"/api/emails/{eid}", headers=AUTH).json()["opens"] == 0


# ---- registration ------------------------------------------------------------


def test_register_is_idempotent_and_uses_relative_age(client, clock):
    eid = new_id()
    register(client, eid, age=0)
    clock["now"] += 50
    register(client, eid, age=50)  # retry 50s later reports the same send time
    rows = client.app.state.db.query("SELECT sent_at FROM emails")
    assert len(rows) == 1 and rows[0]["sent_at"] == pytest.approx(1_800_000_000.0)


@pytest.mark.parametrize(
    "body",
    [
        {"id": "nope"},
        {"id": str(uuid.uuid1())},
        {"id": str(uuid.uuid4()), "age": -1},
        {"id": str(uuid.uuid4()), "age": 10**9},
        {"id": str(uuid.uuid4()), "recipients": "a@b.c"},
    ],
)
def test_register_validation(client, body):
    assert client.post("/api/emails", json=body, headers=AUTH).status_code == 422


def test_delete_email_removes_hits(client):
    eid = new_id()
    register(client, eid)
    hit(client, eid)
    assert client.delete(f"/api/emails/{eid}", headers=AUTH).status_code == 200
    assert client.get(f"/api/emails/{eid}", headers=AUTH).status_code == 404
    assert client.app.state.db.query("SELECT COUNT(*) c FROM hits")[0]["c"] == 0
    assert client.delete(f"/api/emails/{eid}", headers=AUTH).status_code == 404


def test_list_limit_order_and_total(client, clock):
    ids = []
    for i in range(5):
        eid = new_id()
        ids.append(eid)
        register(client, eid, subject=f"s{i}")
        clock["now"] += 10
    r = client.get("/api/emails?limit=2", headers=AUTH)
    assert [e["subject"] for e in r.json()] == ["s4", "s3"]
    assert r.headers["x-total-count"] == "5"
    r = client.get("/api/emails?limit=2&offset=4", headers=AUTH)
    assert [e["subject"] for e in r.json()] == ["s0"]


# ---- campaigns and stats -----------------------------------------------------


def seed_campaigns(client, clock):
    a1, a2, b1, n1 = new_id(), new_id(), new_id(), new_id()
    register(client, a1, campaign="Launch  Week", recipients=["ceo@acme.com"])
    register(client, a2, campaign="Launch Week")
    register(client, b1, campaign="Follow-ups")
    register(client, n1)
    clock["now"] += 3600
    hit(client, a1, ua=GMAIL_UA)
    hit(client, a1, ua=GMAIL_UA)
    hit(client, b1, ua=APPLE_UA)
    return a1, a2, b1, n1


def test_campaign_name_is_normalised_and_grouped(client, clock):
    seed_campaigns(client, clock)
    camps = {c["name"]: c for c in client.get("/api/campaigns", headers=AUTH).json()}
    assert set(camps) == {"Launch Week", "Follow-ups", None}
    lw = camps["Launch Week"]
    assert (lw["emails"], lw["opened"], lw["opens"], lw["open_rate"]) == (2, 1, 2, 0.5)
    assert lw["median_time_to_open"] == pytest.approx(3600)
    assert camps["Follow-ups"]["prefetch_only"] == 1 and camps["Follow-ups"]["opened"] == 0


def test_filters(client, clock):
    a1, a2, b1, n1 = seed_campaigns(client, clock)
    get = lambda qs: {e["id"] for e in client.get(f"/api/emails?{qs}", headers=AUTH).json()}
    assert get("campaign=Launch%20Week") == {a1, a2}
    assert get("campaign=__none__") == {n1}
    assert get("status=opened") == {a1}
    assert get("status=prefetch_only") == {b1}
    assert get("status=not_opened") == {a2, n1}
    assert get("q=acme") == {a1}
    assert client.get("/api/emails?status=bogus", headers=AUTH).status_code == 422


def test_patch_campaign_and_rename(client, clock):
    a1, a2, b1, n1 = seed_campaigns(client, clock)
    r = client.patch(f"/api/emails/{n1}", json={"campaign": "Follow-ups"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["campaign"] == "Follow-ups"
    r = client.patch("/api/campaigns/Follow-ups", json={"name": "Q4 follow-ups"}, headers=AUTH)
    assert r.json()["updated"] == 2
    names = {c["name"] for c in client.get("/api/campaigns", headers=AUTH).json()}
    assert names == {"Launch Week", "Q4 follow-ups"}
    client.patch(f"/api/emails/{a2}", json={"campaign": None}, headers=AUTH)
    assert get_status(client, a2)["campaign"] is None
    assert client.patch(f"/api/emails/{new_id()}", json={"campaign": "x"}, headers=AUTH).status_code == 404


def get_status(client, eid):
    return client.get(f"/api/emails/{eid}", headers=AUTH).json()


def test_reregistration_keeps_dashboard_campaign_when_none_sent(client, clock):
    eid = new_id()
    register(client, eid)
    client.patch(f"/api/emails/{eid}", json={"campaign": "Manual"}, headers=AUTH)
    register(client, eid, age=5)  # a late retry without a campaign must not wipe it
    assert get_status(client, eid)["campaign"] == "Manual"


def test_stats(client, clock):
    seed_campaigns(client, clock)
    s = client.get("/api/stats?days=7", headers=AUTH).json()
    assert (s["emails"], s["opened"], s["opens"], s["prefetch_only"]) == (4, 1, 2, 1)
    assert s["open_rate"] == 0.25
    assert len(s["timeline"]) == 7
    assert sum(d["sent"] for d in s["timeline"]) == 4
    assert sum(d["opens"] for d in s["timeline"]) == 2
    assert sum(s["opens_by_hour"]) == 2
    assert s["recent_opens"][0]["subject"] == "Hello"
    s = client.get("/api/stats?campaign=Follow-ups", headers=AUTH).json()
    assert (s["emails"], s["opened"]) == (1, 0)


def test_stats_timezone_bucketing(client, clock):
    clock["now"] = 1_800_000_000.0  # 2027-01-15 08:00 UTC
    eid = new_id()
    register(client, eid)
    clock["now"] += 15 * 3600 + 100  # 23:01 UTC, 08:01 next day in Seoul
    hit(client, eid)
    utc = client.get("/api/stats?days=2&tz=0", headers=AUTH).json()
    kst = client.get("/api/stats?days=2&tz=540", headers=AUTH).json()
    assert utc["opens_by_hour"][23] == 1
    assert kst["opens_by_hour"][8] == 1
    assert [d["opens"] for d in kst["timeline"]] == [0, 1]


# ---- retention ---------------------------------------------------------------


def test_retention_purge(env, clock):
    env.setenv("RETENTION_DAYS", "1")
    c = TestClient(likelyread.create_app())
    eid = new_id()
    hit(c, eid)
    clock["now"] += 2 * 86400
    hit(c, eid)
    n = likelyread.purge_old(c.app.state.db, c.app.state.cfg)
    assert n == 1
    assert c.app.state.db.query("SELECT COUNT(*) c FROM hits")[0]["c"] == 1


# ---- dashboard and headers ---------------------------------------------------


def test_dashboard_served_with_csp(client):
    r = client.get("/")
    assert r.status_code == 200 and "likelyread" in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/assets/../app.py").status_code == 404
    assert client.get("/assets/nope.js").status_code == 404


def test_dashboard_can_be_disabled(env, clock):
    env.setenv("DASHBOARD", "false")
    c = TestClient(likelyread.create_app())
    assert c.get("/").status_code == 404


def test_cors_only_for_configured_origin(env, clock):
    env.setenv("ALLOWED_ORIGINS", "chrome-extension://abc")
    c = TestClient(likelyread.create_app())
    ok = c.options("/api/ping", headers={"Origin": "chrome-extension://abc", "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == "chrome-extension://abc"
    bad = c.get("/api/ping", headers={"Origin": "https://evil.example", **AUTH})
    assert "access-control-allow-origin" not in bad.headers


# ---- review fixes ------------------------------------------------------------


def test_reserved_campaign_name_is_uncategorized(client):
    eid = new_id()
    register(client, eid, campaign="__none__")
    assert get_status(client, eid)["campaign"] is None
    assert [c["name"] for c in client.get("/api/campaigns", headers=AUTH).json()] == [None]


def test_orphan_hits_purged_even_without_retention(client, clock):
    known, orphan = new_id(), new_id()
    register(client, known)
    hit(client, known)
    hit(client, orphan)
    clock["now"] += 7 * 86400
    assert likelyread.purge_old(client.app.state.db, client.app.state.cfg) == 0, "kept while registration may still arrive"
    clock["now"] += 2 * 86400
    assert likelyread.purge_old(client.app.state.db, client.app.state.cfg) == 1
    left = client.app.state.db.query("SELECT email_id FROM hits")
    assert [r["email_id"] for r in left] == [known]


def test_retention_also_drops_old_email_metadata(env, clock):
    env.setenv("RETENTION_DAYS", "1")
    c = TestClient(likelyread.create_app())
    old = new_id()
    register(c, old)
    clock["now"] += 2 * 86400
    fresh = new_id()
    register(c, fresh)
    likelyread.purge_old(c.app.state.db, c.app.state.cfg)
    assert {e["id"] for e in c.get("/api/emails", headers=AUTH).json()} == {fresh}


def test_rate_limiter_never_resets_live_counters():
    rl = likelyread.RateLimiter(2)
    rl.MAX_KEYS = 3
    assert rl.allow("a", 0) and rl.allow("a", 0) and not rl.allow("a", 0)
    assert rl.allow("b", 0) and rl.allow("c", 0)
    assert not rl.allow("d", 1), "table full of live windows: new key refused, not reset"
    assert not rl.allow("a", 1), "a is still limited"
    assert rl.allow("d", 61), "expired windows are evicted"
