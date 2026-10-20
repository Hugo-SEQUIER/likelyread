"""Sender health: is each address you send from set up and used in a way that
keeps its email out of spam?

Three parts, for every sending address:
- DNS: SPF, DKIM and DMARC on the address's domain (skipped for addresses at
  big providers such as gmail.com, where the provider manages them).
- Ramp: a daily volume guide for addresses that are new to sending, and the
  count already sent in the last 24 hours.
- Trend: the open rate of the last week against the four weeks before. A sharp
  drop is the earliest sign that mail has started landing in spam.

Nothing here sends email or fakes engagement; it only reads DNS and the
account's own tracking data.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

# Addresses at these domains: the provider runs SPF, DKIM and DMARC.
PROVIDER_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
        "yahoo.com", "ymail.com", "icloud.com", "me.com", "mac.com", "aol.com",
        "proton.me", "protonmail.com", "pm.me", "gmx.com", "gmx.de", "gmx.net", "web.de",
        "yandex.com", "yandex.ru", "mail.com", "zoho.com", "fastmail.com", "orange.fr", "free.fr",
        "naver.com", "daum.net", "hanmail.net",
    }
)
# DKIM keys live at <selector>._domainkey.<domain>; the selector is chosen by the
# provider, so try the common ones. Google Workspace: google. Microsoft 365:
# selector1/selector2. Mailchimp: k1. Others: provider defaults.
DKIM_SELECTORS = (
    "google", "selector1", "selector2", "default", "k1", "k2", "s1", "s2",
    "dkim", "mail", "smtp", "zoho", "protonmail", "fm1", "resend", "mxvault",
)

# Ramp for an address new to sending: start at 20 a day, add 5 a day, level at 50.
# Deliverability guidance converges on roughly 10 to 20 a day at first, a few
# more each day, and about 50 cold emails a day per mailbox at most.
RAMP_START = 20
RAMP_STEP = 5
RAMP_MAX = 50

# Trend: last 7 days (minus the last 24 h, whose emails have not had time to be
# opened) against the 28 days before.
TREND_MIN_EMAILS = 10
CACHE_SECONDS = 3600

DnsLookup = Callable[[str], "list[str] | None"]


def default_lookup(name: str) -> list[str] | None:
    """TXT strings at name; [] when the name has no TXT; None when DNS failed."""
    import dns.exception
    import dns.resolver

    resolver = dns.resolver.Resolver()
    resolver.lifetime = 4
    try:
        answer = resolver.resolve(name, "TXT")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except (dns.exception.Timeout, dns.resolver.NoNameservers, dns.resolver.NoResolverConfiguration):
        return None
    out = []
    for rdata in answer:
        try:
            out.append(b"".join(rdata.strings).decode("utf-8", "replace"))
        except AttributeError:
            continue
    return out


def _check(status: str, label: str, detail: str, fix: str | None = None) -> dict:
    return {"status": status, "label": label, "detail": detail, "fix": fix}


def check_domain(domain: str, lookup: DnsLookup) -> dict:
    """DNS checks for one sending domain."""
    domain = domain.strip().lower().rstrip(".")
    if domain in PROVIDER_DOMAINS:
        return {
            "domain": domain,
            "managed": True,
            "checks": [
                _check(
                    "ok",
                    "Authentication",
                    f"{domain} handles SPF, DKIM and DMARC for its users. Nothing to set up.",
                )
            ],
        }

    checks = []
    spf = lookup(domain)
    if spf is None:
        checks.append(_check("unknown", "SPF", "Could not check: the DNS lookup failed. Try again later."))
    else:
        records = [r for r in spf if r.lower().startswith("v=spf1")]
        if not records:
            checks.append(
                _check(
                    "fail",
                    "SPF",
                    "No SPF record. Gmail and Yahoo require SPF or DKIM on every sender, and both for bulk senders.",
                    f'Add a TXT record on {domain} listing your mail provider, for example "v=spf1 include:_spf.google.com ~all" for Google Workspace.',
                )
            )
        elif len(records) > 1:
            checks.append(
                _check(
                    "fail",
                    "SPF",
                    "Two or more SPF records. Receivers treat that as an error and ignore all of them.",
                    "Merge them into one TXT record that starts with v=spf1.",
                )
            )
        elif "+all" in records[0].lower().split():
            checks.append(
                _check(
                    "fail",
                    "SPF",
                    "The SPF record ends with +all, which lets anyone send as your domain.",
                    "End it with ~all or -all.",
                )
            )
        else:
            checks.append(_check("ok", "SPF", records[0]))

    dmarc = lookup(f"_dmarc.{domain}")
    if dmarc is None:
        checks.append(_check("unknown", "DMARC", "Could not check: the DNS lookup failed. Try again later."))
    else:
        records = [r for r in dmarc if r.lower().replace(" ", "").startswith("v=dmarc1")]
        if not records:
            checks.append(
                _check(
                    "fail",
                    "DMARC",
                    "No DMARC record. Gmail and Yahoo require one from bulk senders, and it stops others spoofing your domain.",
                    f'Add a TXT record at _dmarc.{domain}: "v=DMARC1; p=none; rua=mailto:dmarc@{domain}". Move to p=quarantine once the reports look clean.',
                )
            )
        else:
            tags = {
                k.strip().lower(): v.strip().lower()
                for k, _, v in (part.partition("=") for part in records[0].split(";"))
                if k.strip()
            }
            policy = tags.get("p", "")
            if policy in ("quarantine", "reject"):
                checks.append(_check("ok", "DMARC", records[0]))
            else:
                checks.append(
                    _check(
                        "warn",
                        "DMARC",
                        f"Policy p={policy or 'missing'}: reports only, nothing is enforced.",
                        "Once the reports show only your own servers, change it to p=quarantine.",
                    )
                )

    found = None
    failed = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda s: (s, lookup(f"{s}._domainkey.{domain}")), DKIM_SELECTORS))
    for selector, txt in results:
        if txt is None:
            failed += 1
        elif any("p=" in r.replace(" ", "") for r in txt):
            found = selector
            break
    if found:
        checks.append(_check("ok", "DKIM", f"Key found under the selector {found}."))
    elif failed == len(DKIM_SELECTORS):
        checks.append(_check("unknown", "DKIM", "Could not check: the DNS lookups failed. Try again later."))
    else:
        checks.append(
            _check(
                "unknown",
                "DKIM",
                "No key under the usual selectors. Your provider may use another one.",
                "Send yourself an email and look for dkim=pass in the original message (Gmail: Show original). If it is missing, turn on DKIM signing at your mail provider.",
            )
        )
    return {"domain": domain, "managed": False, "checks": checks}


class DnsCache:
    """Per-domain results, kept for an hour."""

    def __init__(self, lookup: DnsLookup | None = None) -> None:
        self.lookup: DnsLookup = lookup or default_lookup
        self._data: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def get(self, domain: str, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self._lock:
            hit = self._data.get(domain)
            if hit and hit[0] > now - CACHE_SECONDS:
                return hit[1]
        result = check_domain(domain, self.lookup)
        if not any(c["status"] == "unknown" and "lookup failed" in c["detail"] for c in result["checks"]):
            with self._lock:
                if len(self._data) > 5000:
                    self._data.clear()
                self._data[domain] = (now, result)
        return result

    def many(self, domains: list[str]) -> dict[str, dict]:
        unique = sorted(set(domains))
        with ThreadPoolExecutor(max_workers=4) as pool:
            return dict(zip(unique, pool.map(self.get, unique)))


def ramp(first_sent: float | None, sent_24h: int, established: bool, now: float) -> dict:
    """Daily volume guide for one address."""
    if established:
        return {"phase": "established", "day": None, "limit": None, "sent_24h": sent_24h, "remaining": None}
    day = 1 if first_sent is None else int(max(0.0, now - first_sent) // 86400) + 1
    limit = min(RAMP_MAX, RAMP_START + RAMP_STEP * (day - 1))
    return {
        "phase": "ramping" if limit < RAMP_MAX else "steady",
        "day": day,
        "limit": limit,
        "sent_24h": sent_24h,
        "remaining": max(0, limit - sent_24h),
    }


def trend(emails: list[dict], now: float) -> dict:
    """Open rate, last week against the four weeks before."""

    def rate(items: list[dict]) -> float | None:
        return sum(1 for e in items if e["opens"] > 0) / len(items) if items else None

    recent = [e for e in emails if now - 7 * 86400 <= (e["sent_at"] or 0) < now - 86400]
    before = [e for e in emails if now - 35 * 86400 <= (e["sent_at"] or 0) < now - 7 * 86400]
    r, b = rate(recent), rate(before)
    out: dict[str, object] = {
        "recent": {"emails": len(recent), "open_rate": r},
        "before": {"emails": len(before), "open_rate": b},
    }
    if len(recent) >= TREND_MIN_EMAILS and r == 0:
        out["warning"] = (
            f"None of the last {len(recent)} emails was opened. They are probably landing in spam: "
            "pause campaigns from this address and check the setup above."
        )
    elif len(recent) >= TREND_MIN_EMAILS and len(before) >= TREND_MIN_EMAILS and b and b >= 0.2 and r is not None and r <= b / 2:
        out["warning"] = (
            f"Open rate fell from {round(b * 100)}% to {round(r * 100)}% this week. "
            "Mail from this address may be going to spam or Promotions: slow down and check the setup above."
        )
    return out


def report(
    sender: str,
    emails: list[dict],
    dns: dict | None,
    established: bool,
    now: float,
) -> dict:
    """Everything for one address, with an overall status and plain-language issues."""
    sent = [e["sent_at"] for e in emails if e["sent_at"] is not None]
    sent_24h = sum(1 for t in sent if t >= now - 86400)
    r = ramp(min(sent) if sent else None, sent_24h, established, now)
    t = trend(emails, now)
    issues = []
    status = "ok"
    if dns:
        for c in dns["checks"]:
            if c["status"] == "fail":
                status = "bad"
                issues.append(f"{c['label']}: {c['detail']}")
            elif c["status"] == "warn" and status == "ok":
                status = "warn"
                issues.append(f"{c['label']}: {c['detail']}")
            elif c["status"] == "warn":
                issues.append(f"{c['label']}: {c['detail']}")
    if r["limit"] is not None and sent_24h > r["limit"]:
        status = "bad" if status == "bad" else "warn"
        issues.append(f"{sent_24h} emails in the last 24 hours, above the {r['limit']} a day suggested for day {r['day']}.")
    if "warning" in t:
        status = "bad" if status == "bad" else "warn"
        issues.append(str(t["warning"]))
    return {
        "sender": sender,
        "status": status,
        "issues": issues,
        "dns": dns,
        "ramp": r,
        "trend": t,
        "emails": len(emails),
        "first_sent": min(sent) if sent else None,
        "established": established,
    }
