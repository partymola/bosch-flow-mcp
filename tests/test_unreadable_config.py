"""A config directory that cannot be searched, or a token file or config that is
a symlink loop, stops whatever needs it.

Without the token file nothing tells an EU Data Act sign-in from the standard
one, so nothing falls back to a default client. A token file that exists but
cannot be opened is a separate case: routing falls back to the configured client
and a refresh is `TokenRefused`.
"""

import errno
import io
import json
import sys
from unittest.mock import MagicMock

import pytest

from bosch_flow_mcp import auth, cli
from tests.conftest import needs_posix_permissions


@needs_posix_permissions
@pytest.mark.parametrize(
    "helper", ["current_client_id", "token_is_euda", "_get_client_id", "_is_euda"]
)
def test_each_helper_that_reads_the_config_directory_raises(helper, unsearchable_config_dir):
    with pytest.raises(PermissionError):
        getattr(auth, helper)()


@needs_posix_permissions
def test_a_refresh_is_a_network_error_and_not_a_refusal(unsearchable_config_dir):
    with pytest.raises(auth.RefreshNetworkError):
        auth.refresh_token()


@needs_posix_permissions
def test_auth_stops_before_the_browser_and_prints_only_the_type(
    unsearchable_config_dir, capsys, monkeypatch
):
    browser = MagicMock()
    monkeypatch.setattr(auth.webbrowser, "open", browser)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("sys.argv", ["bosch-flow-mcp", "auth"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    assert capsys.readouterr().err == "Error: bosch-flow-mcp auth failed (PermissionError).\n"
    browser.assert_not_called()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_a_token_file_that_is_a_symlink_loop_stops_routing_beside_a_readable_config(
    tmp_path, monkeypatch
):
    config = tmp_path / "bosch_config.json"
    config.write_text(json.dumps({"client_id": "euda-test"}))
    tokens = tmp_path / "bosch_tokens.json"
    tokens.symlink_to(tokens)
    monkeypatch.setattr(auth, "BOSCH_CONFIG_PATH", config)
    monkeypatch.setattr(auth, "BOSCH_TOKENS_PATH", tokens)

    with pytest.raises(OSError) as exc_info:
        auth.current_client_id()
    assert exc_info.value.errno == errno.ELOOP


# A symlink loop is what `Path.exists` answers False for on every Python, so
# these hold where a directory at mode 0 still raises from `Path.exists`.


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("helper", ["_get_client_id", "_is_euda"])
def test_a_config_that_is_a_symlink_loop_raises(helper, tmp_path, monkeypatch):
    config = tmp_path / "bosch_config.json"
    config.symlink_to(config)
    monkeypatch.setattr(auth, "BOSCH_CONFIG_PATH", config)

    with pytest.raises(OSError) as exc_info:
        getattr(auth, helper)()
    assert exc_info.value.errno == errno.ELOOP


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX open semantics for a directory")
def test_a_token_file_that_exists_but_cannot_be_opened_is_unusable_credentials(
    tmp_path, monkeypatch
):
    config = tmp_path / "bosch_config.json"
    config.write_text(json.dumps({"client_id": "euda-test"}))
    tokens = tmp_path / "bosch_tokens.json"
    tokens.mkdir()
    monkeypatch.setattr(auth, "BOSCH_CONFIG_PATH", config)
    monkeypatch.setattr(auth, "BOSCH_TOKENS_PATH", tokens)

    assert auth.current_client_id() == "euda-test"
    with pytest.raises(auth.TokenRefused):
        auth.refresh_token()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_a_refresh_from_a_token_file_that_is_a_symlink_loop_is_a_network_error(
    tmp_path, monkeypatch
):
    tokens = tmp_path / "bosch_tokens.json"
    tokens.symlink_to(tokens)
    monkeypatch.setattr(auth, "BOSCH_TOKENS_PATH", tokens)

    with pytest.raises(auth.RefreshNetworkError):
        auth.refresh_token()
