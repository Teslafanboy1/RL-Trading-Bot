"""mcp_client.py — minimal stdlib MCP streamable-HTTP client with OAuth 2.1.

Connects the dashboard DIRECTLY to Robinhood's hosted MCP endpoint
(https://agent.robinhood.com/mcp/trading — same server the bot reaches through
the claude CLI) as its own OAuth client:

  * discovery: RFC 9728 protected-resource metadata (or WWW-Authenticate hint)
    -> authorization-server metadata (RFC 8414)
  * dynamic client registration (RFC 7591), public client + PKCE (S256)
  * one-time browser approval with a localhost callback, refresh tokens after

Tokens live in dashboard/.rh_tokens.json (chmod 600, gitignored). If any step
is rejected (e.g. Robinhood pins allowed clients), callers catch NeedsLogin /
MCPError and the broker layer falls back to the claude -p driver — the
dashboard keeps working either way.

Run the interactive login:   python3 -m dashboard.rh_login
"""

import os
import sys
import json
import time
import base64
import hashlib
import secrets
import threading
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import signals  # noqa: E402 — reuse the bot's certifi-aware TLS context

MCP_URL = os.environ.get("RH_MCP_URL", "https://agent.robinhood.com/mcp/trading")
TOKENS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".rh_tokens.json")
CALLBACK_PORT = int(os.environ.get("RH_OAUTH_CALLBACK_PORT", "8791"))
PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "TradeCommand Dashboard (personal, read+trade)"


class MCPError(Exception):
    pass


class NeedsLogin(MCPError):
    """No usable token — run `python3 -m dashboard.rh_login`."""


# ---------------------------------------------------------------- http utils
def _http(url, data=None, headers=None, method=None, timeout=30):
    """Request -> (status, headers dict, body bytes). Never raises HTTPError —
    error responses are returned so callers can read OAuth/JSON-RPC bodies."""
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": "tradecommand/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=signals._ssl_context()) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


def _post_form(url, fields, timeout=30):
    body = urllib.parse.urlencode(fields).encode()
    return _http(url, data=body, method="POST",
                 headers={"Content-Type": "application/x-www-form-urlencoded",
                          "Accept": "application/json"}, timeout=timeout)


def _origin(url):
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}"


# ---------------------------------------------------------------- token store
def load_tokens():
    try:
        with open(TOKENS_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def save_tokens(tok):
    with open(TOKENS_PATH, "w") as f:
        json.dump(tok, f, indent=2)
    os.chmod(TOKENS_PATH, 0o600)


def clear_tokens():
    try:
        os.remove(TOKENS_PATH)
    except OSError:
        pass


# ---------------------------------------------------------------- discovery
def discover(mcp_url=MCP_URL):
    """Resolve OAuth endpoints for the MCP resource. Returns
    {resource, auth_meta:{authorization_endpoint, token_endpoint,
     registration_endpoint?}}"""
    # 1) ask the resource: an unauthenticated POST should 401 with a
    #    WWW-Authenticate: Bearer resource_metadata="..." hint (RFC 9728).
    rm_url = None
    status, headers, _ = _http(mcp_url, data=b"{}", method="POST",
                               headers={"Content-Type": "application/json",
                                        "Accept": "application/json, text/event-stream"})
    www = headers.get("WWW-Authenticate") or headers.get("www-authenticate") or ""
    m = None
    if www:
        import re
        m = re.search(r'resource_metadata="([^"]+)"', www)
    if m:
        rm_url = m.group(1)
    candidates = []
    if rm_url:
        candidates.append(rm_url)
    p = urllib.parse.urlparse(mcp_url)
    candidates.append(f"{_origin(mcp_url)}/.well-known/oauth-protected-resource{p.path}")
    candidates.append(f"{_origin(mcp_url)}/.well-known/oauth-protected-resource")
    auth_servers = []
    for c in candidates:
        st, _, body = _http(c, method="GET", headers={"Accept": "application/json"})
        if st == 200:
            try:
                meta = json.loads(body)
                auth_servers = meta.get("authorization_servers") or []
                if auth_servers:
                    break
            except Exception:
                continue
    if not auth_servers:
        auth_servers = [_origin(mcp_url)]  # last resort: AS == resource origin
    # 2) authorization-server metadata
    last_err = None
    for asrv in auth_servers:
        asrv = asrv.rstrip("/")
        ap = urllib.parse.urlparse(asrv)
        paths = [f"{ap.scheme}://{ap.netloc}/.well-known/oauth-authorization-server{ap.path}",
                 f"{asrv}/.well-known/oauth-authorization-server",
                 f"{ap.scheme}://{ap.netloc}/.well-known/openid-configuration{ap.path}",
                 f"{asrv}/.well-known/openid-configuration"]
        for u in paths:
            st, _, body = _http(u, method="GET", headers={"Accept": "application/json"})
            if st == 200:
                try:
                    meta = json.loads(body)
                except Exception as e:
                    last_err = e
                    continue
                if meta.get("authorization_endpoint") and meta.get("token_endpoint"):
                    return {"resource": mcp_url, "auth_meta": meta}
    raise MCPError(f"OAuth discovery failed for {mcp_url} ({last_err})")


def register_client(auth_meta, redirect_uri):
    """RFC 7591 dynamic registration as a public (PKCE) client."""
    reg = auth_meta.get("registration_endpoint")
    if not reg:
        raise MCPError("authorization server exposes no registration_endpoint")
    payload = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    st, _, body = _http(reg, data=json.dumps(payload).encode(), method="POST",
                        headers={"Content-Type": "application/json",
                                 "Accept": "application/json"})
    if st not in (200, 201):
        raise MCPError(f"client registration rejected (HTTP {st}): {body[:300]!r}")
    j = json.loads(body)
    if not j.get("client_id"):
        raise MCPError(f"registration response missing client_id: {j}")
    return j


# ---------------------------------------------------------------- login flow
class _CallbackHandler(BaseHTTPRequestHandler):
    result = {}

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.result = {k: v[0] for k, v in q.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        ok = "code" in _CallbackHandler.result
        self.wfile.write((
            "<html><body style='font-family:menlo;background:#0b0e14;color:#d7dce2;"
            "display:flex;align-items:center;justify-content:center;height:95vh'>"
            f"<h2>{'✓ Robinhood connected — you can close this tab.' if ok else '✗ Authorization failed: ' + str(_CallbackHandler.result)[:200]}</h2>"
            "</body></html>").encode())

    def log_message(self, *a):
        pass


def login(mcp_url=MCP_URL, open_browser=True, timeout=300):
    """Interactive OAuth login. Blocks until the browser round-trip completes.
    Saves tokens and returns them."""
    redirect_uri = f"http://127.0.0.1:{CALLBACK_PORT}/callback"
    disc = discover(mcp_url)
    meta = disc["auth_meta"]
    reg = register_client(meta, redirect_uri)
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    params = {
        "response_type": "code",
        "client_id": reg["client_id"],
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": mcp_url,
    }
    scopes = meta.get("scopes_supported")
    if scopes:
        params["scope"] = " ".join(scopes)
    auth_url = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)

    _CallbackHandler.result = {}
    srv = HTTPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
    srv.timeout = 1
    print(f"\nOpen this URL to authorize (waiting up to {timeout}s):\n  {auth_url}\n")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(auth_url)
        except Exception:
            pass
    deadline = time.time() + timeout
    while time.time() < deadline and "code" not in _CallbackHandler.result \
            and "error" not in _CallbackHandler.result:
        srv.handle_request()
    srv.server_close()
    res = _CallbackHandler.result
    if "code" not in res:
        raise MCPError(f"authorization did not complete: {res or 'timeout'}")
    if res.get("state") != state:
        raise MCPError("OAuth state mismatch — aborting")
    st, _, body = _post_form(meta["token_endpoint"], {
        "grant_type": "authorization_code",
        "code": res["code"],
        "redirect_uri": redirect_uri,
        "client_id": reg["client_id"],
        "code_verifier": verifier,
        "resource": mcp_url,
    })
    if st != 200:
        raise MCPError(f"token exchange failed (HTTP {st}): {body[:300]!r}")
    tok = json.loads(body)
    record = {
        "mcp_url": mcp_url,
        "client_id": reg["client_id"],
        "client_secret": reg.get("client_secret"),
        "token_endpoint": meta["token_endpoint"],
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "expires_at": time.time() + int(tok.get("expires_in") or 3600),
        "obtained_at": time.time(),
    }
    save_tokens(record)
    print("✓ Robinhood MCP authorized; tokens saved to dashboard/.rh_tokens.json")
    return record


def _refresh(tok):
    if not tok.get("refresh_token"):
        raise NeedsLogin("token expired and no refresh_token — re-run rh_login")
    fields = {"grant_type": "refresh_token",
              "refresh_token": tok["refresh_token"],
              "client_id": tok["client_id"],
              "resource": tok.get("mcp_url", MCP_URL)}
    if tok.get("client_secret"):
        fields["client_secret"] = tok["client_secret"]
    st, _, body = _post_form(tok["token_endpoint"], fields)
    if st != 200:
        raise NeedsLogin(f"token refresh rejected (HTTP {st}): {body[:200]!r}")
    j = json.loads(body)
    tok["access_token"] = j["access_token"]
    if j.get("refresh_token"):
        tok["refresh_token"] = j["refresh_token"]
    tok["expires_at"] = time.time() + int(j.get("expires_in") or 3600)
    save_tokens(tok)
    return tok


def ensure_token():
    tok = load_tokens()
    if not tok or not tok.get("access_token"):
        raise NeedsLogin("no saved Robinhood MCP token — run `python3 -m dashboard.rh_login`")
    if time.time() > float(tok.get("expires_at", 0)) - 60:
        tok = _refresh(tok)
    return tok


# ---------------------------------------------------------------- MCP session
def _parse_body(headers, body):
    """Streamable-HTTP responses are application/json OR an SSE stream whose
    `data:` lines carry JSON-RPC messages. Return the LAST JSON-RPC message."""
    ctype = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
    text = body.decode("utf-8", "replace")
    if "text/event-stream" in ctype:
        msgs = []
        for chunk in text.split("\n\n"):
            data_lines = [ln[5:].lstrip() for ln in chunk.splitlines()
                          if ln.startswith("data:")]
            if not data_lines:
                continue
            try:
                msgs.append(json.loads("\n".join(data_lines)))
            except Exception:
                continue
        for m in reversed(msgs):
            if isinstance(m, dict) and ("result" in m or "error" in m):
                return m
        return msgs[-1] if msgs else None
    try:
        return json.loads(text)
    except Exception:
        return None


class MCPSession:
    """One initialized MCP session over streamable HTTP. Thread-safe calls."""

    def __init__(self, mcp_url=MCP_URL):
        self.url = mcp_url
        self.session_id = None
        self._id = 0
        self._lock = threading.Lock()
        self._initialized = False

    def _headers(self):
        tok = ensure_token()
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "Authorization": f"Bearer {tok['access_token']}",
             "MCP-Protocol-Version": PROTOCOL_VERSION}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _rpc(self, method, params=None, *, notification=False, timeout=60):
        with self._lock:
            self._id += 1
            payload = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                payload["params"] = params
            if not notification:
                payload["id"] = self._id
        st, headers, body = _http(self.url, data=json.dumps(payload).encode(),
                                  method="POST", headers=self._headers(),
                                  timeout=timeout)
        if st == 401:
            _refresh(ensure_token())  # one retry with a fresh token
            st, headers, body = _http(self.url, data=json.dumps(payload).encode(),
                                      method="POST", headers=self._headers(),
                                      timeout=timeout)
        sid = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        if notification:
            return None
        if st == 404 and self.session_id:
            # session expired server-side — caller should re-initialize
            self._initialized = False
            raise MCPError("MCP session expired (404)")
        if st >= 400:
            raise MCPError(f"MCP HTTP {st}: {body[:300]!r}")
        msg = _parse_body(headers, body)
        if not isinstance(msg, dict):
            raise MCPError(f"unparseable MCP response: {body[:200]!r}")
        if msg.get("error"):
            raise MCPError(f"MCP error: {json.dumps(msg['error'])[:300]}")
        return msg.get("result")

    def initialize(self):
        res = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "tradecommand", "version": "1.0"},
        })
        self._rpc("notifications/initialized", {}, notification=True)
        self._initialized = True
        return res

    def call_tool(self, name, arguments, timeout=60):
        """tools/call -> parsed dict when the text content is JSON, else raw text.
        Raises MCPError on tool-level isError results."""
        if not self._initialized:
            self.initialize()
        try:
            res = self._rpc("tools/call", {"name": name, "arguments": arguments},
                            timeout=timeout)
        except MCPError as e:
            if "session expired" in str(e):
                self.initialize()
                res = self._rpc("tools/call", {"name": name, "arguments": arguments},
                                timeout=timeout)
            else:
                raise
        content = (res or {}).get("content") or []
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        joined = "\n".join(t for t in texts if t)
        if (res or {}).get("isError"):
            raise MCPError(f"tool {name} error: {joined[:400]}")
        try:
            return json.loads(joined)
        except Exception:
            return {"_raw": joined}


_session = None
_session_lock = threading.Lock()


def session():
    global _session
    with _session_lock:
        if _session is None:
            _session = MCPSession()
        return _session
