"""Tests for auth token management."""

import json
import socket
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import bosch_flow_mcp.auth as auth_module
from bosch_flow_mcp.config import CLIENT_ID


@pytest.fixture(autouse=True)
def reset_token_cache():
    """Reset in-memory token cache before each test."""
    auth_module._tokens = None
    yield
    auth_module._tokens = None


def test_refresh_token_missing_file(tmp_path, monkeypatch):
    """A missing token file is a refusal, not a network condition.

    Both classes subclass RuntimeError, so asserting the base class alone
    cannot tell them apart - and they lead to opposite advice: re-authorise,
    or wait for the network.
    """
    monkeypatch.setattr("bosch_flow_mcp.auth.BOSCH_TOKENS_PATH", tmp_path / "no_tokens.json")
    with pytest.raises(auth_module.TokenRefused, match="not configured"):
        auth_module.refresh_token()


def test_refresh_token_uses_cached_if_valid(tmp_path, monkeypatch):
    """refresh_token returns cached token if not expired."""
    future_expiry = datetime.now(timezone.utc).timestamp() + 3600
    auth_module._tokens = {
        "access_token": "cached_token_abc",
        "refresh_token": "refresh_xyz",
        "expiry": future_expiry,
    }
    token = auth_module.refresh_token()
    assert token == "cached_token_abc"


def test_refresh_token_refreshes_when_expired(tmp_path, monkeypatch):
    """refresh_token calls token URL when access token is expired."""
    past_expiry = datetime.now(timezone.utc).timestamp() - 10
    auth_module._tokens = {
        "access_token": "old_token",
        "refresh_token": "valid_refresh",
        "expiry": past_expiry,
    }

    fake_response_data = json.dumps(
        {
            "access_token": "new_token_def",
            "refresh_token": "new_refresh",
            "expires_in": 7200,
        }
    ).encode()

    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.read.return_value = fake_response_data

    tokens_path = tmp_path / "tokens.json"
    tokens_path.write_text(
        json.dumps(
            {
                "access_token": "old_token",
                "refresh_token": "valid_refresh",
                "expiry": past_expiry,
            }
        )
    )
    monkeypatch.setattr("bosch_flow_mcp.auth.BOSCH_TOKENS_PATH", tokens_path)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        token = auth_module.refresh_token()

    assert token == "new_token_def"
    assert auth_module._tokens["access_token"] == "new_token_def"


def test_refresh_token_no_refresh_token_raises(tmp_path, monkeypatch):
    """refresh_token raises RuntimeError if refresh_token is missing."""
    past_expiry = datetime.now(timezone.utc).timestamp() - 10
    auth_module._tokens = {
        "access_token": "old_token",
        "refresh_token": "",
        "expiry": past_expiry,
    }
    tokens_path = tmp_path / "tokens.json"
    tokens_path.write_text(json.dumps(auth_module._tokens))
    monkeypatch.setattr("bosch_flow_mcp.auth.BOSCH_TOKENS_PATH", tokens_path)

    with pytest.raises(auth_module.TokenRefused, match="no refresh token"):
        auth_module.refresh_token()


def test_invalidate_token_cache():
    auth_module._tokens = {"access_token": "something"}
    auth_module.invalidate_token_cache()
    assert auth_module._tokens is None


def test_client_id_is_one_bike_app():
    """Primary client ID is the mobile app public client."""
    assert CLIENT_ID == "one-bike-app"


def test_generate_pkce():
    """PKCE verifier and challenge should be non-empty base64url strings."""
    verifier, challenge = auth_module._generate_pkce()
    assert len(verifier) >= 40
    assert len(challenge) >= 40
    assert "=" not in verifier
    assert "=" not in challenge


# --- Routing helpers: current_client_id / token_is_euda ---


def test_current_client_id_reads_stored_id(token_as):
    """current_client_id reflects the client_id written in the token file."""
    token_as("euda-abc123")
    assert auth_module.current_client_id() == "euda-abc123"


def test_current_client_id_fallback_when_no_client_id(tmp_path, monkeypatch):
    """A legacy token lacking client_id falls back to the default client."""
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"access_token": "x", "expiry": 9999999999}))
    monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", path)
    assert auth_module.current_client_id() == CLIENT_ID


def test_current_client_id_missing_file_fallback():
    """With no token file (isolated by default), falls back without raising."""
    assert auth_module.current_client_id() == CLIENT_ID


def test_current_client_id_reflects_file_change(token_as):
    """current_client_id reads the file fresh, so an out-of-band re-auth is seen."""
    token_as("one-bike-app")
    assert auth_module.current_client_id() == "one-bike-app"
    token_as("euda-newclient")  # rewrites the same token file
    assert auth_module.current_client_id() == "euda-newclient"


@pytest.mark.parametrize(
    "client_id,expected",
    [
        ("euda-00000000-0000-0000-0000-000000000001", True),
        ("euda-", True),
        ("one-bike-app", False),
    ],
)
def test_token_is_euda(token_as, client_id, expected):
    token_as(client_id)
    assert auth_module.token_is_euda() is expected


def test_token_is_euda_no_token_is_false():
    """No token file -> default one-bike-app -> not euda (and no crash on None)."""
    assert auth_module.token_is_euda() is False


class TestTheRefreshBoundary:
    """Every exit from refresh_token is one of two types, by construction.

    Classifying by a list of exception types instead grades whatever nobody
    listed as a dead credential, which is the answer that rewrites the token
    file and spends a working refresh token.
    """

    def _refresh_with_worker_raising(self, exc, monkeypatch):
        monkeypatch.setattr(auth_module, "_refresh_token", MagicMock(side_effect=exc))
        return auth_module.refresh_token()

    @pytest.mark.parametrize(
        "exc",
        [
            TimeoutError("bare timeout"),
            ConnectionResetError("reset"),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
            KeyError("access_token"),
            ValueError("unparseable"),
            RuntimeError("something nobody classified"),
        ],
        ids=["timeout", "reset", "undecodable", "keyerror", "valueerror", "runtime"],
    )
    def test_an_unclassified_failure_becomes_a_network_error(self, exc, monkeypatch):
        with pytest.raises(auth_module.RefreshNetworkError):
            self._refresh_with_worker_raising(exc, monkeypatch)

    def test_a_non_http_response_becomes_a_network_error(self, monkeypatch):
        """http.client exceptions are not OSError, so they escaped every tuple."""
        import http.client

        with pytest.raises(auth_module.RefreshNetworkError):
            self._refresh_with_worker_raising(http.client.BadStatusLine("garbage"), monkeypatch)

    def test_a_refusal_passes_through_unchanged(self, monkeypatch):
        with pytest.raises(auth_module.TokenRefused):
            self._refresh_with_worker_raising(auth_module.TokenRefused("revoked"), monkeypatch)

    def test_a_successful_refresh_is_not_swallowed(self, monkeypatch):
        monkeypatch.setattr(auth_module, "_refresh_token", MagicMock(return_value="a-token"))
        assert auth_module.refresh_token() == "a-token"


class TestRefusalsAndNetworkConditions:
    """Only a verdict on the credentials is a refusal."""

    def _refresh_with_urlopen(self, urlopen, tmp_path, monkeypatch):
        tokens = tmp_path / "bosch_tokens.json"
        tokens.write_text(
            json.dumps(
                {"access_token": "a", "refresh_token": "r", "expiry": 0, "client_id": CLIENT_ID}
            )
        )
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", tokens)
        monkeypatch.setattr(auth_module.urllib.request, "urlopen", urlopen)
        return auth_module.refresh_token()

    def _http_error(self, code):
        import io
        import urllib.error

        return urllib.error.HTTPError("https://example.invalid", code, "no", {}, io.BytesIO(b""))

    def _responding(self, payload):
        resp = MagicMock()
        resp.read.return_value = payload
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        return MagicMock(return_value=resp)

    def test_a_revoked_token_is_a_refusal(self, tmp_path, monkeypatch):
        with pytest.raises(auth_module.TokenRefused):
            self._refresh_with_urlopen(
                MagicMock(side_effect=self._http_error(400)), tmp_path, monkeypatch
            )

    def test_a_rate_limit_is_not_a_refusal(self, tmp_path, monkeypatch):
        with pytest.raises(auth_module.RefreshNetworkError):
            self._refresh_with_urlopen(
                MagicMock(side_effect=self._http_error(429)), tmp_path, monkeypatch
            )

    def test_a_server_side_failure_is_not_a_refusal(self, tmp_path, monkeypatch):
        """A 5xx says the server could not answer, not that the grant is bad."""
        with pytest.raises(auth_module.RefreshNetworkError):
            self._refresh_with_urlopen(
                MagicMock(side_effect=self._http_error(500)), tmp_path, monkeypatch
            )

    def test_a_waf_block_is_not_a_refusal(self, tmp_path, monkeypatch):
        """403 is what bot protection returns; it says nothing about the grant."""
        with pytest.raises(auth_module.RefreshNetworkError):
            self._refresh_with_urlopen(
                MagicMock(side_effect=self._http_error(403)), tmp_path, monkeypatch
            )

    def test_an_undecodable_body_is_not_a_refusal(self, tmp_path, monkeypatch):
        with pytest.raises(auth_module.RefreshNetworkError):
            self._refresh_with_urlopen(
                self._responding(b"\xe9\xff not utf-8"), tmp_path, monkeypatch
            )

    def test_a_body_that_is_not_json_is_not_a_refusal(self, tmp_path, monkeypatch):
        with pytest.raises(auth_module.RefreshNetworkError):
            self._refresh_with_urlopen(
                self._responding(b"<html>captive portal</html>"), tmp_path, monkeypatch
            )

    def test_a_response_without_a_token_is_a_refusal(self, tmp_path, monkeypatch):
        with pytest.raises(auth_module.TokenRefused):
            self._refresh_with_urlopen(
                self._responding(b'{"token_type": "Bearer"}'), tmp_path, monkeypatch
            )

    def test_an_unreadable_token_file_is_a_refusal(self, tmp_path, monkeypatch):
        tokens = tmp_path / "bosch_tokens.json"
        tokens.write_text("{not json")
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", tokens)
        with pytest.raises(auth_module.TokenRefused):
            auth_module.refresh_token()

    def test_an_unreachable_server_is_not_a_refusal(self, tmp_path, monkeypatch):
        import urllib.error

        with pytest.raises(auth_module.RefreshNetworkError):
            self._refresh_with_urlopen(
                MagicMock(side_effect=urllib.error.URLError("name resolution failed")),
                tmp_path,
                monkeypatch,
            )

    def test_a_read_timeout_is_not_a_refusal(self, tmp_path, monkeypatch):
        """urlopen wraps only connect-phase errors, so this arrives bare."""
        with pytest.raises(auth_module.RefreshNetworkError):
            self._refresh_with_urlopen(
                MagicMock(side_effect=TimeoutError("timed out")), tmp_path, monkeypatch
            )

    def test_a_reset_connection_is_not_a_refusal(self, tmp_path, monkeypatch):
        with pytest.raises(auth_module.RefreshNetworkError):
            self._refresh_with_urlopen(
                MagicMock(side_effect=ConnectionResetError("reset")), tmp_path, monkeypatch
            )


class TestTheRoutingHelpersNeverRaise:
    """current_client_id and token_is_euda are called outside any handler.

    _load_json classifies an unusable credential file as TokenRefused, which
    is right for the refresh path. These two answer "which client is in use"
    for routing, and sync_tools calls one before its try block - so a corrupt
    token file used to take the whole sync out with an unclassified error.
    """

    @pytest.mark.parametrize(
        "contents",
        ["{not json", '["not", "an", "object"]', ""],
        ids=["unparseable", "wrong-shape", "empty"],
    )
    def test_current_client_id_falls_back(self, contents, tmp_path, monkeypatch):
        path = tmp_path / "bosch_tokens.json"
        path.write_text(contents)
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", path)
        assert auth_module.current_client_id() == CLIENT_ID

    @pytest.mark.parametrize(
        "contents",
        ["{not json", '["not", "an", "object"]'],
        ids=["unparseable", "wrong-shape"],
    )
    def test_token_is_euda_falls_back(self, contents, tmp_path, monkeypatch):
        path = tmp_path / "bosch_tokens.json"
        path.write_text(contents)
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", path)
        assert auth_module.token_is_euda() is False


class TestRefusalsAreNotNetworkConditions:
    """The refusal sites the commit named, each pinned to the right class."""

    def _refresh_with_response(self, payload, tmp_path, monkeypatch):
        path = tmp_path / "bosch_tokens.json"
        path.write_text(json.dumps({"access_token": "a", "refresh_token": "r", "expiry": 0}))
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", path)
        resp = MagicMock()
        resp.read.return_value = payload
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        monkeypatch.setattr(auth_module.urllib.request, "urlopen", MagicMock(return_value=resp))
        return auth_module.refresh_token()

    def test_an_unauthorised_response_is_a_refusal(self, tmp_path, monkeypatch):
        import io
        import urllib.error

        path = tmp_path / "bosch_tokens.json"
        path.write_text(json.dumps({"access_token": "a", "refresh_token": "r", "expiry": 0}))
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", path)
        monkeypatch.setattr(
            auth_module.urllib.request,
            "urlopen",
            MagicMock(side_effect=urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b""))),
        )
        with pytest.raises(auth_module.TokenRefused):
            auth_module.refresh_token()

    def test_a_response_of_the_wrong_shape_is_a_refusal(self, tmp_path, monkeypatch):
        with pytest.raises(auth_module.TokenRefused):
            self._refresh_with_response(b'["not", "an", "object"]', tmp_path, monkeypatch)

    def test_a_response_without_a_token_is_a_refusal(self, tmp_path, monkeypatch):
        with pytest.raises(auth_module.TokenRefused):
            self._refresh_with_response(b'{"token_type": "Bearer"}', tmp_path, monkeypatch)

    def test_a_token_file_of_the_wrong_shape_is_a_refusal(self, tmp_path, monkeypatch):
        path = tmp_path / "bosch_tokens.json"
        path.write_text('["not", "an", "object"]')
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", path)
        with pytest.raises(auth_module.TokenRefused):
            auth_module.refresh_token()


class TestTheFallbackKeepsTheConfiguredClient:
    """Which client the fallback picks, not merely that it does not raise.

    A EUDA user whose token file is half-written must still route as EUDA:
    falling back to the hardcoded client instead answers the Data-Act types
    with "register a euda client", which they already have, and nothing
    anywhere says the token file is the problem.
    """

    def _with_euda_config(self, tmp_path, monkeypatch, tokens_contents):
        config_path = tmp_path / "bosch_config.json"
        config_path.write_text(
            json.dumps({"client_id": "euda-00000000-0000-0000-0000-000000000009"})
        )
        tokens_path = tmp_path / "bosch_tokens.json"
        tokens_path.write_text(tokens_contents)
        monkeypatch.setattr(auth_module, "BOSCH_CONFIG_PATH", config_path)
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", tokens_path)

    def test_a_corrupt_token_file_still_routes_as_euda(self, tmp_path, monkeypatch):
        self._with_euda_config(tmp_path, monkeypatch, "{not json")
        assert auth_module.current_client_id().startswith("euda-")
        assert auth_module.token_is_euda() is True

    def test_a_readable_token_file_still_wins(self, tmp_path, monkeypatch):
        self._with_euda_config(tmp_path, monkeypatch, json.dumps({"client_id": "one-bike-app"}))
        assert auth_module.current_client_id() == "one-bike-app"
        assert auth_module.token_is_euda() is False

    def test_no_token_file_still_routes_as_euda(self, tmp_path, monkeypatch):
        config_path = tmp_path / "bosch_config.json"
        config_path.write_text(
            json.dumps({"client_id": "euda-00000000-0000-0000-0000-000000000009"})
        )
        monkeypatch.setattr(auth_module, "BOSCH_CONFIG_PATH", config_path)
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", tmp_path / "absent.json")
        assert auth_module.token_is_euda() is True

    def test_a_legacy_token_without_a_client_id_still_routes_as_euda(self, tmp_path, monkeypatch):
        self._with_euda_config(tmp_path, monkeypatch, json.dumps({"access_token": "a"}))
        assert auth_module.token_is_euda() is True

    def test_a_malformed_config_file_does_not_raise(self, tmp_path, monkeypatch):
        """_get_client_id's own guard is what keeps the never-raises promise.

        current_client_id calls it from its except branch, outside any try.
        """
        config_path = tmp_path / "bosch_config.json"
        config_path.write_text("{not json")
        monkeypatch.setattr(auth_module, "BOSCH_CONFIG_PATH", config_path)
        tokens_path = tmp_path / "bosch_tokens.json"
        tokens_path.write_text("{not json either")
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", tokens_path)

        assert auth_module.current_client_id() == CLIENT_ID
        assert auth_module.token_is_euda() is False


class TestAStoredTokenWithoutOneIsARefusal:
    def test_a_token_file_with_no_access_token(self, tmp_path, monkeypatch):
        """The expiry check ran first, so this escaped api.get as a KeyError.

        Reaching this state needs a writer outside this repo: _exchange_code and
        _refresh_token both always store an access_token, and a half-written file
        fails to parse and is refused before this check. Note the refusal is
        strict - a file holding a live refresh_token would have refreshed - so if
        a future writer can produce this state, guard the early return rather
        than relax this test.
        """
        path = tmp_path / "bosch_tokens.json"
        path.write_text(
            json.dumps({"refresh_token": "r", "expiry": 4102444800, "client_id": CLIENT_ID})
        )
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", path)
        with pytest.raises(auth_module.TokenRefused):
            auth_module.refresh_token()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits; Windows uses ACLs")
class TestTheTokenFileIsNeverBrieflyReadable:
    """Writing first and chmodding after left a new token world-readable.

    Only for the duration of the write, and only at creation - but the
    window is real, and the mode was untested either way.
    """

    def test_a_new_file_is_created_owner_only(self, tmp_path):
        import os as os_module

        seen = {}
        real_open = os_module.open

        def spy(path, flags, mode=0o777, **kwargs):
            seen["mode"] = mode
            return real_open(path, flags, mode, **kwargs)

        target = tmp_path / "bosch_tokens.json"
        with patch.object(auth_module.os, "open", spy):
            auth_module._save_json(target, {"refresh_token": "fictional"})

        assert oct(seen["mode"]) == "0o600"
        assert oct(target.stat().st_mode & 0o777) == "0o600"

    def test_an_existing_loose_file_is_tightened(self, tmp_path):
        """And tightened before the token is written, not after.

        On a pre-existing file the mode at open is whatever it already was,
        so writing first would put a real token on disk world-readable -
        which the final mode and the open mode both look right through.
        """
        import os as os_module

        real_fchmod = os_module.fchmod

        def spy(fd, mode):
            assert os_module.fstat(fd).st_size == 0, "content was written before tightening"
            return real_fchmod(fd, mode)

        target = tmp_path / "bosch_tokens.json"
        target.write_text("{}")
        os_module.chmod(target, 0o644)
        with patch.object(auth_module.os, "fchmod", spy):
            auth_module._save_json(target, {"refresh_token": "fictional"})
        assert oct(target.stat().st_mode & 0o777) == "0o600"

    def test_a_failure_to_tighten_does_not_take_the_token_with_it(self, tmp_path):
        def refuse(fd, mode):
            raise PermissionError(1, "Operation not permitted")

        target = tmp_path / "bosch_tokens.json"
        target.write_text('{"refresh_token": "old"}')
        with patch.object(auth_module.os, "fchmod", refuse):
            auth_module._save_json(target, {"refresh_token": "rotated"})

        assert json.loads(target.read_text())["refresh_token"] == "rotated"


class _RecordingSocket:
    """Stands in for the real socket so server_bind's Windows branch can run."""

    def __init__(self):
        self.calls = []

    def setsockopt(self, level, option, value):
        self.calls.append(("setsockopt", level, option, value))

    def bind(self, address):
        self.calls.append(("bind", address))

    def getsockname(self):
        return ("127.0.0.1", auth_module.EUDA_CALLBACK_PORT)


class TestTheCallbackPortIsNotShared:
    """The listener carries the authorisation code, so it must not be displaceable.

    `server_bind` chooses on `sys.platform` at call time, so the Windows branch
    is reachable from a POSIX runner against a recording socket. That is worth
    more than reading the source for it: source assertions here let the option
    be set after the bind, at the wrong level, on the wrong socket, or with a
    value of 0, all of which pass a check that only looks for the name.
    """

    def _bind_as(self, platform, monkeypatch):
        monkeypatch.setattr(auth_module.sys, "platform", platform)
        # Absent on POSIX, so it has to be created rather than replaced.
        monkeypatch.setattr(auth_module.socket, "SO_EXCLUSIVEADDRUSE", 0xFFFFFFFF, raising=False)
        monkeypatch.setattr(auth_module.socket, "getfqdn", lambda host: host)

        server = auth_module._CallbackServer.__new__(auth_module._CallbackServer)
        server.socket = _RecordingSocket()
        server.server_address = ("localhost", auth_module.EUDA_CALLBACK_PORT)
        # The class attribute is fixed at import against the real platform, so
        # the value the other test pins is supplied here rather than read.
        server.allow_reuse_address = platform != "win32"
        server.allow_reuse_port = False
        auth_module._CallbackServer.server_bind(server)
        return server.socket.calls

    def test_windows_asks_for_exclusive_use_before_binding(self, monkeypatch):
        calls = self._bind_as("win32", monkeypatch)
        assert calls[0] == (
            "setsockopt",
            socket.SOL_SOCKET,
            auth_module.socket.SO_EXCLUSIVEADDRUSE,
            1,
        ), calls
        assert ("bind", ("localhost", auth_module.EUDA_CALLBACK_PORT)) in calls
        # Never both: asking to share after asking not to is the configuration
        # Microsoft documents as insecure.
        assert not any(
            call[:3] == ("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEADDR) for call in calls
        ), calls

    def test_posix_asks_for_nothing_exclusive(self, monkeypatch):
        calls = self._bind_as("linux", monkeypatch)
        assert not any(
            call[0] == "setsockopt" and call[2] == auth_module.socket.SO_EXCLUSIVEADDRUSE
            for call in calls
        ), calls

    def test_reuse_is_allowed_off_windows_and_refused_on_it(self):
        """Not asking to share is the fix; the exclusive option is the belt.

        The runtime value alone cannot see a revert to a bare `True`, because
        that is what this platform is supposed to have, so the source form is
        pinned with it.
        """
        import ast
        import inspect

        assert auth_module._CallbackServer.allow_reuse_address == (sys.platform != "win32")

        tree = ast.parse(inspect.getsource(auth_module._CallbackServer))
        assigned = [
            ast.unparse(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "allow_reuse_address" for t in node.targets)
        ]
        assert assigned, "allow_reuse_address is no longer set here"
        assert all("platform" in value for value in assigned), assigned

    def test_only_win32_is_named(self):
        """`sys.platform` is `win32` on 64-bit Windows too, so a `win64` test
        is a branch that never runs."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(auth_module._CallbackServer))
        compared = {
            const.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare) and ast.unparse(node.left) == "sys.platform"
            for const in node.comparators
            if isinstance(const, ast.Constant)
        }
        assert compared == {"win32"}, compared

    @pytest.mark.skipif(sys.platform == "win32", reason="binds a real POSIX socket")
    def test_it_still_binds(self):
        """server_bind is overridden, so a mistake there breaks `auth` outright."""
        server = auth_module._CallbackServer(("localhost", 0), auth_module.BaseHTTPRequestHandler)
        try:
            assert server.server_address[0] == "127.0.0.1"
            assert server.server_address[1] != 0
        finally:
            server.server_close()

    def test_no_bare_httpserver_is_constructed_anywhere(self):
        """Scoped to the module, not to the EUDA flow: a second listener added
        elsewhere would carry the default this class exists to refuse."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(auth_module))
        bare = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "HTTPServer"
        ]
        assert not bare, bare

    def test_a_busy_port_is_reported_before_the_browser_opens(self, monkeypatch, capsys):
        """The only test that reaches the EUDA flow, and it patches the name the
        flow constructs: patching `HTTPServer` instead guards nothing.

        A port that is already held is a failure the flow could not have before,
        so it has to end in advice rather than a traceback - and it has to end
        there before an authorisation page is opened onto a redirect with
        nowhere to land.
        """
        opened = []
        monkeypatch.setattr(auth_module.webbrowser, "open", lambda url: opened.append(url))

        def refuse_to_bind(*args, **kwargs):
            raise OSError(98, "Address already in use")

        monkeypatch.setattr(auth_module, "_CallbackServer", refuse_to_bind)

        with pytest.raises(SystemExit) as exit_info:
            auth_module._setup_euda_auth()

        assert exit_info.value.code == 1
        assert opened == [], opened
        assert str(auth_module.EUDA_CALLBACK_PORT) in capsys.readouterr().err
