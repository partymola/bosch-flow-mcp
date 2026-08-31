"""OAuth token management for the Bosch Flow MCP server.

Two auth paths (auto-detected):
  - one-bike-app (default): the standard Bosch eBike Flow app's public client.
    Works for any account, including non-EU. Requires a DevTools URL paste due to
    the iOS deep-link redirect.
  - EUDA (optional, EU only): if config/bosch_config.json has a client_id, uses it
    with PKCE + a localhost:4200 callback. Register at
    portal.bosch-ebike.com/data-act/app to get your own euda-* client_id. This adds
    the Data-Act-only endpoints (service book, software-update history, capacity
    testers) but returns no data for accounts registered outside the EU.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import socket
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


# RFC 6749 defines the token endpoint's refusals as 400, with 401 for a bad
# client. 403 is deliberately absent: a WAF or bot-protection block returns it
# with no opinion about the grant.
_REFUSAL_CODES = frozenset({400, 401})


class TokenRefused(RuntimeError):
    """The server judged the credentials and rejected them.

    The only failure that warrants telling the user to re-authorise, which
    rewrites the token file and spends a refresh token this host owns.
    """


class RefreshNetworkError(RuntimeError):
    """The refresh request never got a verdict.

    An unreachable server, a rate limit or an unreadable response says nothing
    about whether the credentials are still good.
    """


# In-memory token cache - avoids re-reading the token file on every API call
_tokens: dict | None = None
_token_lock = threading.Lock()

# EUDA localhost callback port
EUDA_CALLBACK_PORT = 4200
EUDA_REDIRECT_URI = f"http://localhost:{EUDA_CALLBACK_PORT}"


class _CallbackServer(HTTPServer):
    """Refuse to share the port the authorisation code arrives on.

    Deliberate, and not what `HTTPServer` does by default: it asks for address
    reuse, which on Windows is what lets another process bind over this
    listener and take the code. Not asking is the fix; the exclusive-use
    option is asked for as well. Why each half is there, and what it costs, is
    in AGENTS.md. Pinned by TestTheCallbackPortIsNotShared, which drives this
    method's Windows branch on a POSIX runner.
    """

    allow_reuse_address = sys.platform != "win32"
    # SO_REUSEPORT is the POSIX-side version of the same hazard; nothing sets
    # this, and nothing should.
    allow_reuse_port = False

    def server_bind(self):
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _save_json(path, data) -> None:
    """Write a credential file owner-only from the outset.

    Writing first and chmodding after left a new token world-readable for the
    duration of the write. The mode on os.open applies only at creation, so
    an existing file keeps what it had - hence the best-effort fchmod, which
    must not be fatal: O_TRUNC has already emptied the file by then, and on a
    refresh the old token is spent.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        if hasattr(os, "fchmod"):
            try:
                os.fchmod(handle.fileno(), 0o600)
            except OSError:
                logger.warning("Could not tighten permissions on %s", path.name)
        handle.write(json.dumps(data, indent=2))


def _load_json(path) -> dict:
    """Read a credential file as a dict, or say why the credentials are unusable.

    Classified here rather than left to the caller: a file that is absent,
    unreadable or not a JSON object means there are no usable credentials,
    which is a refusal - unlike a transport failure it will not clear on its
    own, and the user does have to re-authorise.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise TokenRefused(f"{path.name} is missing or unreadable. Run: bosch-flow-mcp auth") from e
    if not isinstance(data, dict):
        raise TokenRefused(f"{path.name} is malformed. Run: bosch-flow-mcp auth")
    return data


# ---------------------------------------------------------------------------
# Client detection
# ---------------------------------------------------------------------------


def _get_client_id() -> str:
    """Return the client_id to use for auth. EUDA config takes priority."""
    if BOSCH_CONFIG_PATH.exists():
        try:
            cfg = _load_json(BOSCH_CONFIG_PATH)
        except RuntimeError:
            return CLIENT_ID
        cid = cfg.get("client_id", "")
        if cid:
            return cid
    return CLIENT_ID


def _is_euda() -> bool:
    """True if a registered EUDA client is configured (config file present).

    Used only by the interactive auth flow to pick which login to run. For routing
    sync requests, use token_is_euda() instead - it reflects the token actually in
    use, not just what config exists.
    """
    if BOSCH_CONFIG_PATH.exists():
        # Deliberately unguarded. The only caller is the interactive auth
        # command, where a malformed config should stop the user rather than
        # send them through a full browser login that quietly yields the wrong
        # client and then looks consistent afterwards.
        cfg = _load_json(BOSCH_CONFIG_PATH)
        return bool(cfg.get("client_id", ""))
    return False


def current_client_id() -> str:
    """Return the client_id of the token currently in use, reading the token file fresh.

    The token file is the single source of truth: both _exchange_code and
    refresh_token write the client_id into it, so reading it each call reflects an
    out-of-band re-auth (e.g. switching from a euda client to one-bike-app) without
    needing a server restart. Falls back to the configured/default client when no
    token exists yet (CLI sync before auth) or a legacy token lacks the field.
    Never raises and never returns None.
    """
    try:
        if BOSCH_TOKENS_PATH.exists():
            cid = _load_json(BOSCH_TOKENS_PATH).get("client_id")
            if cid:
                return cid
        return _get_client_id()
    except RuntimeError:
        # The configured client, not the hardcoded one: a EUDA user with a
        # half-written token file would otherwise route as non-EUDA.
        return _get_client_id()


def token_is_euda() -> bool:
    """True if the active token was minted by a euda-* (EU Data Act) client."""
    return current_client_id().startswith("euda-")


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


def _refresh_token() -> str:
    """Return a valid access token, refreshing if expired (5-min buffer).

    Thread-safe. Raises RuntimeError if tokens are missing or refresh fails.
    """
    global _tokens

    with _token_lock:
        if _tokens is None:
            if not BOSCH_TOKENS_PATH.exists():
                raise TokenRefused("Bosch not configured. Run: bosch-flow-mcp auth")
            _tokens = _load_json(BOSCH_TOKENS_PATH)

        if not _tokens.get("access_token"):
            # Checked before the expiry, which is what made this a KeyError
            # rather than a verdict. Same rule as a wire response with no
            # token: the credentials on disk are unusable, and no amount of
            # waiting fixes that.
            raise TokenRefused("Token file has no access token. Run: bosch-flow-mcp auth")

        now = datetime.now(timezone.utc).timestamp()
        if now < _tokens.get("expiry", 0) - 300:
            return _tokens["access_token"]

        if not _tokens.get("refresh_token"):
            raise TokenRefused("Token expired and no refresh token. Run: bosch-flow-mcp auth")

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
                raw = resp.read().decode()
        except urllib.error.HTTPError as e:
            # Checked before OSError, which it subclasses. Listed by what the
            # code says about the credentials rather than by range: 429 and 408
            # are 4xx that carry no verdict at all.
            if e.code in _REFUSAL_CODES:
                raise TokenRefused("Token refresh failed. Run: bosch-flow-mcp auth") from e
            raise RefreshNetworkError("Bosch could not answer the refresh request.") from e
        except OSError as e:
            # Not just URLError: urlopen wraps only connect-phase failures in
            # it, so a read timeout or a reset connection arrives bare.
            raise RefreshNetworkError("Could not reach Bosch to refresh the token.") from e

        try:
            new_tokens = json.loads(raw)
        except ValueError as e:
            raise RefreshNetworkError("Bosch returned an unreadable response.") from e

        if not isinstance(new_tokens, dict) or not new_tokens.get("access_token"):
            raise TokenRefused("Token refresh failed. Run: bosch-flow-mcp auth")

        _tokens = {
            "access_token": new_tokens["access_token"],
            "refresh_token": new_tokens.get("refresh_token", _tokens["refresh_token"]),
            "token_type": new_tokens.get("token_type", "Bearer"),
            "expiry": now + new_tokens.get("expires_in", 7200),
            "client_id": client_id,
        }
        _save_json(BOSCH_TOKENS_PATH, _tokens)
        return _tokens["access_token"]


def refresh_token() -> str:
    """Return a valid access token, refreshing if expired.

    Raises exactly two types: TokenRefused, and RefreshNetworkError for
    everything else. Never replace the catch-all with a list of exception
    types - anything unanticipated then reads as a dead credential, and that
    answer spends a working refresh token.
    """
    try:
        return _refresh_token()
    except (TokenRefused, RefreshNetworkError):
        raise
    except Exception as e:
        logger.error("Token refresh failed: %s", type(e).__name__)
        raise RefreshNetworkError("Could not obtain a token from Bosch.") from e


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

    print(
        "\nUnofficial tool, not affiliated with or endorsed by Bosch. It signs in with "
        "your own\nBosch eBike Flow account for read-only access to your own data. By "
        "continuing you\nconfirm you accept Bosch's and SingleKey ID's terms of use. It "
        "may stop working if\nBosch changes their API."
    )

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

    # Bind before opening the browser: the listener no longer asks to share the
    # port, so a busy one is now a real failure, and sending the user to an
    # authorisation page whose redirect has nowhere to land wastes the attempt.
    try:
        server = _CallbackServer(("localhost", EUDA_CALLBACK_PORT), CallbackHandler)
    except OSError:
        print(
            f"Port {EUDA_CALLBACK_PORT} is in use, so the callback cannot be received. "
            "It must match the redirect URI registered for the EUDA client and so "
            "cannot be changed. On Windows a socket from a recent `bosch-flow-mcp auth` "
            "may still be closing; retry once it has.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("\nOpening browser for Bosch login...")
    print(f"URL: {auth_url}\n")
    webbrowser.open(auth_url)

    print(f"Waiting for callback on localhost:{EUDA_CALLBACK_PORT}...")
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
            raw = resp.read()
    except urllib.error.HTTPError as e:
        # The status, never the body. Bosch has no obligation to keep an
        # account out of an error body, and this prints to a terminal.
        print(f"Error exchanging code: HTTP {e.code}.", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        # Wider than URLError, for the reason _refresh_token already documents:
        # urlopen wraps only connect-phase failures in it, so a read timeout or
        # a reset connection arrives bare and would otherwise escape as a
        # traceback, which names this install's absolute paths.
        print(f"Error exchanging code: {type(e).__name__}.", file=sys.stderr)
        sys.exit(1)

    # Parsed outside the transport handlers, the way _refresh_token does it: a
    # body that will not decode or is not JSON is its own failure cause, and
    # neither handler above catches ValueError.
    try:
        tokens = json.loads(raw)
    except ValueError:
        print("Error exchanging code: the response was not readable.", file=sys.stderr)
        sys.exit(1)

    if not isinstance(tokens, dict) or "access_token" not in tokens:
        # Not the dict. The realistic shape here carries a refresh_token and no
        # access_token, so printing it to explain the failure puts a live
        # credential on the terminal. The isinstance guard is what keeps a JSON
        # scalar or list from reaching the subscript below as a TypeError.
        print("Error: the token response carried no access token.", file=sys.stderr)
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
