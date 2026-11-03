"""Tests for the CLI sync output (the new status/message reporting)."""

import io
import json
import logging
import socket
import sqlite3
import threading
from unittest.mock import MagicMock, patch

import pytest

from bosch_flow_mcp import auth, cli, db


def test_cli_version_flag(capsys, monkeypatch):
    """--version prints the package version and exits cleanly."""
    monkeypatch.setattr("sys.argv", ["bosch-flow-mcp", "--version"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "bosch-flow-mcp" in out


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


class TestAFailedCommandEndsInOneLine:
    """A traceback names this install's absolute paths.

    The whole of stderr is compared, and nothing may be logged, because a `logger`
    call is invisible to `capsys` under pytest and lands on stderr in production.
    So a traceback, a path or an `OSError`'s own text cannot be added without failing.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch, caplog):
        self.config = tmp_path / "bosch_config.json"
        self.browser = MagicMock()
        self.tmp_path = tmp_path
        self.caplog = caplog
        caplog.set_level(logging.DEBUG)
        monkeypatch.setattr(auth, "BOSCH_CONFIG_PATH", self.config)
        monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path)
        monkeypatch.setattr(auth.webbrowser, "open", self.browser)

    def _run(self, command, capsys, monkeypatch, exit_code=1):
        monkeypatch.setattr("sys.argv", ["bosch-flow-mcp", command])
        try:
            with pytest.raises(SystemExit) as exc:
                cli.main()
        except KeyboardInterrupt:
            pytest.fail("the interrupt escaped main()")
        assert exc.value.code == exit_code
        captured = capsys.readouterr()
        assert str(self.tmp_path) not in captured.out
        assert self.caplog.records == []
        return captured.err

    @pytest.mark.parametrize("client", [0, [], True], ids=repr)
    def test_a_config_client_that_is_not_text_stops_auth(self, client, capsys, monkeypatch):
        self.config.write_text(json.dumps({"client_id": client}))
        err = self._run("auth", capsys, monkeypatch)
        assert err == "Error: bosch_config.json is malformed.\n"
        self.browser.assert_not_called()

    @pytest.mark.parametrize("contents", ["{not json", "[]"], ids=["not-json", "not-an-object"])
    def test_a_config_that_cannot_be_used_stops_auth_without_sending_it_back_to_auth(
        self, contents, capsys, monkeypatch
    ):
        self.config.write_text(contents)
        err = self._run("auth", capsys, monkeypatch)
        assert err == "Error: bosch_config.json is unreadable or malformed. Fix or remove it.\n"
        self.browser.assert_not_called()

    def test_a_config_directory_that_cannot_be_made_prints_only_its_type(
        self, tmp_path, capsys, monkeypatch
    ):
        blocker = tmp_path / "a-file"
        blocker.write_text("")
        monkeypatch.setattr(auth, "CONFIG_DIR", blocker)
        err = self._run("auth", capsys, monkeypatch)
        assert err == "Error: bosch-flow-mcp auth failed (FileExistsError).\n"
        self.browser.assert_not_called()

    def test_a_permission_error_at_auth_prints_only_its_type(self, capsys, monkeypatch):
        directory = MagicMock()
        directory.mkdir.side_effect = PermissionError(13, "Permission denied", "/srv/install")
        monkeypatch.setattr(auth, "CONFIG_DIR", directory)
        err = self._run("auth", capsys, monkeypatch)
        assert err == "Error: bosch-flow-mcp auth failed (PermissionError).\n"

    @pytest.mark.parametrize(
        "stdin",
        [
            pytest.param(lambda: io.StringIO(""), id="no-input"),
            pytest.param(lambda: None, id="closed"),
            pytest.param(
                lambda: io.TextIOWrapper(io.BytesIO(b"\xff\n"), encoding="utf-8"),
                id="undecodable",
            ),
        ],
    )
    def test_a_prompt_with_nothing_to_read_says_so(self, stdin, capsys, monkeypatch):
        monkeypatch.setattr("sys.stdin", stdin())
        err = self._run("auth", capsys, monkeypatch)
        assert err == "Error: no redirect URL could be read.\n"

    def test_a_pasted_url_that_will_not_parse_has_no_code_in_it(self, capsys, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("http://[not-a-host\n"))
        err = self._run("auth", capsys, monkeypatch)
        assert err == "Error: no 'code' parameter found in the URL.\n"

    def test_a_callback_url_that_will_not_parse_is_refused_without_a_traceback(
        self, capsys, monkeypatch
    ):
        self.config.write_text(json.dumps({"client_id": "euda-test"}))
        monkeypatch.setattr(auth, "EUDA_CALLBACK_PORT", 0)
        answers = []

        class Listener(auth._CallbackServer):
            def handle_request(self):
                def request():
                    with socket.create_connection(self.server_address[:2], timeout=5) as conn:
                        conn.sendall(b"GET http://[x HTTP/1.0\r\n\r\n")
                        answers.append(conn.recv(64))

                sender = threading.Thread(target=request)
                sender.start()
                super().handle_request()
                sender.join(5)

        monkeypatch.setattr(auth, "_CallbackServer", Listener)
        err = self._run("auth", capsys, monkeypatch)
        assert err == "Error: no auth code received.\n"
        assert answers and answers[0].startswith(b"HTTP/1.0 400")

    def test_an_interrupted_auth_says_so_and_exits_as_interrupted(self, capsys, monkeypatch):
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            err = self._run("auth", capsys, monkeypatch, exit_code=130)
        assert err == "Interrupted.\n"

    def test_an_interrupted_sync_says_so_and_exits_as_interrupted(self, capsys, monkeypatch):
        with patch("bosch_flow_mcp.tools.sync_tools.run_sync", side_effect=KeyboardInterrupt):
            err = self._run("sync", capsys, monkeypatch, exit_code=130)
        assert err == "Interrupted.\n"

    def test_a_file_that_is_not_a_database_stops_sync(self, tmp_path, capsys, monkeypatch):
        database = tmp_path / "not-a-database.db"
        database.write_bytes(b"not a database file " * 100)
        monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(database))
        err = self._run("sync", capsys, monkeypatch)
        assert err == "Error: bosch-flow-mcp sync failed (file is not a database).\n"

    def test_a_database_that_cannot_be_opened_stops_sync(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(tmp_path))
        err = self._run("sync", capsys, monkeypatch)
        assert err == "Error: bosch-flow-mcp sync failed (unable to open database file).\n"

    def test_a_lock_stops_sync_saying_it_is_a_lock(self, capsys, monkeypatch):
        locked = sqlite3.OperationalError("database is locked")
        with patch.object(db, "get_db", side_effect=locked):
            err = self._run("sync", capsys, monkeypatch)
        assert err == "Error: bosch-flow-mcp sync failed (database is locked).\n"

    def test_a_database_folder_that_cannot_be_made_stops_sync(self, tmp_path, capsys, monkeypatch):
        blocker = tmp_path / "a-file"
        blocker.write_text("")
        monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(blocker / "bosch_flow.db"))
        err = self._run("sync", capsys, monkeypatch)
        assert err == "Error: bosch-flow-mcp sync failed (FileExistsError).\n"

    def test_a_permission_error_at_sync_prints_only_its_type(self, capsys, monkeypatch):
        denied = PermissionError(13, "Permission denied", "/srv/install/bosch_flow.db")
        with patch.object(db, "get_db", side_effect=denied):
            err = self._run("sync", capsys, monkeypatch)
        assert err == "Error: bosch-flow-mcp sync failed (PermissionError).\n"

    @pytest.mark.parametrize("bug", [KeyError, RuntimeError, ValueError, EOFError])
    def test_an_unanticipated_failure_is_not_reported_as_an_ordinary_one(self, bug, monkeypatch):
        """A bug keeps its traceback, which is what gets it reported."""
        monkeypatch.setattr("sys.argv", ["bosch-flow-mcp", "auth"])
        with patch.object(cli, "setup_auth", side_effect=bug("a bug")):
            with pytest.raises(bug):
                cli.main()


def _callback_server_handling(failure):
    server = auth._CallbackServer(
        ("localhost", 0), auth.BaseHTTPRequestHandler, bind_and_activate=False
    )
    try:
        try:
            raise failure
        except Exception:
            server.handle_error(None, ("127.0.0.1", 1))
    finally:
        server.server_close()


@pytest.mark.parametrize(
    "failure", [ConnectionResetError(104, "reset"), BrokenPipeError(32, "broken pipe")]
)
def test_a_callback_connection_dropped_mid_request_prints_nothing(failure, capsys):
    _callback_server_handling(failure)
    assert capsys.readouterr().err == ""


def test_a_bug_in_the_callback_handler_keeps_its_traceback(capsys):
    _callback_server_handling(KeyError("a bug"))
    assert "Traceback" in capsys.readouterr().err


def test_a_type_that_fails_during_sync_prints_on_stdout_and_exits_0(capsys, monkeypatch):
    """Deliberate: the other types still ran, and each says how it went."""
    failed = {
        "bikes": {
            "status": "rate_limited",
            "records": 0,
            "code": "rate_limited",
            "message": "Try later.",
        }
    }
    monkeypatch.setattr("sys.argv", ["bosch-flow-mcp", "sync", "--types", "bikes"])
    with patch("bosch_flow_mcp.tools.sync_tools.run_sync", return_value=failed):
        cli.main()
    captured = capsys.readouterr()
    assert "  bikes: rate_limited - Try later.\n" in captured.out
    assert captured.err == ""
