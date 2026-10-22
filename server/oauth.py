"""Likelyread Cloud as an OAuth 2.1 authorization server for /mcp, so Claude, ChatGPT
and other apps connect with a sign-in instead of a pasted token.

The app learns where to sign in from /mcp's 401 and the /.well-known metadata. The user
signs in with Google here and approves the app, and the app gets an access token that
works at /mcp only, for an hour, with a refresh token to renew it (cloud.py keeps both
as hashes). Loaded only when MODE=cloud: a self-hosted server keeps its one token.

Apps identify themselves with a client metadata document, the registration the MCP
authorization spec recommends: the client_id is an https URL to a JSON document that
names the app and lists its redirect URIs. There is no dynamic client registration and
there are no client secrets: PKCE (S256) ties each code to the app that asked for it.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import http.client
import ipaddress
import json
import re
import secrets
import socket
import ssl
import time
import urllib.parse
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app import RateLimiter
from cloud import Cloud, _b64url, token_hash

# An authorization code is redeemed once, within this long.
CODE_SECONDS = 300
# Client metadata documents: fetched within DOC_TIMEOUT seconds, at most DOC_MAX_BYTES,
# and cached as their Cache-Control says, between one minute and a day.
DOC_TIMEOUT = 5
DOC_MAX_BYTES = 16 * 1024
DOC_CACHE_DEFAULT, DOC_CACHE_MIN, DOC_CACHE_MAX = 300, 60, 86400
DOC_CACHE_SIZE = 200
# Documents fetched per client IP and minute (cached ones are free).
DOC_FETCHES_PER_MIN = 20
# Longest client_id and redirect_uri. The checked request travels through sign-in in
# signed cookies, which browsers drop above 4 KB: MAX_REQUEST bytes of JSON fit in both.
MAX_URL = 512
MAX_REQUEST = 2400
FORM_MAX_BYTES = 64 * 1024

VERIFIER_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
HOST_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
LOOPBACK = ("localhost", "127.0.0.1", "::1")

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache", "Access-Control-Allow-Origin": "*"}


class ClientError(Exception):
    """The request names no app this server can check. It is shown to the user, never
    sent to a redirect URI that could not be checked."""


# ---------------------------------------------------------------- redirect URIs


def _loopback(host: str | None) -> bool:
    return (host or "").lower() in LOOPBACK


def _port_ok(u: urllib.parse.SplitResult) -> bool:
    try:
        u.port
    except ValueError:
        return False
    return True


def redirect_allowed(uri: str) -> bool:
    """https on a plain host name, or http on this computer (an app such as Claude Code)."""
    u = urllib.parse.urlsplit(uri)
    host = u.hostname or ""
    if len(uri) > MAX_URL or u.fragment or u.username or u.password or not _port_ok(u):
        return False
    if u.scheme == "http":
        return _loopback(host)
    return u.scheme == "https" and bool(HOST_RE.match(host))


def redirect_matches(requested: str, registered: list[str]) -> bool:
    """Exact match, except that a loopback address matches on any port: a native app
    listens on a port it picks when it starts (RFC 8252, section 7.3)."""
    if requested in registered:
        return True
    r = urllib.parse.urlsplit(requested)
    if r.scheme != "http" or not _loopback(r.hostname) or not redirect_allowed(requested):
        return False
    for uri in registered:
        u = urllib.parse.urlsplit(uri)
        if (u.scheme, u.hostname, u.path or "/", u.query) == ("http", r.hostname, r.path or "/", r.query):
            return True
    return False


def origin_of(uri: str) -> str:
    """scheme://host[:port] of a URI that passed redirect_allowed, for the CSP."""
    u = urllib.parse.urlsplit(uri)
    host = f"[{u.hostname}]" if ":" in (u.hostname or "") else u.hostname
    return f"{u.scheme}://{host}" + (f":{u.port}" if u.port else "")


# ---------------------------------------------------------------- client metadata documents


def check_client_id(client_id: str, local: bool = False) -> None:
    """A client_id must be the https URL of the app's client metadata document, on a
    host name and the default port. On a server running on this computer (development
    and tests), a document on this computer works too."""
    u = urllib.parse.urlsplit(client_id)
    host = u.hostname or ""
    if len(client_id) > MAX_URL or u.username or u.password or u.fragment or not _port_ok(u):
        raise ClientError("its client_id is not a valid URL")
    if u.path in ("", "/") or any(part in (".", "..") for part in u.path.split("/")):
        raise ClientError("its client_id has no document path")
    if local and u.scheme == "http" and _loopback(host):
        return
    if u.scheme != "https" or u.port not in (None, 443) or not HOST_RE.match(host):
        raise ClientError("its client_id must be the https URL of its client metadata document")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return
    raise ClientError("its client_id must use a host name, not an IP address")


class _Pinned(http.client.HTTPSConnection):
    """HTTPS to a host name at an address already checked: the connection goes to that
    address, while SNI and the certificate check use the name."""

    def __init__(self, host: str, addr: str, port: int) -> None:
        super().__init__(host, port, timeout=DOC_TIMEOUT, context=ssl.create_default_context())
        self.addr = addr

    def connect(self) -> None:
        sock = socket.create_connection((self.addr, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]


def fetch_document(url: str, local: bool = False) -> tuple[int, str, bytes]:
    """GET a client metadata document: (status, Cache-Control, body). Anyone can put any
    URL in a client_id, so this connects only to public internet addresses (and this
    computer's, when local), to the very address it checked, follows no redirect and
    reads at most DOC_MAX_BYTES + 1 bytes within DOC_TIMEOUT. Replaced in tests."""
    u = urllib.parse.urlsplit(url)
    host, port = u.hostname or "", u.port or (443 if u.scheme == "https" else 80)
    try:
        addrs = [str(info[4][0]) for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
    except OSError:
        raise ClientError(f"{host} does not resolve") from None
    for addr in addrs:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
        if not (ip.is_global or (local and ip.is_loopback)):
            raise ClientError(f"{host} is not on the public internet")
    conn = _Pinned(host, addrs[0], port) if u.scheme == "https" else http.client.HTTPConnection(host, port, timeout=DOC_TIMEOUT)
    deadline = time.monotonic() + DOC_TIMEOUT
    try:
        conn.request("GET", (u.path or "/") + (f"?{u.query}" if u.query else ""),
                     headers={"Accept": "application/json", "User-Agent": "likelyread-cloud"})
        resp = conn.getresponse()
        body = b""
        while len(body) <= DOC_MAX_BYTES:
            if time.monotonic() > deadline:
                raise ClientError("its client metadata document took too long")
            chunk = resp.read1(4096)
            if not chunk:
                break
            body += chunk
        return resp.status, resp.getheader("cache-control") or "", body
    except (OSError, http.client.HTTPException) as exc:
        raise ClientError(f"its client metadata document could not be fetched ({exc.__class__.__name__})") from None
    finally:
        conn.close()


def parse_document(client_id: str, status: int, body: bytes) -> dict:
    """The app's name and redirect URIs, from its client metadata document."""
    if status != 200:
        raise ClientError(f"its client metadata document answered HTTP {status}")
    if len(body) > DOC_MAX_BYTES:
        raise ClientError("its client metadata document is too large")
    try:
        doc = json.loads(body)
    except ValueError:
        raise ClientError("its client metadata document is not JSON") from None
    if not isinstance(doc, dict) or doc.get("client_id") != client_id:
        raise ClientError("its client metadata document names another client_id")
    listed = doc.get("redirect_uris")
    uris = [u for u in listed if isinstance(u, str) and redirect_allowed(u)] if isinstance(listed, list) else []
    if not uris:
        raise ClientError("its client metadata document lists no https or loopback redirect URI")
    for key, needed in (("grant_types", "authorization_code"), ("response_types", "code")):
        values = doc.get(key, [needed])
        if not isinstance(values, list) or needed not in values:
            raise ClientError(f"its client metadata document does not allow {needed}")
    name = doc.get("client_name")
    name = " ".join(name.split())[:80] if isinstance(name, str) else ""
    return {"name": name or urllib.parse.urlsplit(client_id).hostname, "redirect_uris": uris}


def cache_seconds(cache_control: str) -> int:
    cc = cache_control.lower()
    if "no-store" in cc:
        return 0
    m = re.search(r"max-age=(\d+)", cc)
    return min(max(int(m.group(1)) if m else DOC_CACHE_DEFAULT, DOC_CACHE_MIN), DOC_CACHE_MAX)


# ---------------------------------------------------------------- HTTP helpers


async def read_form(request: Request) -> dict[str, str] | None:
    """A form-encoded body, as OAuth sends it. None for another content type, a body
    over FORM_MAX_BYTES or a parameter that comes twice (RFC 6749, section 3.2)."""
    kind = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if kind != "application/x-www-form-urlencoded":
        return None
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > FORM_MAX_BYTES:
            return None
    try:
        pairs = urllib.parse.parse_qsl(body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        return None
    form = dict(pairs)
    return form if len(form) == len(pairs) else None


def token_error(error: str, description: str) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=400, headers=NO_STORE)


def page(title: str, body: str, status: int = 200, form_to: str = "") -> HTMLResponse:
    """A page in the dashboard's style. body is HTML whose values went through html.escape.
    form_to is the origin the consent form's answer redirects to, which the CSP allows."""
    csp = (
        "default-src 'none'; style-src 'self'; font-src 'self'; img-src 'self'; "
        f"form-action 'self'{' ' + form_to if form_to else ''}; frame-ancestors 'none'; base-uri 'none'"
    )
    doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex, nofollow">
  <title>{html.escape(title)} · Likelyread</title>
  <link rel="icon" href="/assets/favicon.svg" type="image/svg+xml">
  <link rel="stylesheet" href="/assets/fonts.css">
  <link rel="stylesheet" href="/assets/style.css">
</head>
<body>
  <main class="login">
    <div class="login-card">
      <p class="wordmark">likelyread</p>
      {body}
    </div>
  </main>
</body>
</html>
"""
    return HTMLResponse(doc, status_code=status, headers={"Content-Security-Policy": csp, "Cache-Control": "no-store"})


def error_page(title: str, text: str, status: int = 400) -> HTMLResponse:
    return page(title, f'<h1 class="login-title">{html.escape(title)}</h1>\n      <p class="muted">{html.escape(text)}</p>', status)


# ---------------------------------------------------------------- the authorization server


class OAuth:
    def __init__(self, cloud: Cloud) -> None:
        self.cloud = cloud
        self.db = cloud.db
        self.issuer = cloud.cfg.public_url
        self.resource = f"{self.issuer}/mcp"
        self.metadata_url = f"{self.issuer}/.well-known/oauth-protected-resource/mcp"
        self.secure = self.issuer.startswith("https://")
        # A server on this computer (development, tests) also takes client documents on it.
        self.local = self.issuer.startswith("http://")
        self.fetch: Callable[[str, bool], tuple[int, str, bytes]] = fetch_document
        self.docs: dict[str, tuple[float, dict]] = {}
        self.limiter = RateLimiter(DOC_FETCHES_PER_MIN)

    # ---- metadata ---------------------------------------------------------

    def resource_metadata(self) -> dict:
        """RFC 9728: /mcp, and the server that issues its tokens (this one)."""
        return {
            "resource": self.resource,
            "authorization_servers": [self.issuer],
            "bearer_methods_supported": ["header"],
            "resource_name": "Likelyread",
            "resource_documentation": "https://github.com/Hugo-SEQUIER/likelyread/blob/main/docs/CLOUD.md",
            "resource_policy_uri": "https://likelyread.com/privacy",
            "resource_tos_uri": "https://likelyread.com/terms",
        }

    def server_metadata(self) -> dict:
        """RFC 8414. Public clients only ("none"), identified by client metadata documents,
        with the issuer in every authorization response (RFC 9207). No scopes: a token
        can do what an agent token does."""
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/oauth/authorize",
            "token_endpoint": f"{self.issuer}/oauth/token",
            "revocation_endpoint": f"{self.issuer}/oauth/revoke",
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": True,
            "service_documentation": "https://github.com/Hugo-SEQUIER/likelyread/blob/main/docs/CLOUD.md",
        }

    def challenge(self, token_sent: bool) -> HTTPException:
        """/mcp's 401: where this server's metadata is (RFC 9728), plus invalid_token when
        a token came and failed, so the app renews it (RFC 6750, section 3.1)."""
        value = f'Bearer resource_metadata="{self.metadata_url}"'
        if token_sent:
            value = f'Bearer error="invalid_token", resource_metadata="{self.metadata_url}"'
        return HTTPException(status_code=401, detail="invalid token", headers={"WWW-Authenticate": value})

    def our_resource(self, value: str) -> bool:
        """The resource a token is asked for (RFC 8707): /mcp, or this server as a whole."""
        u = urllib.parse.urlsplit(value.strip())
        if u.query or u.fragment:
            return False
        asked = f"{u.scheme.lower()}://{u.netloc.lower()}{u.path.rstrip('/')}"
        return asked in (self.resource.lower(), self.issuer.lower())

    # ---- the app behind a client_id ----------------------------------------

    def client(self, client_id: str, ip: str) -> dict:
        check_client_id(client_id, self.local)
        now = time.time()
        cached = self.docs.get(client_id)
        if cached and cached[0] > now:
            return cached[1]
        if not self.limiter.allow(ip or "?"):
            raise ClientError("too many sign-ins from this address: wait a minute")
        status, cache_control, body = self.fetch(client_id, self.local)
        doc = parse_document(client_id, status, body)
        ttl = cache_seconds(cache_control)
        if ttl:
            if len(self.docs) >= DOC_CACHE_SIZE:
                self.docs = {k: v for k, v in self.docs.items() if v[0] > now}
                if len(self.docs) >= DOC_CACHE_SIZE:
                    self.docs.clear()
            self.docs[client_id] = (now + ttl, doc)
        return doc

    # ---- the authorization request, sign-in and consent ----------------------

    def to_app(self, redirect_uri: str, state: str | None, **params: str) -> RedirectResponse:
        """The answer on the app's redirect URI, naming this server in iss (RFC 9207)."""
        query = {**params, **({"state": state} if state is not None else {}), "iss": self.issuer}
        u = urllib.parse.urlsplit(redirect_uri)
        joined = (u.query + "&" if u.query else "") + urllib.parse.urlencode(query)
        return RedirectResponse(urllib.parse.urlunsplit(u._replace(query=joined)), status_code=303)

    def authorize(self, request: Request) -> Response:
        q = request.query_params
        if any(len(q.getlist(k)) > 1 for k in q.keys()):
            return error_page("This app cannot connect", "The request repeats a parameter. Try again from the app.")
        client_id, redirect_uri = q.get("client_id", ""), q.get("redirect_uri", "")
        try:
            app = self.client(client_id, request.client.host if request.client else "")
        except ClientError as exc:
            return error_page(
                "This app cannot connect",
                f"Likelyread could not check which app is asking: {exc}. You can still connect it with an "
                "agent token from Connect in your dashboard.",
            )
        if not redirect_matches(redirect_uri, app["redirect_uris"]):
            return error_page(
                "This app cannot connect",
                "It asked to come back to an address that its client metadata document does not list.",
            )
        state = q.get("state")
        if q.get("response_type") != "code":
            return self.to_app(redirect_uri, state, error="unsupported_response_type",
                               error_description="Only response_type=code is supported.")
        if q.get("code_challenge_method") != "S256" or not CHALLENGE_RE.match(q.get("code_challenge", "")):
            return self.to_app(redirect_uri, state, error="invalid_request",
                               error_description="PKCE is required, with code_challenge_method=S256.")
        resource = q.get("resource")
        if resource is not None and not self.our_resource(resource):
            return self.to_app(redirect_uri, state, error="invalid_target",
                               error_description=f"This server issues tokens for {self.resource} only.")
        request_ = {"client_id": client_id, "name": app["name"], "redirect_uri": redirect_uri,
                    "state": state, "challenge": q["code_challenge"]}
        if len(json.dumps(request_)) > MAX_REQUEST:
            return self.to_app(redirect_uri, state, error="invalid_request", error_description="The request is too long.")
        return self.cloud.google_redirect(app=request_)

    def after_sign_in(self, app: dict, who: tuple[str, str] | None) -> Response:
        """Google answered (cloud.py): on to the consent page as that account, or back to
        the app when sign-in failed."""
        if who is None:
            return self.to_app(app["redirect_uri"], app.get("state"), error="access_denied",
                               error_description="Google sign-in did not complete.")
        acct, email = who
        resp = RedirectResponse(f"{self.issuer}/oauth/consent", status_code=303)
        saved = {"acct": acct, "email": email, "app": app, "nonce": secrets.token_urlsafe(16), "exp": time.time() + 600}
        resp.set_cookie("lr_app", self.cloud._sign(saved), max_age=600, httponly=True, secure=self.secure,
                        samesite="lax", path="/oauth")
        return resp

    def consent_page(self, saved: dict) -> HTMLResponse:
        app = saved["app"]
        name = html.escape(app["name"])
        back = urllib.parse.urlsplit(app["redirect_uri"])
        here = _loopback(back.hostname)
        to = f"the app on this computer ({back.netloc})" if here else back.hostname
        warn = (
            f'\n      <p class="consent-warn">{name} runs on this computer. Approve only if you started this '
            "connection yourself, just now.</p>"
            if here else ""
        )
        body = f"""<h1 class="login-title">Connect {name}</h1>
      <p><strong>{html.escape(urllib.parse.urlsplit(app["client_id"]).hostname or "")}</strong> wants to use your Likelyread account, {html.escape(saved["email"])}.</p>
      <ul class="consent">
        <li>It can track the emails it sends for you, and read your tracked emails, their opens and your campaigns.</li>
        <li>It cannot change, export or delete your account.</li>
      </ul>{warn}
      <form method="post" action="/oauth/consent" class="inline-form">
        <input type="hidden" name="nonce" value="{html.escape(saved["nonce"])}">
        <button class="btn primary" type="submit" name="action" value="approve">Approve</button>
        <button class="btn" type="submit" name="action" value="deny">Cancel</button>
      </form>
      <p class="muted small">Then you go back to {html.escape(to or "")}. You can disconnect it any time under Account in your dashboard.</p>"""
        return page(f"Connect {app['name']}", body, form_to=origin_of(app["redirect_uri"]))

    async def consent(self, request: Request) -> Response:
        form = await read_form(request)
        saved = self.cloud._unsign(request.cookies.get("lr_app", ""))
        if form is None or not saved or not hmac.compare_digest(str(saved.get("nonce", "")), form.get("nonce", "")):
            return error_page("This sign-in has expired", "Start again from the app you were connecting.")
        app = saved["app"]
        if form.get("action") == "approve":
            code = secrets.token_urlsafe(32)
            self.db.execute(
                "INSERT INTO app_codes(hash, account_id, client_id, name, redirect_uri, challenge, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (token_hash(code), saved["acct"], app["client_id"], app["name"], app["redirect_uri"],
                 app["challenge"], time.time() + CODE_SECONDS),
            )
            resp = self.to_app(app["redirect_uri"], app.get("state"), code=code)
        else:
            resp = self.to_app(app["redirect_uri"], app.get("state"), error="access_denied",
                               error_description="The user did not approve the connection.")
        resp.delete_cookie("lr_app", path="/oauth")
        return resp

    # ---- the token endpoint --------------------------------------------------

    def redeem_code(self, form: dict[str, str]) -> JSONResponse:
        code, client_id = form.get("code", ""), form.get("client_id", "")
        verifier, redirect_uri = form.get("code_verifier", ""), form.get("redirect_uri", "")
        if not (code and client_id and verifier and redirect_uri):
            return token_error("invalid_request", "code, client_id, code_verifier and redirect_uri are required.")
        key = token_hash(code)
        with self.db.lock:
            row = self.db.conn.execute("SELECT * FROM app_codes WHERE hash = ?", (key,)).fetchone()
            ok = (
                row is not None
                and row["expires_at"] > time.time()
                and row["client_id"] == client_id
                and row["redirect_uri"] == redirect_uri
                and VERIFIER_RE.match(verifier) is not None
                and hmac.compare_digest(_b64url(hashlib.sha256(verifier.encode()).digest()), row["challenge"])
                and self.cloud.account(row["account_id"]) is not None
            )
            if ok:
                self.db.conn.execute("DELETE FROM app_codes WHERE hash = ?", (key,))  # spent: once only
        if not ok:
            return token_error("invalid_grant", "This authorization code is not valid. Start the sign-in again.")
        return JSONResponse(self.cloud.connect_app(row["account_id"], row["client_id"], row["name"]), headers=NO_STORE)

    async def token(self, request: Request) -> JSONResponse:
        form = await read_form(request)
        if form is None:
            return token_error("invalid_request", "Send the parameters form-encoded, each once.")
        resource = form.get("resource")
        if resource is not None and not self.our_resource(resource):
            return token_error("invalid_target", f"This server issues tokens for {self.resource} only.")
        grant = form.get("grant_type")
        if grant == "authorization_code":
            return self.redeem_code(form)
        if grant == "refresh_token":
            if not form.get("refresh_token") or not form.get("client_id"):
                return token_error("invalid_request", "refresh_token and client_id are required.")
            tokens = self.cloud.refresh_app(form["refresh_token"], form["client_id"])
            if tokens is None:
                return token_error("invalid_grant", "This refresh token is not valid. Connect the app again.")
            return JSONResponse(tokens, headers=NO_STORE)
        return token_error("unsupported_grant_type", "Use authorization_code or refresh_token.")

    async def revoke(self, request: Request) -> Response:
        """RFC 7009: ends the connection a token belongs to. Unknown tokens answer 200 too."""
        form = await read_form(request)
        if form is None or not form.get("token"):
            return token_error("invalid_request", "token is required.")
        h = token_hash(form["token"])
        sql, args = "DELETE FROM apps WHERE (access = ? OR refresh = ? OR refresh_prev = ?)", [h, h, h]
        if form.get("client_id"):
            sql += " AND client_id = ?"
            args.append(form["client_id"])
        self.db.execute(sql, args)
        return Response(status_code=200, headers=NO_STORE)

    # ---- routes --------------------------------------------------------------

    def register_routes(self, app: FastAPI) -> None:
        public = {"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=300"}

        def resource_metadata():
            return JSONResponse(self.resource_metadata(), headers=public)

        # The path-suffixed form first (RFC 9728, section 3.1), then the origin's.
        app.add_api_route("/.well-known/oauth-protected-resource/mcp", resource_metadata, methods=["GET"])
        app.add_api_route("/.well-known/oauth-protected-resource", resource_metadata, methods=["GET"])

        @app.get("/.well-known/oauth-authorization-server")
        def server_metadata():
            return JSONResponse(self.server_metadata(), headers=public)

        @app.get("/oauth/authorize")
        def authorize(request: Request):
            return self.authorize(request)

        @app.get("/oauth/consent")
        def consent_page(request: Request):
            saved = self.cloud._unsign(request.cookies.get("lr_app", ""))
            if not saved or not isinstance(saved.get("app"), dict):
                return error_page("This sign-in has expired", "Start again from the app you were connecting.")
            return self.consent_page(saved)

        @app.post("/oauth/consent")
        async def consent(request: Request):
            return await self.consent(request)

        @app.post("/oauth/token")
        async def token(request: Request):
            return await self.token(request)

        @app.post("/oauth/revoke")
        async def revoke(request: Request):
            return await self.revoke(request)

        def preflight():
            # For apps that run in a browser, such as the MCP Inspector. No cookies are involved.
            return Response(status_code=204, headers={
                "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "POST",
                "Access-Control-Allow-Headers": "Content-Type, MCP-Protocol-Version", "Access-Control-Max-Age": "86400",
            })

        app.add_api_route("/oauth/token", preflight, methods=["OPTIONS"])
        app.add_api_route("/oauth/revoke", preflight, methods=["OPTIONS"])
