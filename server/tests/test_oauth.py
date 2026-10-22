"""OAuth for apps (oauth.py): Claude, ChatGPT and Claude Code sign in and get a token for
/mcp. Google and the apps' client metadata documents are replaced with fakes; the
documents are copies of the ones Claude and ChatGPT published on 22 Oct 2026."""

import base64
import hashlib
import json
import re
import secrets
import socket
import urllib.parse
import uuid

import pytest
from fastapi.testclient import TestClient

import app as likelyread
import cloud
import oauth

BASE = "https://app.likelyread.com"
MCP = f"{BASE}/mcp"
NOW = 1_800_000_000.0
CLIENT_ID = "cid.apps.googleusercontent.com"

CLAUDE = "https://claude.ai/oauth/mcp-oauth-client-metadata"
CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"
CLAUDE_CODE = "https://claude.ai/oauth/claude-code-client-metadata"
CHATGPT = "https://chatgpt.com/oauth/client.json"
CHATGPT_CB = "https://chatgpt.com/connector_platform_oauth_redirect"
DOCS = {
    CLAUDE: {
        "client_id": CLAUDE, "client_name": "Claude", "client_uri": "https://claude.ai", "redirect_uris": [CLAUDE_CB],
        "grant_types": ["authorization_code", "refresh_token", "urn:ietf:params:oauth:grant-type:jwt-bearer"],
        "response_types": ["code"], "token_endpoint_auth_method": "none",
    },
    CLAUDE_CODE: {
        "client_id": CLAUDE_CODE, "client_name": "Claude Code", "client_uri": "https://claude.ai",
        "redirect_uris": ["http://localhost/callback", "http://127.0.0.1/callback"],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    },
    CHATGPT: {
        "client_id": CHATGPT, "client_uri": "https://chatgpt.com/", "redirect_uris": [CHATGPT_CB],
        "token_endpoint_auth_method": "private_key_jwt", "token_endpoint_auth_methods_supported": ["none", "private_key_jwt"],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "client_name": "ChatGPT",
        "logo_uri": "https://persistent.oaistatic.com/sonic/misc/openai-logo.png",
        "token_endpoint_auth_signing_alg": "RS256", "jwks_uri": "https://chatgpt.com/oauth/jwks.json",
    },
}
JSONRPC = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"}


@pytest.fixture
def clock(monkeypatch):
    state = {"now": NOW}
    monkeypatch.setattr(likelyread.time, "time", lambda: state["now"])
    return state


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in {
        "MODE": "cloud", "DB_PATH": str(tmp_path / "cloud.db"), "PUBLIC_URL": BASE, "DOMAIN": "app.likelyread.com",
        "SESSION_SECRET": "s" * 40, "GOOGLE_CLIENT_ID": CLIENT_ID, "GOOGLE_CLIENT_SECRET": "gsecret",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("LIKELYREAD_TOKEN", raising=False)


class Google:
    """Google's token endpoint: each code signs in as the claims it was given."""

    def __init__(self):
        self.codes = {}

    def __call__(self, method, url, headers, body):
        claims = self.codes.get(dict(urllib.parse.parse_qsl(body.decode())).get("code"))
        if not claims:
            return 400, {"error": "invalid_grant"}
        enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
        return 200, {"id_token": f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"}

    def code(self, sub, email):
        code = f"g-{uuid.uuid4().hex[:8]}"
        self.codes[code] = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": sub, "email": email,
                            "email_verified": True, "exp": NOW + 10 * 86400 * 365}
        return code


class Docs:
    """Client metadata documents, served without the network."""

    def __init__(self):
        self.docs = {k: dict(v) for k, v in DOCS.items()}
        self.cache_control = "public, max-age=300"
        self.fetched = []

    def __call__(self, url, local):
        self.fetched.append(url)
        if url not in self.docs:
            return 404, "", b"Not Found"
        return 200, self.cache_control, json.dumps(self.docs[url]).encode()


@pytest.fixture
def google():
    return Google()


@pytest.fixture
def docs():
    return Docs()


@pytest.fixture
def client(env, clock, google, docs):
    application = likelyread.create_app()
    application.state.cloud.http = google
    application.state.cloud.oauth.fetch = docs
    with TestClient(application, base_url="https://testserver", follow_redirects=False) as c:
        yield c


def pkce():
    verifier = secrets.token_urlsafe(48)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def query(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def start(c, client_id=CLAUDE, redirect_uri=CLAUDE_CB, challenge=None, **over):
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
              "code_challenge": challenge or pkce()[1], "code_challenge_method": "S256", "state": "st-123",
              "resource": MCP, "scope": "offline_access"}
    params.update(over)
    return c.get("/oauth/authorize", params={k: v for k, v in params.items() if v is not None})


def sign_in(c, google, r, sub="g-ann", email="ann@example.com"):
    """From the authorize redirect, through Google, to the consent page."""
    assert r.status_code == 302 and r.headers["location"].startswith("https://accounts.google.com/"), r.text
    state = query(r.headers["location"])["state"]
    r = c.get(f"/auth/google/callback?code={google.code(sub, email)}&state={state}")
    assert r.status_code == 303 and r.headers["location"] == f"{BASE}/oauth/consent"
    page = c.get("/oauth/consent")
    assert page.status_code == 200
    return page


def answer(c, page, action="approve"):
    nonce = re.search(r'name="nonce" value="([^"]+)"', page.text).group(1)
    r = c.post("/oauth/consent", data={"nonce": nonce, "action": action})
    assert r.status_code == 303
    return r.headers["location"]


def connect(c, google, client_id=CLAUDE, redirect_uri=CLAUDE_CB, sub="g-ann", email="ann@example.com"):
    """The whole flow, as Claude runs it: returns the token response."""
    verifier, challenge = pkce()
    back = answer(c, sign_in(c, google, start(c, client_id, redirect_uri, challenge), sub, email))
    r = exchange(c, query(back)["code"], verifier, client_id, redirect_uri)
    assert r.status_code == 200, r.text
    return r.json()


def exchange(c, code, verifier, client_id=CLAUDE, redirect_uri=CLAUDE_CB, **over):
    form = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier, "client_id": client_id,
            "redirect_uri": redirect_uri, "resource": MCP}
    form.update(over)
    return c.post("/oauth/token", data={k: v for k, v in form.items() if v is not None})


def refresh(c, token, client_id=CLAUDE):
    return c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": token, "client_id": client_id,
                                        "resource": MCP})


def tools(c, token):
    return c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                  headers={**JSONRPC, "Authorization": f"Bearer {token}"})


def H(token):
    return {"Authorization": f"Bearer {token}"}


def dashboard(c, sub="g-ann", email="ann@example.com"):
    acct = c.app.state.cloud.upsert_google_account(sub, email)
    return c.app.state.cloud.create_token(acct, "Dashboard")


# ---- discovery ---------------------------------------------------------------


def test_metadata_points_apps_at_this_server(client):
    prm = client.get("/.well-known/oauth-protected-resource/mcp")
    assert prm.status_code == 200 and prm.headers["access-control-allow-origin"] == "*"
    assert client.get("/.well-known/oauth-protected-resource").json() == prm.json()
    meta = prm.json()
    assert meta["resource"] == MCP and meta["authorization_servers"] == [BASE], "the exact strings Claude and ChatGPT compare"
    assert meta["bearer_methods_supported"] == ["header"] and "scopes_supported" not in meta

    asm = client.get("/.well-known/oauth-authorization-server").json()
    assert asm["issuer"] == BASE
    assert (asm["authorization_endpoint"], asm["token_endpoint"], asm["revocation_endpoint"]) == (
        f"{BASE}/oauth/authorize", f"{BASE}/oauth/token", f"{BASE}/oauth/revoke")
    assert asm["code_challenge_methods_supported"] == ["S256"]
    # Claude picks CIMD only with both of these; ChatGPT uses its stable client with the third.
    assert asm["client_id_metadata_document_supported"] is True
    assert asm["token_endpoint_auth_methods_supported"] == ["none"]
    assert asm["authorization_response_iss_parameter_supported"] is True
    assert "registration_endpoint" not in asm and "scopes_supported" not in asm


def test_mcp_401_names_the_metadata(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=JSONRPC)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == f'Bearer resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"'


def test_selfhost_has_no_oauth(tmp_path, monkeypatch):
    monkeypatch.delenv("MODE", raising=False)
    monkeypatch.setenv("LIKELYREAD_TOKEN", "t" * 32)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "s.db"))
    c = TestClient(likelyread.create_app())
    for method, path in [("get", "/oauth/authorize"), ("post", "/oauth/token"), ("get", "/oauth/consent"),
                         ("get", "/.well-known/oauth-authorization-server")]:
        assert getattr(c, method)(path).status_code in (404, 405), path


# ---- the flow, for each app ------------------------------------------------------


def test_claude_connects_and_its_token_works_at_mcp_only(client, google):
    verifier, challenge = pkce()
    page = sign_in(client, google, start(client, challenge=challenge))
    assert "Connect Claude" in page.text and "<strong>claude.ai</strong>" in page.text
    assert "ann@example.com" in page.text and "go back to claude.ai" in page.text
    assert "runs on this computer" not in page.text
    csp = page.headers["content-security-policy"]
    assert "form-action 'self' https://claude.ai;" in csp and "frame-ancestors 'none'" in csp
    assert page.headers["x-frame-options"] == "DENY" and page.headers["cache-control"] == "no-store"

    back = answer(client, page)
    assert back.startswith(CLAUDE_CB + "?")
    params = query(back)
    assert params["state"] == "st-123" and params["iss"] == BASE, "RFC 9207: iss on every answer"
    assert client.get("/oauth/consent").status_code == 400, "the consent cookie is gone"

    r = exchange(client, params["code"], verifier)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    tokens = r.json()
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == 3600 and tokens["refresh_token"]
    assert "scope" not in tokens

    listed = tools(client, tokens["access_token"])
    assert listed.status_code == 200 and len(listed.json()["result"]["tools"]) == 11
    acct = client.post("/mcp", headers={**JSONRPC, **H(tokens["access_token"])}, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "account", "arguments": {}}})
    assert json.loads(acct.json()["result"]["content"][0]["text"])["email"] == "ann@example.com"
    # Issued for /mcp: the API refuses it, account actions included.
    for path in ("/api/emails", "/api/me", "/api/me/apps", "/api/me/export"):
        assert client.get(path, headers=H(tokens["access_token"])).status_code == 401, path

    rows = client.app.state.db.query("SELECT * FROM apps")
    assert len(rows) == 1 and rows[0]["name"] == "Claude" and rows[0]["client_id"] == CLAUDE
    assert tokens["access_token"] not in dict(rows[0]).values() and tokens["refresh_token"] not in dict(rows[0]).values()
    assert client.app.state.db.query("SELECT COUNT(*) AS n FROM tokens")[0]["n"] == 0, "no dashboard token was made"


def test_chatgpt_connects_as_a_public_client(client, google):
    tokens = connect(client, google, CHATGPT, CHATGPT_CB)
    assert tools(client, tokens["access_token"]).status_code == 200
    assert client.app.state.db.query("SELECT name FROM apps")[0]["name"] == "ChatGPT"


@pytest.mark.parametrize("redirect", ["http://127.0.0.1:33418/callback", "http://localhost:5173/callback"])
def test_claude_code_comes_back_on_any_local_port(client, google, redirect):
    verifier, challenge = pkce()
    page = sign_in(client, google, start(client, CLAUDE_CODE, redirect, challenge))
    assert "Connect Claude Code" in page.text and "runs on this computer" in page.text
    assert f"the app on this computer ({urllib.parse.urlsplit(redirect).netloc})" in page.text
    assert f"form-action 'self' {redirect.rsplit('/', 1)[0]};" in page.headers["content-security-policy"]
    back = answer(client, page)
    assert back.startswith(redirect + "?")
    assert exchange(client, query(back)["code"], verifier, CLAUDE_CODE, redirect).status_code == 200


def test_the_app_name_is_escaped(client, google, docs):
    docs.docs[CLAUDE]["client_name"] = '<script>alert(1)</script> "Claude"'
    page = sign_in(client, google, start(client))
    assert "<script>alert" not in page.text and "&lt;script&gt;alert(1)&lt;/script&gt; &quot;Claude&quot;" in page.text


def test_cancel_and_failed_sign_in_go_back_with_access_denied(client, google):
    back = answer(client, sign_in(client, google, start(client)), action="deny")
    assert query(back) == {"error": "access_denied", "error_description": "The user did not approve the connection.",
                           "state": "st-123", "iss": BASE}
    assert client.app.state.db.query("SELECT COUNT(*) AS n FROM app_codes")[0]["n"] == 0

    r = start(client)
    state = query(r.headers["location"])["state"]
    r = client.get(f"/auth/google/callback?error=access_denied&state={state}")
    assert r.status_code == 303 and r.headers["location"].startswith(CLAUDE_CB + "?")
    assert query(r.headers["location"])["error"] == "access_denied" and query(r.headers["location"])["iss"] == BASE


def test_consent_needs_its_cookie_and_nonce(client, google):
    page = sign_in(client, google, start(client))
    assert client.post("/oauth/consent", data={"nonce": "forged", "action": "approve"}).status_code == 400
    nonce = re.search(r'name="nonce" value="([^"]+)"', page.text).group(1)
    assert client.post("/oauth/consent", json={"nonce": nonce, "action": "approve"}).status_code == 400, "form-encoded only"
    client.cookies.clear()
    assert client.post("/oauth/consent", data={"nonce": nonce, "action": "approve"}).status_code == 400
    assert client.get("/oauth/consent").status_code == 400
    assert client.app.state.db.query("SELECT COUNT(*) AS n FROM app_codes")[0]["n"] == 0


# ---- what the authorization endpoint refuses ------------------------------------------


@pytest.mark.parametrize("client_id, redirect", [
    ("https://unknown.example/client.json", CLAUDE_CB),  # the document answers 404
    ("http://claude.ai/oauth/mcp-oauth-client-metadata", CLAUDE_CB),
    ("https://203.0.113.9/client.json", CLAUDE_CB),
    ("https://claude.ai", CLAUDE_CB),
    ("https://claude.ai:8443/oauth/mcp-oauth-client-metadata", CLAUDE_CB),
    ("not a url", CLAUDE_CB),
    (CLAUDE, "https://evil.example/callback"),
    (CLAUDE, CLAUDE_CB + "/extra"),
    (CLAUDE, None),
    (CLAUDE_CODE, "http://127.0.0.1:33418/other"),
    (CLAUDE_CODE, "http://evil.example:33418/callback"),
])
def test_unchecked_apps_get_a_page_never_a_redirect(client, client_id, redirect):
    r = start(client, client_id, redirect)
    assert r.status_code == 400 and "location" not in r.headers, (client_id, redirect)
    assert "This app cannot connect" in r.text


def test_a_document_must_name_itself(client, docs):
    docs.docs[CLAUDE]["client_id"] = "https://claude.ai/someone-else"
    assert "names another client_id" in start(client).text
    docs.docs[CHATGPT]["redirect_uris"] = ["chatgpt://callback", "http://chatgpt.com/cb"]
    assert "lists no https or loopback redirect URI" in start(client, CHATGPT, CHATGPT_CB).text


@pytest.mark.parametrize("over, error", [
    ({"code_challenge": None}, "invalid_request"),
    ({"code_challenge_method": "plain"}, "invalid_request"),
    ({"code_challenge": "short"}, "invalid_request"),
    ({"response_type": "token"}, "unsupported_response_type"),
    ({"resource": "https://evil.example/mcp"}, "invalid_target"),
    ({"state": "x" * oauth.MAX_REQUEST}, "invalid_request"),
])
def test_checked_apps_get_errors_on_their_redirect(client, over, error):
    r = start(client, **over)
    assert r.status_code == 303 and r.headers["location"].startswith(CLAUDE_CB + "?")
    got = query(r.headers["location"])
    assert got["error"] == error and got["iss"] == BASE and got["state"] == over.get("state", "st-123")


def test_the_longest_request_still_fits_in_the_sign_in_cookies(client, google):
    """Browsers drop a cookie over 4 KB, which would lose the sign-in without a word."""
    long_name = "é" * 80  # escaped in JSON: six bytes each
    client.app.state.cloud.oauth.docs.clear()
    docs = client.app.state.cloud.oauth.fetch
    docs.docs[CLAUDE]["client_name"] = long_name
    r = start(client, state="s" * (oauth.MAX_REQUEST - 800))  # the name's 480 bytes and the rest fit in the other 800
    assert r.status_code == 302, "under the limit"
    assert len(r.headers["set-cookie"].split(";")[0]) < 4000
    state = query(r.headers["location"])["state"]
    r = client.get(f"/auth/google/callback?code={google.code('g-' + 'x' * 200, 'a' * 64 + '@' + 'b' * 250 + '.com')}&state={state}")
    assert len(r.headers["set-cookie"].split(";")[0]) < 4000
    assert client.get("/oauth/consent").status_code == 200


def test_resource_may_be_the_server_or_absent(client, google):
    for resource in (BASE, "HTTPS://APP.likelyread.com/mcp/", None):
        assert start(client, resource=resource).status_code == 302, resource


def test_repeated_parameters_are_refused(client):
    r = client.get(f"/oauth/authorize?client_id={CLAUDE}&client_id=https://evil.example/c.json&redirect_uri={CLAUDE_CB}")
    assert r.status_code == 400 and "repeats a parameter" in r.text


def test_documents_are_cached_and_fetches_limited(client, docs, clock):
    start(client)
    start(client)
    assert docs.fetched == [CLAUDE], "cached for its max-age"
    clock["now"] += 301
    start(client)
    assert docs.fetched == [CLAUDE, CLAUDE]
    for i in range(oauth.DOC_FETCHES_PER_MIN):
        start(client, f"https://unknown{i}.example/client.json")
    assert "too many sign-ins" in start(client, "https://another.example/client.json").text


# ---- the token endpoint ---------------------------------------------------------


def test_a_code_works_once_for_its_app_its_redirect_and_its_verifier(client, google):
    verifier, challenge = pkce()
    code = query(answer(client, sign_in(client, google, start(client, challenge=challenge))))["code"]
    for over in ({"code_verifier": pkce()[0]}, {"client_id": CHATGPT}, {"redirect_uri": CHATGPT_CB},
                 {"code_verifier": "short"}):
        r = exchange(client, code, verifier, **over)
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant", over
    assert exchange(client, code, verifier, resource="https://evil.example/mcp").json()["error"] == "invalid_target"
    assert exchange(client, code, verifier).status_code == 200, "a failed attempt does not spend the code"
    assert exchange(client, code, verifier).json()["error"] == "invalid_grant", "spent"


def test_a_code_expires_after_five_minutes(client, google, clock):
    verifier, challenge = pkce()
    code = query(answer(client, sign_in(client, google, start(client, challenge=challenge))))["code"]
    clock["now"] += oauth.CODE_SECONDS + 1
    assert exchange(client, code, verifier).json()["error"] == "invalid_grant"


def test_token_requests_must_be_well_formed(client):
    assert client.post("/oauth/token", json={"grant_type": "authorization_code"}).json()["error"] == "invalid_request"
    assert client.post("/oauth/token", data={"grant_type": "password"}).json()["error"] == "unsupported_grant_type"
    assert client.post("/oauth/token", data={"grant_type": "authorization_code"}).json()["error"] == "invalid_request"
    assert client.post("/oauth/token", data={"grant_type": "refresh_token"}).json()["error"] == "invalid_request"
    twice = "grant_type=refresh_token&refresh_token=a&refresh_token=b&client_id=x"
    r = client.post("/oauth/token", content=twice, headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.json()["error"] == "invalid_request"
    pre = client.options("/oauth/token")
    assert pre.status_code == 204 and pre.headers["access-control-allow-origin"] == "*"


def test_refresh_rotates_and_keeps_the_last_spent_token_for_a_lost_answer(client, google):
    first = connect(client, google)
    second = refresh(client, first["refresh_token"]).json()
    assert tools(client, second["access_token"]).status_code == 200
    stale = tools(client, first["access_token"])
    assert stale.status_code == 401 and 'error="invalid_token"' in stale.headers["www-authenticate"]
    # The answer was lost: the app sends the spent token again and still gets a new pair...
    third = refresh(client, first["refresh_token"]).json()
    assert third["access_token"] and third["refresh_token"] not in (first["refresh_token"], second["refresh_token"])
    # ...which retires the pair it never received.
    assert refresh(client, second["refresh_token"]).json()["error"] == "invalid_grant"
    fourth = refresh(client, third["refresh_token"]).json()
    assert refresh(client, first["refresh_token"]).json()["error"] == "invalid_grant", "using the new one retires it"
    assert refresh(client, fourth["refresh_token"], client_id=CHATGPT).json()["error"] == "invalid_grant"
    assert tools(client, fourth["access_token"]).status_code == 200
    assert client.app.state.db.query("SELECT COUNT(*) AS n FROM apps")[0]["n"] == 1


def test_access_lasts_an_hour_and_a_connection_180_days_unused(client, google, clock):
    tokens = connect(client, google)
    clock["now"] += cloud.APP_ACCESS_SECONDS + 1
    r = tools(client, tokens["access_token"])
    assert r.status_code == 401 and 'error="invalid_token"' in r.headers["www-authenticate"]
    tokens = refresh(client, tokens["refresh_token"]).json()
    assert tools(client, tokens["access_token"]).status_code == 200
    clock["now"] += cloud.TOKEN_IDLE_SECONDS - 3600
    tokens = refresh(client, tokens["refresh_token"]).json()
    assert tools(client, tokens["access_token"]).status_code == 200, "each refresh counts as use"
    clock["now"] += cloud.TOKEN_IDLE_SECONDS + 1
    assert refresh(client, tokens["refresh_token"]).json()["error"] == "invalid_grant"
    assert client.app.state.db.query("SELECT COUNT(*) AS n FROM apps")[0]["n"] == 0


def test_revocation_ends_the_connection(client, google):
    tokens = connect(client, google)
    assert client.post("/oauth/revoke", data={"token": tokens["refresh_token"], "client_id": CHATGPT}).status_code == 200
    assert tools(client, tokens["access_token"]).status_code == 200, "another app's client_id revokes nothing"
    assert client.post("/oauth/revoke", data={"token": "unknown"}).status_code == 200
    assert client.post("/oauth/revoke", data={"token": tokens["refresh_token"], "client_id": CLAUDE}).status_code == 200
    assert tools(client, tokens["access_token"]).status_code == 401
    assert refresh(client, tokens["refresh_token"]).json()["error"] == "invalid_grant"


# ---- connections and the account ------------------------------------------------------


def test_connections_are_a_pool_of_their_own(client, google, clock):
    dash = dashboard(client)
    agent = client.post("/api/me/agent-token", headers=H(dash)).json()["token"]
    first = connect(client, google)
    for _ in range(cloud.MAX_APPS):
        clock["now"] += 1
        connect(client, google, CHATGPT, CHATGPT_CB)
    assert tools(client, first["access_token"]).status_code == 401, "the eleventh replaced the least recently used"
    assert client.app.state.db.query("SELECT COUNT(*) AS n FROM apps")[0]["n"] == cloud.MAX_APPS
    assert client.get("/api/me", headers=H(agent)).status_code == 200, "never an agent token"
    assert client.get("/api/me", headers=H(dash)).json()["apps"] == cloud.MAX_APPS


def test_accounts_list_and_disconnect_their_apps(client, google):
    tokens = connect(client, google)
    dash = dashboard(client)
    apps = client.get("/api/me/apps", headers=H(dash)).json()
    assert [(a["name"], a["site"]) for a in apps] == [("Claude", "claude.ai")]
    agent = client.post("/api/me/agent-token", headers=H(dash)).json()["token"]
    assert client.get("/api/me/apps", headers=H(agent)).status_code == 403
    assert client.delete(f"/api/me/apps/{apps[0]['id']}", headers=H(agent)).status_code == 403
    bob = dashboard(client, "g-bob", "bob@example.com")
    assert client.delete(f"/api/me/apps/{apps[0]['id']}", headers=H(bob)).status_code == 404, "someone else's"
    assert client.delete(f"/api/me/apps/{apps[0]['id']}", headers=H(dash)).json() == {"ok": True}
    assert tools(client, tokens["access_token"]).status_code == 401
    assert client.get("/api/me/apps", headers=H(dash)).json() == []


def test_sign_out_everywhere_and_deletion_end_connections(client, google):
    tokens = connect(client, google)
    dash = dashboard(client)
    assert client.post("/api/me/signout-all", headers=H(dash)).json()["revoked"] == 2, "the dashboard token and the app"
    assert tools(client, tokens["access_token"]).status_code == 401

    tokens = connect(client, google)
    verifier, challenge = pkce()
    answer(client, sign_in(client, google, start(client, challenge=challenge)))  # a code nobody redeemed
    dash = dashboard(client)
    assert client.delete("/api/me", headers=H(dash)).json() == {"ok": True}
    for table in ("apps", "app_codes"):
        assert client.app.state.db.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] == 0, table


def test_daily_purge_forgets_lapsed_connections_and_codes(client, google, clock):
    connect(client, google)
    answer(client, sign_in(client, google, start(client)))
    clock["now"] += cloud.TOKEN_IDLE_SECONDS + 1
    client.app.state.cloud.purge()
    for table in ("apps", "app_codes"):
        assert client.app.state.db.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] == 0, table


def test_sign_ins_from_the_extension_and_dashboard_still_work(client, google):
    """The app flow shares Google's callback with the dashboard's sign-in."""
    r = client.get("/auth/google")
    state = query(r.headers["location"])["state"]
    r = client.get(f"/auth/google/callback?code={google.code('g-ann', 'ann@example.com')}&state={state}")
    assert r.status_code == 303 and "#connect=" in r.headers["location"]


# ---- the client_id, redirect URI and document fetch rules --------------------------------


@pytest.mark.parametrize("client_id, ok", [
    (CLAUDE, True), (CHATGPT, True), ("https://app.example.com/oauth/client.json?v=2", True),
    ("http://app.example.com/client.json", False), ("https://app.example.com", False),
    ("https://app.example.com/", False), ("https://app.example.com/a/../b", False),
    ("https://user:pw@app.example.com/c.json", False), ("https://app.example.com/c.json#x", False),
    ("https://localhost/c.json", False), ("https://10.0.0.1/c.json", False), ("https://[::1]/c.json", False),
    ("https://app.example.com:444/c.json", False), ("https://intranet/c.json", False),
    ("http://127.0.0.1:9000/c.json", False),
])
def test_client_id_rules(client_id, ok):
    if ok:
        oauth.check_client_id(client_id)
    else:
        with pytest.raises(oauth.ClientError):
            oauth.check_client_id(client_id)


def test_a_local_server_takes_local_documents():
    oauth.check_client_id("http://127.0.0.1:9000/c.json", local=True)
    oauth.check_client_id("http://localhost:9000/c.json", local=True)
    with pytest.raises(oauth.ClientError):
        oauth.check_client_id("http://app.example.com/c.json", local=True)


def test_redirect_rules():
    code = DOCS[CLAUDE_CODE]["redirect_uris"]
    assert oauth.redirect_matches("http://127.0.0.1:61234/callback", code)
    assert oauth.redirect_matches("http://localhost/callback", code)
    assert not oauth.redirect_matches("http://127.0.0.1:61234/callback?x=1", code)
    assert not oauth.redirect_matches("http://[::1]:61234/callback", code), "only the hosts the document lists"
    assert oauth.redirect_matches("http://[::1]:61234/callback", ["http://[::1]/callback"])
    assert not oauth.redirect_matches("http://127.0.0.1:99999/callback", code)
    assert not oauth.redirect_matches("https://claude.ai/api/mcp/auth_callback/", [CLAUDE_CB])
    assert not oauth.redirect_allowed("http://claude.ai/callback")
    assert not oauth.redirect_allowed("myapp://callback")
    # The origin goes into the consent page's CSP: only plain host names get there.
    assert not oauth.redirect_allowed("https://claude.ai;script-src */cb")
    assert oauth.redirect_allowed("https://claude.ai/cb;x") and oauth.origin_of("https://claude.ai/cb;x") == "https://claude.ai"
    assert oauth.origin_of("http://[::1]:5000/cb") == "http://[::1]:5000"


def fake_dns(monkeypatch, addr):
    family = socket.AF_INET6 if ":" in addr else socket.AF_INET
    monkeypatch.setattr(oauth.socket, "getaddrinfo",
                        lambda host, port, **kw: [(family, socket.SOCK_STREAM, 6, "", (addr, port))])


@pytest.mark.parametrize("addr", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.1.1", "::1", "fd00::1",
                                  "100.64.0.1", "0.0.0.0"])
def test_documents_are_never_fetched_from_private_addresses(monkeypatch, addr):
    fake_dns(monkeypatch, addr)
    monkeypatch.setattr(oauth.socket, "create_connection", lambda *a, **kw: pytest.fail("connected"))
    with pytest.raises(oauth.ClientError, match="not on the public internet"):
        oauth.fetch_document("https://rebind.example/client.json")


def test_documents_are_fetched_from_the_address_that_was_checked(monkeypatch):
    fake_dns(monkeypatch, "93.184.215.14")
    seen = []

    def connect(address, timeout):
        seen.append(address)
        raise OSError("refused")

    monkeypatch.setattr(oauth.socket, "create_connection", connect)
    with pytest.raises(oauth.ClientError, match="could not be fetched"):
        oauth.fetch_document("https://app.example.com/client.json")
    assert seen == [("93.184.215.14", 443)], "no second DNS lookup to rebind"


def test_the_mcp_sdk_client_signs_in_like_claude_code(tmp_path, monkeypatch, google, docs):
    """The MCP SDK's own OAuth client against a live server: discovery from the 401, the
    client metadata document instead of registration, PKCE, the resource and iss checks,
    then the tools. Only Google, the document fetch and the user's clicks are simulated."""
    import asyncio
    import threading
    import time

    import httpx2
    import uvicorn
    from mcp import Client
    from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client
    from mcp.shared.auth import OAuthClientMetadata

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    for k, v in {"MODE": "cloud", "DB_PATH": str(tmp_path / "live.db"), "PUBLIC_URL": base, "SESSION_SECRET": "s" * 40,
                 "GOOGLE_CLIENT_ID": CLIENT_ID, "GOOGLE_CLIENT_SECRET": "gsecret", "DASHBOARD": "false"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("LIKELYREAD_TOKEN", raising=False)
    application = likelyread.create_app()
    application.state.cloud.http = google
    application.state.cloud.oauth.fetch = docs
    server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)

    answer: dict[str, str] = {}

    class Memory:
        tokens = None
        client = None

        async def get_tokens(self):
            return self.tokens

        async def set_tokens(self, tokens):
            self.tokens = tokens

        async def get_client_info(self):
            return self.client

        async def set_client_info(self, info):
            self.client = info

    async def browser(url):
        """The user: picks the Google account, then clicks Approve."""
        async with httpx2.AsyncClient(follow_redirects=False) as b:
            r = await b.get(url)
            state = query(str(r.headers["location"]))["state"]
            await b.get(f"{base}/auth/google/callback", params={"code": google.code("g-ann", "ann@example.com"), "state": state})
            page = await b.get(f"{base}/oauth/consent")
            nonce = re.search(r'name="nonce" value="([^"]+)"', page.text).group(1)
            r = await b.post(f"{base}/oauth/consent", data={"nonce": nonce, "action": "approve"})
            answer.update(query(str(r.headers["location"])))

    async def callback():
        return AuthorizationCodeResult(code=answer["code"], state=answer["state"], iss=answer.get("iss"))

    storage = Memory()
    auth = OAuthClientProvider(
        server_url=f"{base}/mcp",
        client_metadata=OAuthClientMetadata(redirect_uris=["http://127.0.0.1:33418/callback"], client_name="Claude Code",
                                            token_endpoint_auth_method="none"),
        storage=storage, redirect_handler=browser, callback_handler=callback, client_metadata_url=CLAUDE_CODE,
    )

    async def session():
        async with Client(streamable_http_client(f"{base}/mcp", http_client=create_mcp_http_client(auth=auth))) as client:
            return {t.name for t in (await client.list_tools()).tools}

    try:
        names = asyncio.run(session())
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert len(names) == 11 and "track_email" in names
    assert answer["iss"] == base and storage.client.client_id == CLAUDE_CODE, "no registration: the document's URL"
    assert storage.tokens.refresh_token and storage.tokens.expires_in == 3600


def test_document_cache_lifetimes():
    assert oauth.cache_seconds("public, max-age=300") == 300
    assert oauth.cache_seconds("max-age=5") == oauth.DOC_CACHE_MIN
    assert oauth.cache_seconds("max-age=99999999") == oauth.DOC_CACHE_MAX
    assert oauth.cache_seconds("") == oauth.DOC_CACHE_DEFAULT
    assert oauth.cache_seconds("no-store") == 0
