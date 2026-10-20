"""Sender health: DNS checks, ramp, open-rate trend and the endpoints. DNS is faked."""

import uuid

import pytest
from fastapi.testclient import TestClient

import app as likelyread
import health

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOW = 1_800_000_000.0
DAY = 86400


def fake_dns(records: dict, fail: set | None = None):
    fail = fail or set()

    def lookup(name):
        if name in fail:
            return None
        return records.get(name, [])

    return lookup


GOOD = {
    "acme.io": ["v=spf1 include:_spf.google.com ~all", "google-site-verification=x"],
    "_dmarc.acme.io": ["v=DMARC1; p=quarantine; rua=mailto:d@acme.io"],
    "google._domainkey.acme.io": ["v=DKIM1; k=rsa; p=MIIBIjANBg"],
}


def by_label(result):
    return {c["label"]: c for c in result["checks"]}


# ---- DNS checks ------------------------------------------------------------------


def test_well_configured_domain():
    r = by_label(health.check_domain("acme.io", fake_dns(GOOD)))
    assert (r["SPF"]["status"], r["DMARC"]["status"], r["DKIM"]["status"]) == ("ok", "ok", "ok")
    assert "google" in r["DKIM"]["detail"]


def test_missing_everything():
    r = by_label(health.check_domain("new.io", fake_dns({})))
    assert r["SPF"]["status"] == "fail" and r["SPF"]["fix"]
    assert r["DMARC"]["status"] == "fail" and "_dmarc.new.io" in r["DMARC"]["fix"]
    assert r["DKIM"]["status"] == "unknown"


@pytest.mark.parametrize(
    "spf,expected",
    [
        (["v=spf1 a ~all", "v=spf1 mx ~all"], "Two or more"),
        (["v=spf1 +all"], "+all"),
    ],
)
def test_bad_spf(spf, expected):
    r = by_label(health.check_domain("x.io", fake_dns({"x.io": spf})))
    assert r["SPF"]["status"] == "fail" and expected in r["SPF"]["detail"]


def test_dmarc_none_is_a_warning():
    r = by_label(health.check_domain("x.io", fake_dns({"_dmarc.x.io": ["v=DMARC1; p=none"]})))
    assert r["DMARC"]["status"] == "warn" and "p=quarantine" in r["DMARC"]["fix"]


def test_provider_domains_are_skipped():
    calls = []
    r = health.check_domain("gmail.com", lambda n: calls.append(n) or [])
    assert r["managed"] and calls == [] and r["checks"][0]["status"] == "ok"


def test_dns_failure_is_unknown_not_fail_and_not_cached():
    lookup = fake_dns({}, fail={"x.io", "_dmarc.x.io"} | {f"{s}._domainkey.x.io" for s in health.DKIM_SELECTORS})
    cache = health.DnsCache(lookup)
    r = by_label(cache.get("x.io"))
    assert {c["status"] for c in r.values()} == {"unknown"}
    assert cache._data == {}


# ---- ramp and trend --------------------------------------------------------------


def test_ramp_schedule():
    assert health.ramp(NOW, 0, False, NOW)["limit"] == 20
    assert health.ramp(NOW - 2 * DAY, 0, False, NOW)["limit"] == 30  # day 3
    r = health.ramp(NOW - 30 * DAY, 12, False, NOW)
    assert (r["limit"], r["phase"], r["remaining"]) == (50, "steady", 38)
    assert health.ramp(NOW - DAY, 5, True, NOW)["limit"] is None


def _emails(n, start, opened):
    return [{"sent_at": start + i * 600, "opens": 1 if i < opened else 0} for i in range(n)]


def test_trend_warns_on_a_drop():
    emails = _emails(20, NOW - 30 * DAY, 10) + _emails(10, NOW - 5 * DAY, 2)
    t = health.trend(emails, NOW)
    assert "fell from 50% to 20%" in t["warning"]


def test_trend_warns_when_nothing_is_opened():
    assert "None of the last 12" in health.trend(_emails(12, NOW - 5 * DAY, 0), NOW)["warning"]


def test_trend_quiet_with_little_data_or_steady_rate():
    assert "warning" not in health.trend(_emails(5, NOW - 5 * DAY, 0), NOW)
    steady = _emails(20, NOW - 30 * DAY, 8) + _emails(10, NOW - 5 * DAY, 4)
    assert "warning" not in health.trend(steady, NOW)


def test_last_24_hours_are_not_judged_yet():
    assert "warning" not in health.trend(_emails(12, NOW - 3600 * 5, 0), NOW)


# ---- endpoints -------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LIKELYREAD_TOKEN", TOKEN)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "h.db"))
    monkeypatch.delenv("MODE", raising=False)
    state = {"now": NOW}
    monkeypatch.setattr(likelyread.time, "time", lambda: state["now"])
    application = likelyread.create_app()
    application.state.dns_cache.lookup = fake_dns(GOOD)
    c = TestClient(application)
    c.clock = state
    return c


def send(c, sender, age=0, opened=False):
    eid = str(uuid.uuid4())
    r = c.post("/api/emails", json={"id": eid, "subject": "s", "recipients": ["r@x.com"], "age": age, "sender": sender}, headers=AUTH)
    assert r.status_code == 200
    if opened:
        c.clock["now"] += 3600
        c.get(f"/p/{eid}.gif", headers={"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit"})
        c.clock["now"] -= 3600
    return eid


def test_health_report_per_sender(client):
    for _ in range(3):
        send(client, "ann@acme.io")
    send(client, "me@gmail.com")
    rows = {r["sender"]: r for r in client.get("/api/senders/health", headers=AUTH).json()}
    acme = rows["ann@acme.io"]
    assert acme["status"] == "ok" and acme["ramp"]["sent_24h"] == 3 and acme["ramp"]["remaining"] == 17
    assert {c["label"] for c in acme["dns"]["checks"]} == {"SPF", "DMARC", "DKIM"}
    assert rows["me@gmail.com"]["dns"]["managed"] is True


def test_over_the_ramp_is_flagged_and_established_turns_it_off(client):
    for _ in range(21):
        send(client, "ann@acme.io")
    r = client.get("/api/senders/health?sender=ann@acme.io", headers=AUTH).json()[0]
    assert r["status"] == "warn" and "above the 20 a day" in r["issues"][0]
    assert client.put("/api/senders/health/ann@acme.io", json={"established": True}, headers=AUTH).status_code == 200
    r = client.get("/api/senders/health?sender=ann@acme.io", headers=AUTH).json()[0]
    assert r["status"] == "ok" and r["ramp"]["phase"] == "established"


def test_ramp_counts_from_the_first_tracked_email_ever(client):
    send(client, "ann@acme.io")
    client.clock["now"] += 60 * DAY
    send(client, "ann@acme.io")
    r = client.get("/api/senders/health", headers=AUTH).json()[0]
    assert r["ramp"]["day"] == 61 and r["ramp"]["limit"] == 50


def test_fast_mode_skips_dns(client):
    send(client, "ann@acme.io")
    client.app.state.dns_cache.lookup = lambda n: pytest.fail("DNS must not be queried")
    client.app.state.dns_cache._data.clear()
    r = client.get("/api/senders/health?dns=false", headers=AUTH).json()[0]
    assert r["dns"] is None and r["ramp"]["remaining"] == 19


def test_bad_dns_makes_status_bad(client):
    client.app.state.dns_cache.lookup = fake_dns({})
    send(client, "ann@new.io")
    r = client.get("/api/senders/health", headers=AUTH).json()[0]
    assert r["status"] == "bad" and any(i.startswith("SPF") for i in r["issues"])


def test_health_rejects_non_address(client):
    assert client.put("/api/senders/health/nobody", json={"established": True}, headers=AUTH).status_code == 422
