"""Tests for config path resolution."""

import errno
import sys
from pathlib import Path

import pytest

from bosch_flow_mcp.config import (
    BES3_BIKE,
    BES3_BIKES,
    BES3_REGISTRATIONS,
    BOSCH_AUTH_URL,
    BOSCH_TOKEN_URL,
    BOSCH_TOKENS_PATH,
    CLIENT_ID,
    CONFIG_DIR,
    DATA_ACT_API_BASE,
    DB_PATH,
    MOBILE_API_BASE,
    REDIRECT_URI,
    SCOPE,
    path_exists,
)
from tests.conftest import needs_posix_permissions


class TestPathExists:
    """The same answer on every Python, where `Path.exists` differs from 3.14."""

    def test_a_missing_path_is_absent(self, tmp_path):
        assert path_exists(tmp_path / "missing") is False
        assert path_exists(tmp_path / "missing-dir" / "bosch_tokens.json") is False

    def test_a_path_under_a_file_is_absent(self, tmp_path):
        (tmp_path / "a-file").write_text("")
        assert path_exists(tmp_path / "a-file" / "bosch_tokens.json") is False

    def test_a_present_file_or_directory_exists(self, tmp_path):
        (tmp_path / "bosch_tokens.json").write_text("{}")
        assert path_exists(tmp_path / "bosch_tokens.json") is True
        assert path_exists(tmp_path) is True

    @needs_posix_permissions
    def test_a_directory_that_cannot_be_searched_raises(self, tmp_path):
        locked = tmp_path / "locked-config"
        locked.mkdir()
        (locked / "bosch_tokens.json").write_text("{}")
        locked.chmod(0)
        try:
            with pytest.raises(PermissionError):
                (locked / "bosch_tokens.json").stat()
            with pytest.raises(PermissionError):
                path_exists(locked / "bosch_tokens.json")
        finally:
            locked.chmod(0o700)

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
    def test_a_symlink_loop_raises(self, tmp_path):
        """`Path.exists` answers False for one on every Python."""
        loop = tmp_path / "bosch_tokens.json"
        loop.symlink_to(loop)
        with pytest.raises(OSError) as exc_info:
            path_exists(loop)
        assert exc_info.value.errno == errno.ELOOP


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
