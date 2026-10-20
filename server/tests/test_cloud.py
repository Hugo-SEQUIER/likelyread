"""Cloud mode: accounts, isolation, plans, Google sign-in and Polar billing.
Google and Polar are replaced with fakes; nothing leaves the machine."""

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import uuid

import pytest
from fastapi.testclient import TestClient

import app as likelyread
import cloud

SECRET = "s" * 40
WEBHOOK_SECRET = "polar_whs_test_secret"
CLIENT_ID = "cid.apps.googleusercontent.com"
NOW = 1_800_000_000.0


@pytest.fixture
def clock(monkeypatch):
    state = {"now": NOW}
    monkeypatch.setattr(likelyread.time, "time", lambda: state["now"])
    monkeypatch.setattr(cloud.time, "time", lambda: state["now"])
    return state


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in {
        "MODE": "cloud",
        "DB_PATH": str(tmp_path / "cloud.db"),
        "PIXEL_RATE_PER_MIN": "1000",
        "PUBLIC_URL": "https://app.likelyread.com",
        "SESSION_SECRET": SECRET,
        "GOOGLE_CLIENT_ID": CLIENT_ID,
        "GOOGLE_CLIENT_SECRET": "gsecret",
        "POLAR_ACCESS_TOKEN": "polar_oat_test",
        "POLAR_WEBHOOK_SECRET": WEBHOOK_SECRET,
        "POLAR_PRODUCT_PRO_MONTHLY": "prod_month",
        "POLAR_PRODUCT_PRO_YEARLY": "prod_year",
        "POLAR_PRODUCT_LIFETIME": "prod_life",
        "ADMIN_EMAILS": "boss@likelyread.com",
        "LIFETIME_CAP": "2",
        "EXTENSION_IDS": "abcdefghijklmnopabcdefghijklmnop",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("LIKELYREAD_TOKEN", raising=False)
    return monkeypatch


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.google = {}  # code -> claims
        self.cancel_status = 200

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if "oauth2" in url:
            form = dict(urllib.parse.parse_qsl(body.decode()))
            claims = self.google.get(form["code"])
            if not claims:
                return 400, {"error": "invalid_grant"}
            enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
            return 200, {"id_token": f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig", "access_token": "x"}
        if url.endswith("/v1/checkouts/"):
            return 201, {"url": "https://polar.sh/checkout/abc", "id": "chk_1"}
        if "/v1/subscriptions/" in url and method == "DELETE":
            return self.cancel_status, {}
        if url.endswith("/v1/customer-sessions/"):
            return 201, {"customer_portal_url": "https://polar.sh/portal/xyz"}
        return 404, {}


@pytest.fixture
def fake():
    return FakeHTTP()


@pytest.fixture
def client(env, clock, fake):
    application = likelyread.create_app()
    application.state.cloud.http = fake
    return TestClient(application, base_url="https://testserver", follow_redirects=False)


def google_claims(sub, email, **over):
    c = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": sub, "email": email,
         "email_verified": True, "exp": NOW + 3600}
    c.update(over)
    return c


def sign_in(client, fake, sub="g-1", email="ann@example.com", ext=False, **claims_over):
    """Run the whole Google round trip and return the bearer token."""
    r = client.get("/auth/google")
    assert r.status_code == 302
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(r.headers["location"]).query))
    assert q["code_challenge_method"] == "S256" and q["scope"] == "openid email"
    code = f"code-{sub}-{uuid.uuid4().hex[:6]}"
    fake.google[code] = google_claims(sub, email, **claims_over)
    r = client.get(f"/auth/google/callback?code={code}&state={q['state']}")
    assert r.status_code == 303
    loc = r.headers["location"]
    if "#connect=" not in loc:
        return None
    token = loc.split("#connect=")[1].split("&")[0]
    return token


def H(token):
    return {"Authorization": f"Bearer {token}"}


def register(client, token, eid=None, **kw):
    eid = eid or str(uuid.uuid4())
    body = {"id": eid, "subject": kw.get("subject", "Hi"), "recipients": ["x@y.com"], "age": kw.get("age", 0)}
    if "campaign" in kw:
        body["campaign"] = kw["campaign"]
    return client.post("/api/emails", json=body, headers=H(token)), eid


def webhook(client, event, msg_id=None, ts=None, secret=WEBHOOK_SECRET):
    body = json.dumps(event).encode()
    msg_id = msg_id or f"msg_{uuid.uuid4().hex}"
    ts = str(int(ts if ts is not None else NOW))
    sig = base64.b64encode(hmac.new(secret.encode(), f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return client.post(
        "/webhooks/polar",
        content=body,
        headers={"webhook-id": msg_id, "webhook-timestamp": ts, "webhook-signature": f"v1,{sig}",
                 "content-type": "application/json"},
    )


def account_id(client, token):
    return client.app.state.cloud.authenticate(token).account_id


# ---- configuration -------------------------------------------------------------


def test_cloud_refuses_to_start_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("MODE", "cloud")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        likelyread.create_app()


def test_selfhost_has_every_feature_and_reports_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("LIKELYREAD_TOKEN", "t" * 32)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.delenv("MODE", raising=False)
    c = TestClient(likelyread.create_app())
    assert c.get("/api/config").json() == {"mode": "selfhost"}
    me = c.get("/api/me", headers=H("t" * 32)).json()
    assert me["plan"] == "selfhost" and me["features"]["campaigns"] is True
    assert c.get("/auth/google").status_code == 404


def test_public_config(client):
    cfg = client.get("/api/config").json()
    assert cfg["mode"] == "cloud" and cfg["billing"] is True and cfg["lifetime_left"] == 2
    assert cfg["free"]["campaigns"] is False and cfg["pro"]["campaigns"] is True


# ---- Google sign-in ------------------------------------------------------------


def test_sign_in_creates_account_and_token(client, fake):
    token = sign_in(client, fake)
    me = client.get("/api/me", headers=H(token)).json()
    assert me["email"] == "ann@example.com" and me["plan"] == "free"
    # Same Google user again: same account, a second token.
    token2 = sign_in(client, fake)
    assert token2 != token
    assert account_id(client, token) == account_id(client, token2)
    # Only hashes are stored.
    rows = client.app.state.db.query("SELECT hash FROM tokens")
    assert all(r["hash"] != token for r in rows) and len(rows) == 2


def test_extension_sign_in_marks_the_link(client, fake):
    ext = "abcdefghijklmnopabcdefghijklmnop"
    r = client.get(f"/auth/google?ext={ext}")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(r.headers["location"]).query))
    fake.google["c1"] = google_claims("g-9", "e@x.com")
    r = client.get(f"/auth/google/callback?code=c1&state={q['state']}")
    assert r.headers["location"].endswith(f"&ext={ext}")
    # Anything that is not an allowed extension id is dropped, even a well-formed one.
    r = client.get("/auth/google?ext=ponmlkjihgfedcbaponmlkjihgfedcba")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(r.headers["location"]).query))
    fake.google["c2"] = google_claims("g-9", "e@x.com")
    r = client.get(f"/auth/google/callback?code=c2&state={q['state']}")
    assert "&ext" not in r.headers["location"]


def test_callback_rejects_wrong_state(client, fake):
    client.get("/auth/google")
    fake.google["c"] = google_claims("g", "a@b.com")
    r = client.get("/auth/google/callback?code=c&state=forged")
    assert r.headers["location"].endswith("?signin=failed")


def test_callback_rejects_without_cookie(client, fake):
    r = client.get("/auth/google")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(r.headers["location"]).query))
    client.cookies.clear()
    fake.google["c"] = google_claims("g", "a@b.com")
    r = client.get(f"/auth/google/callback?code=c&state={q['state']}")
    assert "signin=failed" in r.headers["location"]


@pytest.mark.parametrize(
    "over",
    [{"aud": "someone-else"}, {"iss": "https://evil.example"}, {"email_verified": False}, {"exp": NOW - 10}],
)
def test_callback_rejects_bad_id_tokens(client, fake, over):
    assert sign_in(client, fake, **over) is None
    assert client.app.state.db.query("SELECT COUNT(*) AS n FROM accounts")[0]["n"] == 0


def test_tampered_oauth_cookie_is_rejected(client):
    assert client.app.state.cloud._unsign("e30.AAAA") is None


def test_bad_token_is_401(client):
    assert client.get("/api/emails", headers=H("nope")).status_code == 401
    assert client.get("/api/emails").status_code == 401


# ---- isolation -----------------------------------------------------------------


def test_accounts_never_see_each_other(client, fake, clock):
    a = sign_in(client, fake, "g-a", "a@x.com")
    b = sign_in(client, fake, "g-b", "b@x.com")
    r, eid = register(client, a, subject="A's secret")
    assert r.status_code == 200
    assert [e["subject"] for e in client.get("/api/emails", headers=H(a)).json()] == ["A's secret"]
    assert client.get("/api/emails", headers=H(b)).json() == []
    assert client.get(f"/api/emails/{eid}", headers=H(b)).status_code == 404
    assert client.patch(f"/api/emails/{eid}", json={"campaign": "x"}, headers=H(b)).status_code == 404
    assert client.delete(f"/api/emails/{eid}", headers=H(b)).status_code == 404
    # B cannot take over A's id either.
    r, _ = register(client, b, eid=eid, subject="hijack")
    assert r.status_code == 409
    assert client.get(f"/api/emails/{eid}", headers=H(a)).json()["subject"] == "A's secret"


def test_other_accounts_suppress_pings_do_not_hide_opens(client, fake, clock):
    a = sign_in(client, fake, "g-a", "a@x.com")
    b = sign_in(client, fake, "g-b", "b@x.com")
    _, eid = register(client, a)
    clock["now"] += 300
    client.post(f"/api/emails/{eid}/suppress", headers=H(b))
    client.get(f"/p/{eid}.gif", headers={"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit"})
    assert client.get(f"/api/emails/{eid}", headers=H(a)).json()["opens"] == 1
    # The owner's own ping still works.
    clock["now"] += 300
    client.post(f"/api/emails/{eid}/suppress", headers=H(a))
    client.get(f"/p/{eid}.gif", headers={"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit"})
    e = client.get(f"/api/emails/{eid}", headers=H(a)).json()
    assert e["opens"] == 1 and e["self"] == 1


def test_delete_account_removes_everything(client, fake):
    a = sign_in(client, fake)
    _, eid = register(client, a)
    client.get(f"/p/{eid}.gif")
    assert client.delete("/api/me", headers=H(a)).json() == {"ok": True}
    db = client.app.state.db
    for table in ("accounts", "tokens", "emails", "hits"):
        assert db.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] == 0, table
    assert client.get("/api/me", headers=H(a)).status_code == 401


def test_export_contains_only_my_data(client, fake):
    a = sign_in(client, fake, "g-a", "a@x.com")
    b = sign_in(client, fake, "g-b", "b@x.com")
    _, eid = register(client, a, subject="mine")
    register(client, b, subject="theirs")
    client.get(f"/p/{eid}.gif", headers={"User-Agent": "UA"})
    r = client.get("/api/me/export", headers=H(a))
    assert r.headers["content-disposition"].startswith("attachment")
    data = r.json()
    assert data["account"]["email"] == "a@x.com"
    assert [e["subject"] for e in data["emails"]] == ["mine"]
    assert data["emails"][0]["hits"][0]["ua"] == "UA"


def test_sign_out_everywhere(client, fake):
    a = sign_in(client, fake)
    a2 = sign_in(client, fake)
    assert client.post("/api/me/signout-all", headers=H(a)).json()["revoked"] == 2
    assert client.get("/api/me", headers=H(a2)).status_code == 401


# ---- plans -----------------------------------------------------------------------


def test_free_plan_tracks_but_locks_insights(client, fake, clock):
    a = sign_in(client, fake)
    r, eid = register(client, a, campaign="Launch")
    assert r.status_code == 200
    assert client.get("/api/emails", headers=H(a)).json()[0]["campaign"] == "Launch"
    assert client.get("/api/campaign-names", headers=H(a)).json() == ["Launch"]
    for path in ("/api/campaigns", "/api/stats", "/api/senders", "/api/emails?campaign=Launch"):
        r = client.get(path, headers=H(a))
        assert r.status_code == 402, path
        assert r.json()["detail"]["error"] == "pro_required"


def test_free_history_is_30_days_and_pro_unlocks_it(client, fake, clock):
    a = sign_in(client, fake)
    a = client.app.state.cloud.create_token(account_id(client, a), "Chrome extension")  # outlives 30 idle days
    _, old = register(client, a, subject="old")
    clock["now"] += 40 * 86400
    register(client, a, subject="new")
    assert [e["subject"] for e in client.get("/api/emails", headers=H(a)).json()] == ["new"]
    assert client.get(f"/api/emails/{old}", headers=H(a)).status_code == 402
    acct = account_id(client, a)
    client.app.state.db.execute("UPDATE accounts SET plan='lifetime' WHERE id=?", (acct,))
    assert len(client.get("/api/emails", headers=H(a)).json()) == 2


def test_free_daily_limit(client, fake, clock, monkeypatch):
    monkeypatch.setitem(likelyread.FEATURES["free"], "daily_limit", 3)
    a = sign_in(client, fake)
    ids = [register(client, a)[1] for _ in range(3)]
    r, _ = register(client, a)
    assert r.status_code == 429 and r.json()["detail"]["error"] == "daily_limit"
    # Retrying an existing registration is never blocked.
    r, _ = register(client, a, eid=ids[0])
    assert r.status_code == 200
    clock["now"] += 86401
    assert register(client, a)[0].status_code == 200


# ---- billing ---------------------------------------------------------------------


def test_checkout_uses_account_as_external_customer(client, fake):
    a = sign_in(client, fake)
    r = client.post("/api/billing/checkout", json={"plan": "pro_yearly"}, headers=H(a))
    assert r.json() == {"url": "https://polar.sh/checkout/abc"}
    method, url, headers, body = fake.calls[-1]
    sent = json.loads(body)
    assert url == "https://api.polar.sh/v1/checkouts/"
    assert headers["Authorization"] == "Bearer polar_oat_test"
    assert sent["products"] == ["prod_year"] and sent["external_customer_id"] == account_id(client, a)
    assert sent["customer_email"] == "ann@example.com"
    assert client.post("/api/billing/checkout", json={"plan": "gold"}, headers=H(a)).status_code == 422


def test_subscription_lifecycle(client, fake, clock):
    a = sign_in(client, fake)
    acct = account_id(client, a)
    end = "2027-01-15T00:00:00Z"
    sub = {"id": "sub_1", "status": "active", "current_period_end": end, "cancel_at_period_end": False,
           "customer": {"external_id": acct}, "product_id": "prod_month"}
    assert webhook(client, {"type": "subscription.active", "data": sub}).json()["result"] == "pro"
    me = client.get("/api/me", headers=H(a)).json()
    assert me["plan"] == "pro" and client.get("/api/campaigns", headers=H(a)).status_code == 200
    # Cancelled: keeps Pro until the period (plus grace) ends.
    webhook(client, {"type": "subscription.canceled", "data": {**sub, "status": "canceled", "cancel_at_period_end": True}})
    me = client.get("/api/me", headers=H(a)).json()
    assert me["plan"] == "pro" and me["cancel_at_period_end"] is True
    clock["now"] = cloud._iso_ts(end) + cloud.GRACE_SECONDS + 1
    assert client.get("/api/me", headers=H(a)).json()["plan"] == "free"
    # Revoked: free at once.
    clock["now"] = NOW
    webhook(client, {"type": "subscription.active", "data": sub})
    webhook(client, {"type": "subscription.revoked", "data": {**sub, "status": "revoked"}})
    assert client.get("/api/me", headers=H(a)).json()["plan"] == "free"


def test_lifetime_purchase_cap_and_refund(client, fake):
    tokens = [sign_in(client, fake, f"g{i}", f"u{i}@x.com") for i in range(3)]
    for t in tokens[:2]:
        order = {"id": "ord", "product_id": "prod_life", "customer": {"external_id": account_id(client, t)}}
        assert webhook(client, {"type": "order.paid", "data": order}).json()["result"] == "lifetime"
    assert client.get("/api/me", headers=H(tokens[0])).json()["plan"] == "pro"
    r = client.post("/api/billing/checkout", json={"plan": "lifetime"}, headers=H(tokens[2]))
    assert r.status_code == 410
    r = client.post("/api/billing/checkout", json={"plan": "pro_monthly"}, headers=H(tokens[0]))
    assert r.status_code == 409  # already lifetime
    order = {"id": "ord", "product_id": "prod_life", "customer": {"external_id": account_id(client, tokens[0])}}
    webhook(client, {"type": "order.refunded", "data": order})
    assert client.get("/api/me", headers=H(tokens[0])).json()["plan"] == "free"


def test_webhook_rejects_bad_signature_and_old_timestamp(client, fake):
    a = sign_in(client, fake)
    ev = {"type": "order.paid", "data": {"product_id": "prod_life", "customer": {"external_id": account_id(client, a)}}}
    assert webhook(client, ev, secret="wrong").status_code == 403
    assert webhook(client, ev, ts=NOW - 3600).status_code == 403
    assert client.get("/api/me", headers=H(a)).json()["plan"] == "free"


def test_webhook_is_idempotent(client, fake):
    a = sign_in(client, fake)
    ev = {"type": "order.paid", "data": {"product_id": "prod_life", "customer": {"external_id": account_id(client, a)}}}
    assert webhook(client, ev, msg_id="m1").json()["result"] == "lifetime"
    assert webhook(client, ev, msg_id="m1").json() == {"ok": True, "duplicate": True}


def test_webhook_for_unknown_account_is_ignored(client):
    ev = {"type": "order.paid", "data": {"product_id": "prod_life", "customer": {"external_id": "nobody"}}}
    assert webhook(client, ev).json()["result"].startswith("ignored")


def test_portal(client, fake):
    a = sign_in(client, fake)
    assert client.post("/api/billing/portal", headers=H(a)).json() == {"url": "https://polar.sh/portal/xyz"}
    assert json.loads(fake.calls[-1][3])["external_customer_id"] == account_id(client, a)


def test_admin_stats_only_for_admins(client, fake):
    a = sign_in(client, fake)
    assert client.get("/api/admin/stats", headers=H(a)).status_code == 404
    boss = sign_in(client, fake, "g-boss", "boss@likelyread.com")
    s = client.get("/api/admin/stats", headers=H(boss)).json()
    assert s["accounts"] == 2 and s["plans"] == {"free": 2}


def test_gmail_proxy_rate_limit_is_per_email(env, clock, fake, monkeypatch):
    monkeypatch.setenv("PIXEL_RATE_PER_MIN", "2")
    application = likelyread.create_app()
    application.state.cloud.http = fake
    c = TestClient(application, base_url="https://testserver", client=("66.249.80.10", 50000))
    ids = [str(uuid.uuid4()) for _ in range(3)]
    for eid in ids:
        c.get(f"/p/{eid}.gif")
    assert application.state.db.query("SELECT COUNT(*) AS n FROM hits")[0]["n"] == 3


# ---- regressions from the security review -----------------------------------


def test_daily_limit_cannot_be_dodged_with_age(client, fake, monkeypatch):
    monkeypatch.setitem(likelyread.FEATURES["free"], "daily_limit", 3)
    a = sign_in(client, fake)
    for _ in range(3):
        assert register(client, a, age=172800)[0].status_code == 200
    assert register(client, a, age=172800)[0].status_code == 429
    assert client.get("/api/me", headers=H(a)).json()["usage"]["today"] == 3


def test_patch_respects_free_history(client, fake, clock):
    a = sign_in(client, fake)
    a = client.app.state.cloud.create_token(account_id(client, a), "Chrome extension")  # outlives 30 idle days
    _, old = register(client, a)
    clock["now"] += 31 * 86400
    assert client.patch(f"/api/emails/{old}", json={"campaign": "x"}, headers=H(a)).status_code == 402


def _sub(acct, sid, status="active", end="2027-06-01T00:00:00Z"):
    return {"id": sid, "status": status, "current_period_end": end, "cancel_at_period_end": False,
            "customer": {"external_id": acct}, "product_id": "prod_year"}


def test_old_subscription_revocation_does_not_downgrade(client, fake):
    a = sign_in(client, fake)
    acct = account_id(client, a)
    webhook(client, {"type": "subscription.active", "data": _sub(acct, "sub_B")})
    r = webhook(client, {"type": "subscription.revoked", "data": _sub(acct, "sub_A", "revoked")})
    assert r.json()["result"] == "ignored: not the current subscription"
    assert client.get("/api/me", headers=H(a)).json()["plan"] == "pro"


def test_no_second_subscription_while_pro(client, fake):
    a = sign_in(client, fake)
    webhook(client, {"type": "subscription.active", "data": _sub(account_id(client, a), "sub_1")})
    r = client.post("/api/billing/checkout", json={"plan": "pro_monthly"}, headers=H(a))
    assert r.status_code == 409


def test_lifetime_over_cap_still_granted_but_flagged(client, fake):
    tokens = [sign_in(client, fake, f"g{i}", f"u{i}@x.com") for i in range(3)]
    results = []
    for t in tokens:
        order = {"id": "o", "product_id": "prod_life", "customer": {"external_id": account_id(client, t)}}
        results.append(webhook(client, {"type": "order.paid", "data": order}).json()["result"])
    assert results == ["lifetime", "lifetime", "lifetime (over cap)"]


def test_delete_cancels_subscription_and_tombstones(client, fake):
    a = sign_in(client, fake)
    acct = account_id(client, a)
    webhook(client, {"type": "subscription.active", "data": _sub(acct, "sub_9")})
    fake.cancel_status = 500
    assert client.delete("/api/me", headers=H(a)).status_code == 502
    assert client.get("/api/me", headers=H(a)).status_code == 200  # nothing deleted
    fake.cancel_status = 200
    assert client.delete("/api/me", headers=H(a)).status_code == 200
    assert any(c[0] == "DELETE" and c[1].endswith("/v1/subscriptions/sub_9") for c in fake.calls)
    r = webhook(client, {"type": "subscription.revoked", "data": _sub(acct, "sub_9", "revoked")})
    assert r.json()["result"] == "ignored: deleted account"


def test_failed_webhook_is_applied_on_retry(client, fake, monkeypatch):
    a = sign_in(client, fake)
    ev = {"type": "order.paid", "data": {"product_id": "prod_life", "customer": {"external_id": account_id(client, a)}}}
    real = client.app.state.cloud.handle_event
    calls = {"n": 0}

    def flaky(event):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db hiccup")
        return real(event)

    monkeypatch.setattr(client.app.state.cloud, "handle_event", flaky)
    with pytest.raises(RuntimeError):
        webhook(client, ev, msg_id="m-retry")
    assert webhook(client, ev, msg_id="m-retry").json()["result"] == "lifetime"


def test_suppress_on_foreign_id_writes_nothing(client, fake):
    a = sign_in(client, fake, "g-a", "a@x.com")
    b = sign_in(client, fake, "g-b", "b@x.com")
    _, eid = register(client, a)
    assert client.post(f"/api/emails/{eid}/suppress", headers=H(b)).status_code == 200
    assert client.post(f"/api/emails/{uuid.uuid4()}/suppress", headers=H(b)).status_code == 200
    assert client.app.state.db.query("SELECT COUNT(*) AS n FROM suppressions")[0]["n"] == 0


def test_idle_tokens_expire(client, fake, clock):
    a = sign_in(client, fake)
    clock["now"] += cloud.TOKEN_IDLE_SECONDS + 10
    assert client.get("/api/me", headers=H(a)).status_code == 401


def test_webhook_accepts_spec_derived_key(client, fake):
    a = sign_in(client, fake)
    raw = b"0123456789abcdef0123456789abcdef"
    client.app.state.cloud.cfg.polar_webhook_secret = "whsec_" + base64.b64encode(raw).decode()
    ev = {"type": "order.paid", "data": {"product_id": "prod_life", "customer": {"external_id": account_id(client, a)}}}
    body = json.dumps(ev).encode()
    ts = str(int(NOW))
    sig = base64.b64encode(hmac.new(raw, f"m-spec.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    r = client.post("/webhooks/polar", content=body, headers={"webhook-id": "m-spec", "webhook-timestamp": ts,
                    "webhook-signature": f"v1,{sig}", "content-type": "application/json"})
    assert r.json()["result"] == "lifetime"


def test_daily_purge_forgets_old_webhooks(client, clock):
    db = client.app.state.db
    db.execute("INSERT INTO webhook_events(id, received_at) VALUES ('old', ?)", (NOW - 40 * 86400,))
    client.app.state.cloud.purge(NOW)
    assert db.query("SELECT COUNT(*) AS n FROM webhook_events")[0]["n"] == 0


def test_dashboard_tokens_lapse_sooner(client, fake, clock):
    dash = sign_in(client, fake)  # signed in from the dashboard
    ext_token = client.app.state.cloud.create_token(account_id(client, dash), "Chrome extension")
    clock["now"] += cloud.DASHBOARD_TOKEN_IDLE_SECONDS + 10
    assert client.get("/api/me", headers=H(dash)).status_code == 401
    assert client.get("/api/me", headers=H(ext_token)).status_code == 200


def test_delete_refuses_when_polar_token_cannot_cancel(client, fake):
    a = sign_in(client, fake)
    webhook(client, {"type": "subscription.active", "data": _sub(account_id(client, a), "sub_x")})
    fake.cancel_status = 403
    assert client.delete("/api/me", headers=H(a)).status_code == 502
    assert client.get("/api/me", headers=H(a)).status_code == 200


def test_proxy_hits_have_their_own_limiter_table(env, clock, fake, monkeypatch):
    monkeypatch.setattr(likelyread.RateLimiter, "MAX_KEYS", 3)
    application = likelyread.create_app()
    application.state.cloud.http = fake
    proxy = TestClient(application, base_url="https://testserver", client=("66.249.80.10", 1))
    for _ in range(5):  # fills the proxy table
        proxy.get(f"/p/{uuid.uuid4()}.gif")
    direct = TestClient(application, base_url="https://testserver", client=("203.0.113.9", 1))
    eid = str(uuid.uuid4())
    direct.get(f"/p/{eid}.gif")
    n = application.state.db.query("SELECT COUNT(*) AS n FROM hits WHERE email_id = ?", (eid,))[0]["n"]
    assert n == 1, "a direct open is still logged while the proxy table is full"
