"""OAuth token management for the Bosch Flow MCP server.

Two auth paths (auto-detected):
  - EUDA (preferred): If config/bosch_config.json has a client_id, uses that
    with PKCE + localhost:4200 callback. No DevTools needed. Register at
    portal.bosch-ebike.com/data-act/app to get your own euda-* client_id.
  - one-bike-app (fallback): If no config file, uses the mobile app's public
    client. Requires DevTools URL paste due to iOS deep link redirect.
"""

import base64
import hashlib
import json
import logging
import secrets
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from .config import (
    BOSCH_AUTH_URL,
    BOSCH_CONFIG_PATH,
    BOSCH_TOKEN_URL,
    BOSCH_TOKENS_PATH,
    CLIENT_ID,
    CONFIG_DIR,
    REDIRECT_URI,
    SCOPE,
)

logger = logging.getLogger(__name__)

# In-memory token cache - avoids re-reading the token file on every API call
_tokens: dict | None = None
_token_lock = threading.Lock()

# EUDA localhost callback port
EUDA_CALLBACK_PORT = 4200
EUDA_REDIRECT_URI = f"http://localhost:{EUDA_CALLBACK_PORT}"


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _save_json(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    path.chmod(0o600)


def _load_json(path) -> dict:
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Client detection
# ---------------------------------------------------------------------------


def _get_client_id() -> str:
    """Return the client_id to use for auth. EUDA config takes priority."""
    if BOSCH_CONFIG_PATH.exists():
        cfg = _load_json(BOSCH_CONFIG_PATH)
        cid = cfg.get("client_id", "")
        if cid:
            return cid
    return CLIENT_ID


def _is_euda() -> bool:
    """True if using a registered EUDA client (has config file)."""
    if BOSCH_CONFIG_PATH.exists():
        cfg = _load_json(BOSCH_CONFIG_PATH)
        return bool(cfg.get("client_id", ""))
    return False


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def _generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return verifier, challenge


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------


def refresh_token() -> str:
    """Return a valid access token, refreshing if expired (5-min buffer).

    Thread-safe. Raises RuntimeError if tokens are missing or refresh fails.
    """
    global _tokens

    with _token_lock:
        if _tokens is None:
            if not BOSCH_TOKENS_PATH.exists():
                raise RuntimeError("Bosch not configured. Run: bosch-flow-mcp auth")
            _tokens = _load_json(BOSCH_TOKENS_PATH)

        now = datetime.now(timezone.utc).timestamp()
        if now < _tokens.get("expiry", 0) - 300:
            return _tokens["access_token"]

        if not _tokens.get("refresh_token"):
            raise RuntimeError("Token expired and no refresh token. Run: bosch-flow-mcp auth")

        # Use whichever client_id was used to obtain the token
        client_id = _tokens.get("client_id", _get_client_id())

        # Public client refresh - no client_secret needed
        data = urlencode(
            {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": _tokens["refresh_token"],
            }
        ).encode()

        req = urllib.request.Request(BOSCH_TOKEN_URL, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                new_tokens = json.loads(resp.read().decode())
        except urllib.error.URLError as e:
            raise RuntimeError(f"Token refresh failed: {e}. Run: bosch-flow-mcp auth") from e

        _tokens = {
            "access_token": new_tokens["access_token"],
            "refresh_token": new_tokens.get("refresh_token", _tokens["refresh_token"]),
            "token_type": new_tokens.get("token_type", "Bearer"),
            "expiry": now + new_tokens.get("expires_in", 7200),
            "client_id": client_id,
        }
        _save_json(BOSCH_TOKENS_PATH, _tokens)
        return _tokens["access_token"]


def invalidate_token_cache() -> None:
    """Force re-read of token file on next refresh_token() call."""
    global _tokens
    with _token_lock:
        _tokens = None


# ---------------------------------------------------------------------------
# Interactive auth setup
# ---------------------------------------------------------------------------


def setup_auth() -> None:
    """Interactive OAuth setup. Auto-detects EUDA vs one-bike-app.

    If config/bosch_config.json has a client_id, uses EUDA flow with
    localhost:4200 callback (simple browser redirect). Otherwise falls back
    to one-bike-app with DevTools URL paste.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if _is_euda():
        _setup_euda_auth()
    else:
        _setup_mobile_auth()


def _setup_euda_auth() -> None:
    """EUDA auth: PKCE + localhost:4200 callback. No DevTools needed."""
    client_id = _get_client_id()
    verifier, challenge = _generate_pkce()
    state = secrets.token_urlsafe(16)

    auth_url = (
        BOSCH_AUTH_URL
        + "?"
        + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": EUDA_REDIRECT_URI,
                "response_type": "code",
                "scope": SCOPE,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
            }
        )
    )

    auth_code: list[str] = []

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = parse_qs(urlparse(self.path).query)
            if "code" in qs:
                auth_code.append(qs["code"][0])
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1>Auth complete - you can close this tab.</h1>")
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Missing code parameter")

        def log_message(self, format, *a):
            pass  # suppress request logs

    print(f"\nBosch eBike Flow auth (EUDA: {client_id[:20]}...)")
    print("=" * 50)
    print("\nOpening browser for Bosch login...")
    print(f"URL: {auth_url}\n")
    webbrowser.open(auth_url)

    print(f"Waiting for callback on localhost:{EUDA_CALLBACK_PORT}...")
    server = HTTPServer(("localhost", EUDA_CALLBACK_PORT), CallbackHandler)
    server.handle_request()

    if not auth_code:
        print("Error: no auth code received.", file=sys.stderr)
        sys.exit(1)

    _exchange_code(client_id, auth_code[0], verifier, EUDA_REDIRECT_URI)


def _setup_mobile_auth() -> None:
    """one-bike-app auth: PKCE + DevTools URL paste (iOS redirect workaround)."""
    verifier, challenge = _generate_pkce()
    state = secrets.token_urlsafe(16)
    nonce = secrets.token_urlsafe(16)

    auth_url = (
        BOSCH_AUTH_URL
        + "?"
        + urlencode(
            {
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "scope": SCOPE,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "kc_idp_hint": "skid",
                "prompt": "login",
                "nonce": nonce,
                "state": state,
            }
        )
    )

    print("\nBosch eBike Flow auth (one-bike-app)")
    print("=" * 50)
    print("\nSTEP 1: Open browser DevTools FIRST (F12), go to the Network tab.")
    print("\nSTEP 2: Open this URL (or it will open automatically):")
    print(f"\n  {auth_url}\n")
    webbrowser.open(auth_url)

    print("STEP 3: Log in with your Bosch Flow account.")
    print("\nSTEP 4: The browser will show a failed redirect to 'onebikeapp-ios://'.")
    print("        In DevTools > Network, find 'oauth2redirect' and copy the full URL.")
    print("        (Right-click > Copy URL in Chrome/Edge, or copy the Location")
    print("        response header in Firefox.)")
    print("\nSTEP 5: Paste the full redirect URL below:")
    redirect_url = input("> ").strip()

    qs = parse_qs(urlparse(redirect_url).query)
    if "code" not in qs:
        print("Error: no 'code' parameter found in the URL.", file=sys.stderr)
        sys.exit(1)

    _exchange_code(CLIENT_ID, qs["code"][0], verifier, REDIRECT_URI)


def _exchange_code(client_id: str, code: str, verifier: str, redirect_uri: str) -> None:
    """Exchange auth code for tokens and save them."""
    data = urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
        }
    ).encode()

    req = urllib.request.Request(
        BOSCH_TOKEN_URL,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            tokens = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:
            pass
        print(f"Error exchanging code: HTTP {e.code} - {body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Error exchanging code: {e}", file=sys.stderr)
        sys.exit(1)

    if "access_token" not in tokens:
        print(f"Error: unexpected token response: {tokens}", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc).timestamp()
    token_store = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", ""),
        "token_type": tokens.get("token_type", "Bearer"),
        "expiry": now + tokens.get("expires_in", 7200),
        "client_id": client_id,
    }
    _save_json(BOSCH_TOKENS_PATH, token_store)
    invalidate_token_cache()

    print("\nAuth complete. Tokens saved to config/bosch_tokens.json")
    print("\nTry a sync:")
    print("  bosch-flow-mcp sync")
