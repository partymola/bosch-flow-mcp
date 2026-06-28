"""Tests for the Bosch API HTTP client."""

import json
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

import bosch_flow_mcp.api as api_module
from bosch_flow_mcp.api import BoschAPIError, BoschAuthError, BoschRateLimitError


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
