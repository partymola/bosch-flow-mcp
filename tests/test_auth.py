"""Tests for auth token management."""

import json
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
    """refresh_token raises RuntimeError if token file doesn't exist."""
    monkeypatch.setattr("bosch_flow_mcp.auth.BOSCH_TOKENS_PATH", tmp_path / "no_tokens.json")
    with pytest.raises(RuntimeError, match="not configured"):
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

    with pytest.raises(RuntimeError, match="no refresh token"):
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
