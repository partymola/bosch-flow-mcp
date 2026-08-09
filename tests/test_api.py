"""Tests for the Bosch API HTTP client."""

import json
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

import bosch_flow_mcp.api as api_module
import bosch_flow_mcp.auth as auth_module
from bosch_flow_mcp.api import (
    BoschAPIError,
    BoschAuthError,
    BoschForbiddenError,
    BoschRateLimitError,
)


@pytest.fixture(autouse=True)
def patch_refresh_token():
    """Patch refresh_token to return a fake token in all API tests."""
    with patch("bosch_flow_mcp.api.refresh_token", return_value="fake_access_token"):
        yield


def _mock_urlopen(response_data: dict):
    """Create a mock urlopen context manager returning JSON."""
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.read.return_value = json.dumps(response_data).encode()
    return mock_resp


def test_get_success():
    with patch("urllib.request.urlopen", return_value=_mock_urlopen({"bikes": []})):
        result = api_module.get("/bike-profile/smart-system/v1/bikes")
    assert result == {"bikes": []}


def test_get_404_returns_none():
    err = HTTPError(url="", code=404, msg="Not Found", hdrs={}, fp=None)
    with patch("urllib.request.urlopen", side_effect=err):
        result = api_module.get("/some/path")
    assert result is None


def test_get_403_raises_forbidden():
    """403 must raise (not return None) so routing can distinguish wrong-client."""
    err = HTTPError(url="", code=403, msg="Forbidden", hdrs={}, fp=None)
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(BoschForbiddenError):
            api_module.get("/some/path")


def test_forbidden_is_subclass_of_api_error():
    """run_sync's except BoschAPIError and the soc handler rely on this hierarchy."""
    assert issubclass(BoschForbiddenError, BoschAPIError)


def test_get_429_raises_rate_limit():
    err = HTTPError(url="", code=429, msg="Too Many Requests", hdrs={}, fp=None)
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(BoschRateLimitError):
            api_module.get("/some/path")


def test_get_401_retries_then_raises():
    err = HTTPError(url="", code=401, msg="Unauthorized", hdrs={}, fp=None)
    with patch("urllib.request.urlopen", side_effect=err):
        with patch("bosch_flow_mcp.api.invalidate_token_cache"):
            with pytest.raises(BoschAuthError, match="failed after retry"):
                api_module.get("/some/path", retries=2)


def test_get_network_error_raises():
    with patch("urllib.request.urlopen", side_effect=URLError("connection refused")):
        with pytest.raises(BoschAPIError, match="Network error"):
            api_module.get("/some/path")


def test_get_500_raises_api_error():
    mock_fp = MagicMock()
    mock_fp.read.return_value = b"Internal Server Error"
    err = HTTPError(url="", code=500, msg="Server Error", hdrs={}, fp=mock_fp)
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(BoschAPIError, match="500"):
            api_module.get("/some/path")


class TestRefreshFailuresReachCallersClassified:
    """api.get maps the two types refresh_token guarantees, and nothing else.

    The messages are asserted by equality because they reach the MCP client
    and are written to sync_log: the original can carry an absolute config
    path, which is what interpolating it used to do.
    """

    def _get_with_refresh_raising(self, exc, monkeypatch):
        monkeypatch.setattr(api_module, "refresh_token", MagicMock(side_effect=exc))
        return api_module.get("/bikes")

    def test_a_refusal_becomes_an_auth_error(self, monkeypatch):
        with pytest.raises(api_module.BoschAuthError) as caught:
            self._get_with_refresh_raising(
                auth_module.TokenRefused("/etc/secret/path is missing"), monkeypatch
            )
        assert str(caught.value) == "Could not obtain an access token. Run: bosch-flow-mcp auth"

    def test_a_network_failure_is_not_an_auth_error(self, monkeypatch):
        with pytest.raises(api_module.BoschAPIError) as caught:
            self._get_with_refresh_raising(
                auth_module.RefreshNetworkError("/etc/secret/path timed out"), monkeypatch
            )
        assert not isinstance(caught.value, api_module.BoschAuthError)
        assert str(caught.value) == "Network error. Check your connection."


class TestNothingLoggedCarriesTheRequestPath:
    """An MCP client captures this server's stderr to a file on disk.

    Some request paths carry a part number and a battery serial, so a log
    line naming one writes it to disk just as surely as the sync log did.
    """

    def test_a_403_logs_the_host_not_the_path(self, monkeypatch, caplog):
        import logging

        secret_path = "/capacity-testers?partNumber=PART-SECRET&serialNumber=SN-SECRET"
        monkeypatch.setattr(api_module, "refresh_token", lambda: "tok")
        monkeypatch.setattr(
            api_module.urllib.request,
            "urlopen",
            MagicMock(side_effect=HTTPError("https://example.invalid", 403, "no", {}, None)),
        )

        with caplog.at_level(logging.INFO, logger="bosch_flow_mcp.api"):
            with pytest.raises(BoschForbiddenError):
                api_module.get(secret_path)

        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "PART-SECRET" not in logged
        assert "SN-SECRET" not in logged
        assert "capacity-testers" not in logged
