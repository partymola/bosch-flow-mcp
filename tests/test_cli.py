"""Tests for the CLI sync output (the new status/message reporting)."""

from unittest.mock import patch

from bosch_flow_mcp import cli


def test_cli_sync_prints_unavailable_message(capsys, monkeypatch):
    """An unavailable type must print its explanatory message, not '0 records'."""
    fake = {
        "service": {
            "status": "unavailable",
            "records": 0,
            "code": "requires_euda",
            "message": "Register a euda client at portal.bosch-ebike.com/data-act",
        }
    }
    monkeypatch.setattr("sys.argv", ["bosch-flow-mcp", "sync", "--types", "service"])
    with patch("bosch_flow_mcp.tools.sync_tools.run_sync", return_value=fake):
        cli.main()
    out = capsys.readouterr().out
    assert "unavailable" in out
    assert "Register a euda client" in out
    assert "0 records" not in out


def test_cli_sync_ok_prints_record_count(capsys, monkeypatch):
    fake = {"bikes": {"status": "ok", "records": 2}}
    monkeypatch.setattr("sys.argv", ["bosch-flow-mcp", "sync", "--types", "bikes"])
    with patch("bosch_flow_mcp.tools.sync_tools.run_sync", return_value=fake):
        cli.main()
    out = capsys.readouterr().out
    assert "2 records" in out


def test_cli_sync_all_expands_to_all_types(capsys, monkeypatch):
    from bosch_flow_mcp.tools.sync_tools import _ALL_TYPES

    monkeypatch.setattr("sys.argv", ["bosch-flow-mcp", "sync", "--types", "all"])
    with patch("bosch_flow_mcp.tools.sync_tools.run_sync", return_value={}) as mock_run:
        cli.main()
    mock_run.assert_called_once_with(_ALL_TYPES)
