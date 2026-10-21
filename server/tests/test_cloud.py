"""Cloud mode: accounts, isolation, plans, Google sign-in and Stripe billing.
Google and Stripe are replaced with fakes; nothing leaves the machine."""

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
import urllib.parse
import uuid

import pytest
from fastapi.testclient import TestClient

import app as likelyread
import cloud

SECRET = "s" * 40
WEBHOOK_SECRET = "whsec_test_secret"
CLIENT_ID = "cid.apps.googleusercontent.com"
NOW = 1_800_000_000.0
PERIOD_END = int(NOW) + 7 * 86400


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
        "STRIPE_SECRET_KEY": "rk_test_likelyread",
        "STRIPE_WEBHOOK_SECRET": WEBHOOK_SECRET,
        "STRIPE_PRICE_PRO_MONTHLY": "price_month",
        "STRIPE_PRICE_PRO_YEARLY": "price_year",
        "STRIPE_PRICE_LIFETIME": "price_life",
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
        self.customers = 0
        self.cancel_status = 200
        self.portal_status = 200

    def sent(self, path):
        """The form body of the latest call to a Stripe path, as a dict."""
        for _, url, _, body in reversed(self.calls):
            if url.endswith(path):
                return dict(urllib.parse.parse_qsl(body.decode())) if body else {}
        return None

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if "oauth2" in url:
            form = dict(urllib.parse.parse_qsl(body.decode()))
            claims = self.google.get(form["code"])
            if not claims:
                return 400, {"error": "invalid_grant"}
            enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
            return 200, {"id_token": f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig", "access_token": "x"}
        if url.endswith("/v1/customers") and method == "POST":
            self.customers += 1
            return 200, {"id": f"cus_{self.customers}", "object": "customer"}
        if url.endswith("/v1/checkout/sessions"):
            return 200, {"id": "cs_test_1", "url": "https://checkout.stripe.com/c/pay/cs_test_1"}
        if "/v1/subscriptions/" in url and method == "DELETE":
            return self.cancel_status, {}
        if url.endswith("/v1/billing_portal/sessions"):
            if self.portal_status != 200:
                return self.portal_status, {"error": {"message": "No configuration provided"}}
            return 200, {"url": "https://billing.stripe.com/p/session/xyz"}
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


def webhook(client, kind, obj, event_id=None, ts=None, secret=WEBHOOK_SECRET):
    """Deliver a Stripe event the way Stripe signs it."""
    event = {"id": event_id or f"evt_{uuid.uuid4().hex}", "object": "event", "type": kind, "data": {"object": obj}}
    body = json.dumps(event).encode()
    ts = str(int(ts if ts is not None else NOW))
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/stripe",
        content=body,
        headers={"stripe-signature": f"t={ts},v1={sig}", "content-type": "application/json"},
    )


def account_id(client, token):
    return client.app.state.cloud.authenticate(token).account_id


def _sub(acct, sid, status="active", end=PERIOD_END, **over):
    """A Stripe subscription as recent API versions send it: the period end is on the item."""
    s = {"id": sid, "object": "subscription", "status": status, "customer": "cus_test", "cancel_at_period_end": False,
         "metadata": {"account_id": acct, "plan": "pro_yearly"},
         "items": {"data": [{"price": {"id": "price_year"}, "current_period_end": end}]}}
    s.update(over)
    return s


def _lifetime_session(acct, pi="pi_life", paid=True):
    return {"id": "cs_life", "object": "checkout.session", "mode": "payment", "payment_intent": pi,
            "payment_status": "paid" if paid else "unpaid", "client_reference_id": acct,
            "metadata": {"account_id": acct, "plan": "lifetime"}}


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
    for path in ("/api/campaigns", "/api/stats", "/api/senders", "/api/senders/health", "/api/emails?campaign=Launch"):
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


def test_checkout_creates_one_customer_and_a_taxed_subscription(client, fake):
    a = sign_in(client, fake)
    acct = account_id(client, a)
    r = client.post("/api/billing/checkout", json={"plan": "pro_yearly"}, headers=H(a))
    assert r.json() == {"url": "https://checkout.stripe.com/c/pay/cs_test_1"}
    assert fake.sent("/v1/customers") == {"email": "ann@example.com", "metadata[account_id]": acct}
    _, url, headers, _ = next(c for c in fake.calls if c[1].endswith("/v1/customers"))
    assert url == "https://api.stripe.com/v1/customers"
    assert headers["Authorization"] == "Bearer rk_test_likelyread"
    assert headers["Idempotency-Key"] == f"likelyread-customer-{acct}-{int(NOW // 600)}"
    sent = fake.sent("/v1/checkout/sessions")
    assert sent["mode"] == "subscription" and sent["customer"] == "cus_1" and sent["client_reference_id"] == acct
    assert sent["line_items[0][price]"] == "price_year" and sent["line_items[0][quantity]"] == "1"
    assert sent["metadata[plan]"] == "pro_yearly" and sent["subscription_data[metadata][account_id]"] == acct
    assert sent["automatic_tax[enabled]"] == "true" and sent["tax_id_collection[enabled]"] == "true"
    assert sent["consent_collection[terms_of_service]"] == "required"
    assert sent["customer_update[address]"] == "auto" and sent["customer_update[name]"] == "auto"
    # The next checkout reuses the customer.
    client.post("/api/billing/checkout", json={"plan": "pro_monthly"}, headers=H(a))
    assert fake.customers == 1 and fake.sent("/v1/checkout/sessions")["customer"] == "cus_1"
    assert client.post("/api/billing/checkout", json={"plan": "gold"}, headers=H(a)).status_code == 422


def test_lifetime_checkout_is_a_one_time_payment_with_an_invoice(client, fake):
    a = sign_in(client, fake)
    client.post("/api/billing/checkout", json={"plan": "lifetime"}, headers=H(a))
    sent = fake.sent("/v1/checkout/sessions")
    assert sent["mode"] == "payment" and sent["line_items[0][price]"] == "price_life"
    assert sent["invoice_creation[enabled]"] == "true" and "subscription_data[metadata][account_id]" not in sent


def test_stripe_tax_can_be_switched_off(client, fake):
    assert cloud.CloudSettings({**os.environ, "STRIPE_TAX": "false"}).stripe_tax is False
    client.app.state.cloud.cfg.stripe_tax = False
    a = sign_in(client, fake)
    client.post("/api/billing/checkout", json={"plan": "pro_monthly"}, headers=H(a))
    assert fake.sent("/v1/checkout/sessions")["automatic_tax[enabled]"] == "false"


def test_subscription_lifecycle(client, fake, clock):
    a = sign_in(client, fake)
    acct = account_id(client, a)
    # The subscription's own events decide the plan, not the checkout.
    done = {"id": "cs_1", "object": "checkout.session", "mode": "subscription", "payment_status": "paid",
            "metadata": {"account_id": acct, "plan": "pro_yearly"}}
    assert webhook(client, "checkout.session.completed", done).json()["result"] == "ignored: subscription checkout"
    assert webhook(client, "customer.subscription.created", _sub(acct, "sub_1")).json()["result"] == "pro"
    me = client.get("/api/me", headers=H(a)).json()
    assert me["plan"] == "pro" and me["plan_until"] == PERIOD_END
    assert client.get("/api/campaigns", headers=H(a)).status_code == 200
    # Cancelled in the portal: keeps Pro until the period (plus grace) ends.
    webhook(client, "customer.subscription.updated", _sub(acct, "sub_1", cancel_at_period_end=True))
    me = client.get("/api/me", headers=H(a)).json()
    assert me["plan"] == "pro" and me["cancel_at_period_end"] is True
    clock["now"] = PERIOD_END + cloud.GRACE_SECONDS + 1
    assert client.get("/api/me", headers=H(a)).json()["plan"] == "free"
    # Deleted, so ended: free at once.
    clock["now"] = NOW
    webhook(client, "customer.subscription.updated", _sub(acct, "sub_1"))
    assert client.get("/api/me", headers=H(a)).json()["plan"] == "pro"
    webhook(client, "customer.subscription.deleted", _sub(acct, "sub_1", status="canceled"))
    assert client.get("/api/me", headers=H(a)).json()["plan"] == "free"


def test_period_end_is_read_from_old_and_new_api_versions():
    assert cloud._period_end({"current_period_end": 1_700_000_000}) == 1_700_000_000
    items = {"data": [{"current_period_end": 1_700_000_000}, {"current_period_end": 1_700_000_500}]}
    assert cloud._period_end({"items": items}) == 1_700_000_500
    assert cloud._period_end({"status": "active"}) is None


def test_lifetime_purchase_cap_and_refund(client, fake):
    tokens = [sign_in(client, fake, f"g{i}", f"u{i}@x.com") for i in range(3)]
    for i, t in enumerate(tokens[:2]):
        assert client.post("/api/billing/checkout", json={"plan": "lifetime"}, headers=H(t)).status_code == 200
        r = webhook(client, "checkout.session.completed", _lifetime_session(account_id(client, t), pi=f"pi_{i}"))
        assert r.json()["result"] == "lifetime"
    assert client.get("/api/me", headers=H(tokens[0])).json()["plan"] == "pro"
    r = client.post("/api/billing/checkout", json={"plan": "lifetime"}, headers=H(tokens[2]))
    assert r.status_code == 410
    r = client.post("/api/billing/checkout", json={"plan": "pro_monthly"}, headers=H(tokens[0]))
    assert r.status_code == 409  # already lifetime
    # A refund names the Stripe customer, not the account. Only a full refund of
    # the lifetime payment itself ends lifetime access.
    charge = {"id": "ch_0", "object": "charge", "customer": "cus_1", "payment_intent": "pi_0", "refunded": False}
    assert webhook(client, "charge.refunded", charge).json()["result"] == "ignored"
    other = {**charge, "refunded": True, "payment_intent": "pi_other"}
    assert webhook(client, "charge.refunded", other).json()["result"] == "ignored"
    full = {**charge, "refunded": True}
    assert webhook(client, "charge.refunded", full).json()["result"] == "lifetime refunded"
    assert client.get("/api/me", headers=H(tokens[0])).json()["plan"] == "free"


def test_lifetime_waits_for_a_delayed_payment(client, fake):
    a = sign_in(client, fake)
    acct = account_id(client, a)
    r = webhook(client, "checkout.session.completed", _lifetime_session(acct, paid=False))
    assert r.json()["result"] == "ignored: payment pending"
    assert client.get("/api/me", headers=H(a)).json()["plan"] == "free"
    r = webhook(client, "checkout.session.async_payment_succeeded", _lifetime_session(acct))
    assert r.json()["result"] == "lifetime"


def test_webhook_rejects_bad_signature_and_old_timestamp(client, fake):
    a = sign_in(client, fake)
    obj = _lifetime_session(account_id(client, a))
    assert webhook(client, "checkout.session.completed", obj, secret="whsec_wrong").status_code == 403
    assert webhook(client, "checkout.session.completed", obj, ts=NOW - 3600).status_code == 403
    assert client.post("/webhooks/stripe", content=b"{}", headers={"content-type": "application/json"}).status_code == 403
    assert client.get("/api/me", headers=H(a)).json()["plan"] == "free"


def test_webhook_accepts_either_secret_while_one_is_rolled(client, fake):
    """While a signing secret is rolled, Stripe sends one v1 signature per secret."""
    a = sign_in(client, fake)
    event = {"id": "evt_roll", "type": "checkout.session.completed",
             "data": {"object": _lifetime_session(account_id(client, a))}}
    body = json.dumps(event).encode()
    ts = str(int(NOW))
    sig = lambda s: hmac.new(s.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()  # noqa: E731
    header = f"t={ts},v1={sig('whsec_old_secret')},v1={sig(WEBHOOK_SECRET)}"
    r = client.post("/webhooks/stripe", content=body, headers={"stripe-signature": header, "content-type": "application/json"})
    assert r.json()["result"] == "lifetime"


def test_webhook_is_idempotent(client, fake):
    a = sign_in(client, fake)
    obj = _lifetime_session(account_id(client, a))
    assert webhook(client, "checkout.session.completed", obj, event_id="evt_1").json()["result"] == "lifetime"
    assert webhook(client, "checkout.session.completed", obj, event_id="evt_1").json() == {"ok": True, "duplicate": True}


def test_webhook_for_unknown_account_is_ignored(client):
    r = webhook(client, "checkout.session.completed", _lifetime_session("nobody"))
    assert r.json()["result"].startswith("ignored")


def test_events_without_metadata_map_through_the_customer(client, fake):
    a = sign_in(client, fake)
    client.post("/api/billing/checkout", json={"plan": "pro_monthly"}, headers=H(a))  # creates cus_1
    sub = _sub(account_id(client, a), "sub_1", metadata={}, customer="cus_1")
    assert webhook(client, "customer.subscription.created", sub).json()["result"] == "pro"


def test_portal(client, fake):
    a = sign_in(client, fake)
    assert client.post("/api/billing/portal", headers=H(a)).status_code == 404  # no Stripe customer yet
    client.post("/api/billing/checkout", json={"plan": "pro_monthly"}, headers=H(a))
    assert client.post("/api/billing/portal", headers=H(a)).json() == {"url": "https://billing.stripe.com/p/session/xyz"}
    assert fake.sent("/v1/billing_portal/sessions") == {"customer": "cus_1", "return_url": "https://app.likelyread.com/#/account"}
    fake.portal_status = 400  # the customer portal is not saved in Stripe's settings
    assert client.post("/api/billing/portal", headers=H(a)).status_code == 502


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


def test_old_subscription_ending_does_not_downgrade(client, fake):
    a = sign_in(client, fake)
    acct = account_id(client, a)
    webhook(client, "customer.subscription.created", _sub(acct, "sub_B"))
    r = webhook(client, "customer.subscription.deleted", _sub(acct, "sub_A", status="canceled"))
    assert r.json()["result"] == "ignored: not the current subscription"
    assert client.get("/api/me", headers=H(a)).json()["plan"] == "pro"


def test_no_second_subscription_while_pro(client, fake):
    a = sign_in(client, fake)
    webhook(client, "customer.subscription.created", _sub(account_id(client, a), "sub_1"))
    r = client.post("/api/billing/checkout", json={"plan": "pro_monthly"}, headers=H(a))
    assert r.status_code == 409


def test_lifetime_over_cap_still_granted_but_flagged(client, fake):
    tokens = [sign_in(client, fake, f"g{i}", f"u{i}@x.com") for i in range(3)]
    results = []
    for t in tokens:
        r = webhook(client, "checkout.session.completed", _lifetime_session(account_id(client, t)))
        results.append(r.json()["result"])
    assert results == ["lifetime", "lifetime", "lifetime (over cap)"]


def test_delete_cancels_subscription_and_tombstones(client, fake):
    a = sign_in(client, fake)
    acct = account_id(client, a)
    webhook(client, "customer.subscription.created", _sub(acct, "sub_9"))
    fake.cancel_status = 500
    assert client.delete("/api/me", headers=H(a)).status_code == 502
    assert client.get("/api/me", headers=H(a)).status_code == 200  # nothing deleted
    fake.cancel_status = 200
    assert client.delete("/api/me", headers=H(a)).status_code == 200
    assert any(c[0] == "DELETE" and c[1].endswith("/v1/subscriptions/sub_9") for c in fake.calls)
    r = webhook(client, "customer.subscription.deleted", _sub(acct, "sub_9", status="canceled"))
    assert r.json()["result"] == "ignored: deleted account"


def test_failed_webhook_is_applied_on_retry(client, fake, monkeypatch):
    a = sign_in(client, fake)
    obj = _lifetime_session(account_id(client, a))
    real = client.app.state.cloud.handle_event
    calls = {"n": 0}

    def flaky(event):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db hiccup")
        return real(event)

    monkeypatch.setattr(client.app.state.cloud, "handle_event", flaky)
    with pytest.raises(RuntimeError):
        webhook(client, "checkout.session.completed", obj, event_id="evt_retry")
    assert webhook(client, "checkout.session.completed", obj, event_id="evt_retry").json()["result"] == "lifetime"


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


def test_delete_refuses_when_stripe_key_cannot_cancel(client, fake):
    a = sign_in(client, fake)
    webhook(client, "customer.subscription.created", _sub(account_id(client, a), "sub_x"))
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


def test_database_from_before_stripe_gets_the_new_columns(env, tmp_path):
    path = tmp_path / "before-stripe.db"
    env.setenv("DB_PATH", str(path))
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE accounts (id TEXT PRIMARY KEY, email TEXT NOT NULL, google_sub TEXT UNIQUE, "
        "created_at REAL NOT NULL, plan TEXT NOT NULL DEFAULT 'free', plan_until REAL, subscription_id TEXT, "
        "subscription_status TEXT, cancel_at_period_end INTEGER NOT NULL DEFAULT 0)"
    )
    con.commit()
    con.close()
    likelyread.create_app()
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(accounts)")}
    assert {"customer_id", "lifetime_payment"} <= cols
