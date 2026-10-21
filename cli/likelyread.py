#!/usr/bin/env python3
"""likelyread: command line for agents and scripts. Python 3.9+, stdlib only.

Every command prints JSON on stdout so an agent can parse it (except
`html --raw`, which prints the HTML itself). Errors print
{"error": ...} on stderr and exit non-zero.

Quickest setup: `likelyread setup 'https://your-server/#connect=TOKEN'` with the
setup link printed by the installer (also on the dashboard, under Connect).

Configuration, in order of precedence:
  env    LIKELYREAD_URL       public base URL that recipients load the pixel from
         LIKELYREAD_API_URL   where to call the API (default: LIKELYREAD_URL);
                              use http://127.0.0.1:PORT when the server is local
         LIKELYREAD_TOKEN     the server token
  file   ~/.config/likelyread/config.json  {"url": ..., "api_url": ..., "token": ...}
         (or the path in LIKELYREAD_CONFIG). Keep it mode 600.

Typical agent flow for an email sent later (draft, then send):
  likelyread pixel                       -> {"id", "img"}: put img in the HTML part
  ... send the email ...
  likelyread track --id ID --subject S --to a@b.com --campaign C --from me@x.com --sent-at 1792...
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

VERSION = "0.3.0"


class CliError(Exception):
    pass


# ---------------------------------------------------------------- config


def load_config(environ: Mapping[str, str] | None = None) -> dict:
    env: Mapping[str, str] = os.environ if environ is None else environ
    path = Path(env.get("LIKELYREAD_CONFIG") or Path.home() / ".config" / "likelyread" / "config.json")
    data: dict = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text())
        except ValueError as e:
            raise CliError(f"invalid config file {path}: {e}") from None
    url = (env.get("LIKELYREAD_URL") or data.get("url") or "").rstrip("/")
    api_url = (env.get("LIKELYREAD_API_URL") or data.get("api_url") or url).rstrip("/")
    token = env.get("LIKELYREAD_TOKEN") or data.get("token") or ""
    return {"url": url, "api_url": api_url, "token": token}


def need(cfg: dict, *keys: str) -> None:
    missing = [k for k in keys if not cfg.get(k)]
    if missing:
        names = {"url": "LIKELYREAD_URL", "api_url": "LIKELYREAD_API_URL", "token": "LIKELYREAD_TOKEN"}
        raise CliError("not configured: set " + ", ".join(names[k] for k in missing) + " or ~/.config/likelyread/config.json")


# ---------------------------------------------------------------- pixel and html


def new_id() -> str:
    return str(uuid.uuid4())


def pixel_url(base: str, email_id: str) -> str:
    return f"{base.rstrip('/')}/p/{email_id}.gif"


def pixel_tag(base: str, email_id: str) -> str:
    return f'<img src="{pixel_url(base, email_id)}" width="1" height="1" alt="" style="display:none!important">'


URL_RE = re.compile(r"https?://[^\s<>\"']+")


def _linkify(escaped: str) -> str:
    def repl(m: re.Match) -> str:
        url = m.group(0)
        trail = ""
        while url and url[-1] in ".,;:!?)":  # sentence punctuation is not part of the link
            trail = url[-1] + trail
            url = url[:-1]
        return f'<a href="{url}">{url}</a>{trail}'

    return URL_RE.sub(repl, escaped)


def text_to_html(text: str, pixel: str | None = None) -> str:
    """Plain text to a minimal HTML body: blank lines split paragraphs, single
    newlines become <br>, text is escaped and bare URLs become links. The
    visible content stays identical to the plain-text part."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    out = []
    for p in paras:
        lines = [_linkify(html.escape(line, quote=True)) for line in p.split("\n")]
        out.append("<p>" + "<br>".join(lines) + "</p>")
    body = "\n".join(out)
    if pixel:
        body += "\n" + pixel
    return f'<div dir="ltr">\n{body}\n</div>'


def parse_time(value: str | None) -> float | None:
    """Unix seconds, unix milliseconds (Gmail internalDate) or ISO 8601."""
    if value in (None, ""):
        return None
    v = value.strip()
    if re.fullmatch(r"\d+(\.\d+)?", v):
        n = float(v)
        return n / 1000 if n > 1e11 else n
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise CliError(f"cannot parse time: {value!r}") from None
    if dt.tzinfo is None:
        raise CliError("ISO time needs a timezone, e.g. 2026-10-19T11:00:00+09:00")
    return dt.timestamp()


EMAIL_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def split_addresses(values: list[str] | None) -> list[str]:
    """Addresses from values like 'a@x.com, Maya H <maya@y.com>'. Display names are dropped."""
    out: list[str] = []
    for v in values or []:
        for addr in EMAIL_RE.findall(v):
            addr = addr.lower()
            if addr not in out:
                out.append(addr)
    return out


UUID4_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def checked_id(value: str | None) -> str:
    """A pixel id: a new UUIDv4, or the given one if it is a valid UUIDv4."""
    if not value:
        return new_id()
    v = value.strip().lower()
    if not UUID4_RE.match(v):
        raise CliError(f"--id must be a UUIDv4 (as printed by `likelyread pixel`), got {value!r}")
    return v


def local_tz_minutes() -> int:
    off = datetime.now().astimezone().utcoffset()
    return int(off.total_seconds() // 60) if off else 0


# ---------------------------------------------------------------- API


def api(cfg: dict, method: str, path: str, body: dict | None = None, query: dict | None = None):
    need(cfg, "api_url", "token")
    q = {k: v for k, v in (query or {}).items() if v not in (None, "")}
    url = cfg["api_url"] + path + ("?" + urllib.parse.urlencode(q) if q else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + cfg["token"])
    req.add_header("User-Agent", f"likelyread-cli/{VERSION}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        if e.code == 401:
            raise CliError("token rejected (401)") from None
        raise CliError(f"{method} {path} failed: {e.code} {detail}") from None
    except urllib.error.URLError as e:
        raise CliError(f"cannot reach {cfg['api_url']}: {e.reason}") from None
    return json.loads(raw) if raw else None


# ---------------------------------------------------------------- commands


def cmd_pixel(cfg: dict, a) -> dict:
    need(cfg, "url")
    email_id = checked_id(a.id)
    return {"id": email_id, "url": pixel_url(cfg["url"], email_id), "img": pixel_tag(cfg["url"], email_id)}


def cmd_html(cfg: dict, a) -> dict | str:
    text = Path(a.file).read_text() if a.file else sys.stdin.read()
    tag = None
    if a.id:
        need(cfg, "url")
        tag = pixel_tag(cfg["url"], checked_id(a.id))
    out = text_to_html(text, tag)
    return out if a.raw else {"id": a.id, "html": out}


def cmd_track(cfg: dict, a) -> dict:
    need(cfg, "url")
    email_id = checked_id(a.id)
    sent_at = parse_time(a.sent_at)
    age = 0.0 if sent_at is None else max(0.0, time.time() - sent_at)
    body = {
        "id": email_id,
        "subject": a.subject or "",
        "recipients": split_addresses(a.to),
        "age": age,
        "campaign": a.campaign or None,
        "sender": (a.sender or "").strip().lower() or None,
    }
    api(cfg, "POST", "/api/emails", body)
    return {"id": email_id, "registered": True, "img": pixel_tag(cfg["url"], email_id), "age_seconds": round(age, 1)}


def cmd_ls(cfg: dict, a):
    q = {"campaign": a.campaign, "sender": a.sender, "status": a.status, "q": a.q, "limit": a.limit}
    return api(cfg, "GET", "/api/emails", query=q)


def cmd_show(cfg: dict, a):
    email = api(cfg, "GET", f"/api/emails/{urllib.parse.quote(a.id)}")
    if not a.full:
        # Whoever loads the pixel chooses its user agent, so it could carry instructions
        # for an agent reading this: keep only the server's own words unless asked.
        email["hits"] = [{k: h.get(k) for k in ("ts", "kind", "via", "reason")} for h in email.get("hits", [])]
    return email


def cmd_campaigns(cfg: dict, a):
    return api(cfg, "GET", "/api/campaigns")


def cmd_stats(cfg: dict, a):
    return api(cfg, "GET", "/api/stats", query={"campaign": a.campaign, "sender": a.sender, "days": a.days, "tz": local_tz_minutes() if a.tz is None else a.tz})


def cmd_health(cfg: dict, a):
    """Sender health. With --from, one object: check ramp.remaining before a batch."""
    rows = api(cfg, "GET", "/api/senders/health", query={"sender": a.sender, "dns": "false" if a.no_dns else None})
    if a.sender:
        if not rows:
            raise CliError(f"no tracked email from {a.sender} yet")
        return rows[0]
    return rows


def cmd_set_campaign(cfg: dict, a):
    return api(cfg, "PATCH", f"/api/emails/{urllib.parse.quote(a.id)}", {"campaign": a.campaign or None})


SETUP_RE = re.compile(r"^#connect=([A-Za-z0-9._~-]{16,})$")


def parse_setup_link(link: str) -> tuple[str, str]:
    """(base_url, token) from https://server/#connect=TOKEN."""
    u = urllib.parse.urlsplit(link.strip())
    m = SETUP_RE.match("#" + u.fragment) if u.fragment else None
    if u.scheme != "https" or not u.netloc or not m:
        raise CliError("not a setup link: expected https://your-server/#connect=TOKEN")
    return f"https://{u.netloc}", m.group(1)


def cmd_setup(cfg: dict, a) -> dict:
    url, token = parse_setup_link(a.link)
    path = Path(os.environ.get("LIKELYREAD_CONFIG") or Path.home() / ".config" / "likelyread" / "config.json")
    data = {"url": url, "token": token}
    if a.api_url:
        data["api_url"] = a.api_url.rstrip("/")
    new_cfg = {"url": url, "api_url": data.get("api_url", url), "token": token}
    if not a.no_check:
        api(new_cfg, "GET", "/api/ping")  # fails with a clear error before anything is written
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)
    return {"ok": True, "config": str(path), "url": url, "api_url": new_cfg["api_url"], "checked": not a.no_check}


def cmd_ping(cfg: dict, a):
    return {"ok": True, "api_url": cfg["api_url"], "pixel_url": cfg["url"], "server": api(cfg, "GET", "/api/ping")}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="likelyread", description="Likelyread open tracking for agents. Prints JSON.")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("pixel", help="new pixel id and <img> tag (offline, registers nothing)")
    s.add_argument("--id", help="use this id instead of a new one")
    s.set_defaults(fn=cmd_pixel)

    s = sub.add_parser("html", help="plain text (stdin or --file) to an HTML body, with the pixel if --id")
    s.add_argument("--file")
    s.add_argument("--id", help="append the pixel for this id")
    s.add_argument("--raw", action="store_true", help="print the HTML itself, not JSON")
    s.set_defaults(fn=cmd_html)

    s = sub.add_parser("track", help="register a sent email (idempotent)")
    s.add_argument("--id", help="pixel id already in the email; omit to create one")
    s.add_argument("--subject", default="")
    s.add_argument("--to", action="append", help="recipient(s); repeat or comma-separate")
    s.add_argument("--campaign")
    s.add_argument("--from", dest="sender", help="address the email was sent from")
    s.add_argument("--sent-at", help="when it was sent: unix s, unix ms (Gmail internalDate) or ISO 8601; default now")
    s.set_defaults(fn=cmd_track)

    s = sub.add_parser("ls", help="recent emails with status")
    s.add_argument("--campaign", help="campaign name, or __none__ for emails without one")
    s.add_argument("--from", dest="sender")
    s.add_argument("--status", choices=["opened", "not_opened", "prefetch_only"])
    s.add_argument("--q", help="search subject, recipient, campaign")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(fn=cmd_ls)

    s = sub.add_parser("show", help="one email with every hit and why it was classified that way")
    s.add_argument("id")
    s.add_argument("--full", action="store_true", help="also each hit's user agent and IP, set by whoever loaded the image")
    s.set_defaults(fn=cmd_show)

    sub.add_parser("campaigns", help="per-campaign open rates").set_defaults(fn=cmd_campaigns)

    s = sub.add_parser("stats", help="totals, timeline and recent opens")
    s.add_argument("--campaign")
    s.add_argument("--from", dest="sender")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--tz", type=int, help="minutes east of UTC for daily and hourly buckets (default: this machine's)")
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("health", help="sender health: SPF/DKIM/DMARC, daily pace (ramp.remaining) and open-rate trend")
    s.add_argument("--from", dest="sender", help="one sending address")
    s.add_argument("--no-dns", action="store_true", help="skip the DNS checks (fast)")
    s.set_defaults(fn=cmd_health)

    s = sub.add_parser("set-campaign", help="move an email to another campaign (empty for none)")
    s.add_argument("id")
    s.add_argument("campaign", nargs="?", default="")
    s.set_defaults(fn=cmd_set_campaign)

    sub.add_parser("ping", help="check configuration and token").set_defaults(fn=cmd_ping)

    s = sub.add_parser("setup", help="save the configuration from a setup link (https://server/#connect=TOKEN)")
    s.add_argument("link")
    s.add_argument("--api-url", help="call the API here instead, e.g. http://127.0.0.1:8100 on the server itself")
    s.add_argument("--no-check", action="store_true", help="save without testing the connection")
    s.set_defaults(fn=cmd_setup)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.fn(load_config(), args)
    except CliError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1
    if isinstance(result, str):
        sys.stdout.write(result + "\n")
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
