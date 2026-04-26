"""Tests for config path resolution."""

import os
import pytest
from pathlib import Path

from bosch_flow_mcp.config import (
    CONFIG_DIR, DB_PATH, BOSCH_TOKENS_PATH, BOSCH_CONFIG_PATH,
    CLIENT_ID, REDIRECT_URI, SCOPE,
    BOSCH_AUTH_URL, BOSCH_TOKEN_URL, MOBILE_API_BASE, DATA_ACT_API_BASE,
    BES3_BIKES, BES3_BIKE, BES3_REGISTRATIONS,
)


def test_config_dir_is_inside_package():
    """Config dir should be inside the package root, not ~/.config."""
    assert not str(CONFIG_DIR).startswith(str(Path.home() / ".config")), (
        "Config must live inside the tool directory, not ~/.config"
    )
    assert "bosch-flow-mcp" in str(CONFIG_DIR) or "config" in CONFIG_DIR.name


def test_db_path_inside_package():
    assert "bosch-flow-mcp" in str(DB_PATH) or DB_PATH.parent == CONFIG_DIR.parent


def test_tokens_path_inside_config_dir():
    assert BOSCH_TOKENS_PATH.parent == CONFIG_DIR


def test_env_override_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSCH_FLOW_MCP_CONFIG_DIR", str(tmp_path / "custom_config"))
    # Re-import to pick up env var (use importlib to avoid cached module)
    import importlib
    import bosch_flow_mcp.config as cfg_module
    importlib.reload(cfg_module)
    assert str(tmp_path / "custom_config") in str(cfg_module.CONFIG_DIR)
    # Restore
    importlib.reload(cfg_module)


def test_client_id_is_public():
    """Primary client is the one-bike-app public client (no secret needed)."""
    assert CLIENT_ID == "one-bike-app"
    assert "onebikeapp-ios" in REDIRECT_URI
    assert "offline_access" in SCOPE


def test_api_urls():
    assert BOSCH_AUTH_URL.startswith("https://p9.authz.bosch.com")
    assert BOSCH_TOKEN_URL.startswith("https://p9.authz.bosch.com")
    assert MOBILE_API_BASE == "https://obc-rider-profile.prod.connected-biking.cloud"
    assert DATA_ACT_API_BASE == "https://api.bosch-ebike.com"


def test_bes3_endpoint_paths():
    assert BES3_BIKES.startswith("/bike-profile/smart-system/")
    assert "{bike_id}" in BES3_BIKE
    assert BES3_REGISTRATIONS.startswith("/bike-registration/smart-system/")
