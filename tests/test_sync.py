"""Tests for sync orchestration."""

import json
import logging
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from bosch_flow_mcp.tools import sync_tools
from bosch_flow_mcp.tools.sync_tools import auto_sync_if_stale, run_sync
from tests.conftest import FAKE_BIKE_ID

# Fake API responses using fictional data
FAKE_BIKES_RESPONSE = {
    "bikes": [
        {
            "id": FAKE_BIKE_ID,
            "attributes": {
                "brandName": "TestBrand",
                "frameNumber": "TESTFRAME001",
                "name": "My Test Bike",
            },
        }
    ]
}

FAKE_BIKE_DETAIL_RESPONSE = {
    "attributes": {
        "brandName": "TestBrand",
        "frameNumber": "TESTFRAME001",
        "batteries": [
            {
                "serialNumber": "SN123456",
                "partNumber": "TESTPART001",
                "batteryLevel": 75,
                "remainingEnergy": 468.75,
                "totalEnergy": 625.0,
                "isCharging": False,
                "numberOfFullChargeCycles": {"total": 42, "onBike": 40, "offBike": 2},
                "deliveredWhOverLifetime": 21000.0,
                "softwareVersion": "1.2.3",
            }
        ],
    }
}

FAKE_REGISTRATIONS_RESPONSE = {
    "registrations": [
        {
            "bikeId": FAKE_BIKE_ID,
            "components": [
                {
                    "componentType": "driveUnit",
                    "partNumber": "TESTPART002",
                    "serialNumber": "SN654321",
                    "productName": "Test Drive Unit CX",
                    "softwareVersion": "4.5.6",
                }
            ],
        }
    ]
}


def _mock_api_get(path, base=None, retries=3):
    """Intercept api.get calls with fake responses."""
    # Mobile API: /v1/bike-profile returns bike list
    if path == "/v1/bike-profile":
        return [
            {
                "id": FAKE_BIKE_ID,
                "brandName": "TestBrand",
                "frameNumber": "TESTFRAME001",
                "name": "My Test Bike",
            }
        ]
    # Mobile API v2: /v2/bike-profile/{bike_id}
    if "/v2/bike-profile/" in path and FAKE_BIKE_ID in path:
        return FAKE_BIKE_DETAIL_RESPONSE
    # Data Act API fallbacks
    if "bikes" in path and "{" not in path and path.endswith("bikes"):
        return FAKE_BIKES_RESPONSE
    if FAKE_BIKE_ID in path:
        return FAKE_BIKE_DETAIL_RESPONSE
    if "registrations" in path:
        return FAKE_REGISTRATIONS_RESPONSE
    if "service-records" in path:
        return {"serviceRecords": []}
    if "installation-reports" in path:
        return {"installationReports": []}
    if "capacity-testers" in path:
        return []
    return {}


def test_run_sync_bikes(tmp_path):
    """Syncing bikes fetches and stores bike data."""
    import os

    import bosch_flow_mcp.db as db_module

    os.environ["BOSCH_FLOW_MCP_DB_PATH"] = str(tmp_path / "test.db")

    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=_mock_api_get):
        results = run_sync(["bikes"])

    assert results["bikes"]["status"] == "ok"
    assert results["bikes"]["records"] == 1

    conn = db_module.get_db(tmp_path / "test.db")
    bikes = db_module.query_bikes(conn)
    conn.close()
    assert len(bikes) == 1
    assert bikes[0]["bike_id"] == FAKE_BIKE_ID


def test_run_sync_batteries(tmp_path):
    """Syncing batteries creates snapshots for each bike's battery."""
    import os

    os.environ["BOSCH_FLOW_MCP_DB_PATH"] = str(tmp_path / "test.db")

    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=_mock_api_get):
        results = run_sync(["bikes", "batteries"])

    assert results["batteries"]["status"] == "ok"
    assert results["batteries"]["records"] >= 1


def test_run_sync_unknown_type():
    results = run_sync(["does_not_exist"])
    assert results["does_not_exist"]["status"] == "error"


def test_auto_sync_if_stale_skips_if_synced_today(tmp_path, monkeypatch):
    """auto_sync_if_stale does not call run_sync if already synced today."""
    import bosch_flow_mcp.db as db_module

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(db_path))
    conn = db_module.get_db(db_path)
    db_module.log_sync(conn, "batteries", "ok", 0)
    conn.close()

    with patch("bosch_flow_mcp.tools.sync_tools.run_sync") as mock_run:
        auto_sync_if_stale("batteries")
        mock_run.assert_not_called()


def test_auto_sync_if_stale_syncs_if_never_synced(tmp_path, monkeypatch):
    """auto_sync_if_stale calls run_sync if data type has never been synced."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(db_path))

    with patch("bosch_flow_mcp.tools.sync_tools.run_sync") as mock_run:
        mock_run.return_value = {"batteries": {"status": "ok", "records": 0}}
        auto_sync_if_stale("batteries")
        mock_run.assert_called_once_with(["batteries"])


def test_auto_sync_if_stale_swallows_errors(tmp_path, monkeypatch):
    """auto_sync_if_stale does not propagate exceptions."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(db_path))

    with patch("bosch_flow_mcp.tools.sync_tools.run_sync", side_effect=RuntimeError("auth error")):
        auto_sync_if_stale("batteries")  # Should not raise


class TestAutoSyncKeepsItsFailuresToItself:
    """A read tool calls this first and queries the cache whatever happens here."""

    @pytest.mark.parametrize(
        "failure",
        [sqlite3.OperationalError("database is locked"), PermissionError(13, "Permission denied")],
        ids=["lock", "oserror"],
    )
    def test_a_failure_while_opening_the_cache_does_not_escape(self, failure):
        with (
            patch.object(sync_tools.db, "get_db", side_effect=failure),
            patch.object(sync_tools, "run_sync") as mock_run,
        ):
            auto_sync_if_stale("batteries")
        mock_run.assert_not_called()

    def test_a_file_that_is_not_a_database_does_not_escape(self, tmp_path, monkeypatch):
        not_a_database = tmp_path / "not-a-database.db"
        not_a_database.write_bytes(b"not a database file " * 100)
        monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(not_a_database))
        with patch.object(sync_tools, "run_sync") as mock_run:
            auto_sync_if_stale("batteries")
        mock_run.assert_not_called()

    def test_a_malformed_sync_time_does_not_escape(self, tmp_path, monkeypatch):
        import bosch_flow_mcp.db as db_module

        db_path = tmp_path / "test.db"
        conn = db_module.get_db(db_path)
        conn.execute(
            "INSERT INTO sync_log (synced_at, data_type, status) VALUES (?, ?, ?)",
            ("not-a-date", "batteries", "ok"),
        )
        conn.commit()
        conn.close()
        monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(db_path))
        with patch.object(sync_tools, "run_sync") as mock_run:
            auto_sync_if_stale("batteries")
        mock_run.assert_not_called()

    def test_a_lock_while_reading_the_last_sync_closes_the_connection(self):
        conn = MagicMock()
        with (
            patch.object(sync_tools.db, "get_db", return_value=conn),
            patch.object(
                sync_tools.db,
                "get_last_sync_time",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
        ):
            auto_sync_if_stale("batteries")
        conn.close.assert_called_once_with()

    def test_the_connection_is_closed_before_the_sync_opens_its_own(self):
        conn = MagicMock()
        closed_when_the_sync_began = []
        with (
            patch.object(sync_tools.db, "get_db", return_value=conn),
            patch.object(sync_tools.db, "get_last_sync_time", return_value=None),
            patch.object(
                sync_tools,
                "run_sync",
                side_effect=lambda _types: closed_when_the_sync_began.append(conn.close.called),
            ),
        ):
            auto_sync_if_stale("batteries")
        assert closed_when_the_sync_began == [True]

    async def test_a_read_tool_still_answers_from_the_cache(self, tmp_path, monkeypatch):
        import bosch_flow_mcp.db as db_module
        from bosch_flow_mcp.tools.bike_tools import bosch_get_bikes

        db_path = tmp_path / "test.db"
        conn = db_module.get_db(db_path)
        db_module.save_bike(conn, FAKE_BIKE_ID, {"name": "Cached"})
        conn.commit()
        conn.close()
        monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(db_path))

        opens = [sqlite3.OperationalError("database is locked"), db_module.get_db(db_path)]
        with patch.object(db_module, "get_db", side_effect=opens):
            data = json.loads(await bosch_get_bikes())
        assert [bike["bike_id"] for bike in data["bikes"]] == [FAKE_BIKE_ID]


def test_auto_sync_skips_after_unavailable_today(tmp_path, monkeypatch):
    """An 'unavailable' result counts as 'checked today' - no retry spam."""
    import bosch_flow_mcp.db as db_module

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(db_path))
    conn = db_module.get_db(db_path)
    db_module.log_sync(conn, "service", "unavailable", 0, "needs euda")
    conn.close()

    with patch("bosch_flow_mcp.tools.sync_tools.run_sync") as mock_run:
        auto_sync_if_stale("service")
        mock_run.assert_not_called()


def test_auto_sync_retries_after_error_today(tmp_path, monkeypatch):
    """An 'error' is transient and must be retried on the next read."""
    import bosch_flow_mcp.db as db_module

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(db_path))
    conn = db_module.get_db(db_path)
    db_module.log_sync(conn, "batteries", "error", 0, "boom")
    conn.close()

    with patch("bosch_flow_mcp.tools.sync_tools.run_sync") as mock_run:
        mock_run.return_value = {"batteries": {"status": "ok", "records": 0}}
        auto_sync_if_stale("batteries")
        mock_run.assert_called_once_with(["batteries"])


def test_an_auth_failure_is_recorded_in_the_sync_log(tmp_path):
    """An auth failure must leave a row behind.

    A dead token will not clear itself, so it is the failure worth finding later.
    """
    import os

    import bosch_flow_mcp.db as db_module
    from bosch_flow_mcp import api

    os.environ["BOSCH_FLOW_MCP_DB_PATH"] = str(tmp_path / "test.db")

    with patch(
        "bosch_flow_mcp.tools.sync_tools.api.get",
        side_effect=api.BoschAuthError("token rejected for /v1/x?serialNumber=SN-SECRET"),
    ):
        results = run_sync(["bikes"])

    assert results["bikes"]["status"] == "auth_error"

    conn = db_module.get_db(tmp_path / "test.db")
    rows = conn.execute("SELECT data_type, status, notes FROM sync_log").fetchall()
    conn.close()
    assert [(r["data_type"], r["status"]) for r in rows] == [("bikes", "auth_error")]
    # Fixed text, not the exception's: this row is re-served to the model.
    assert rows[0]["notes"] == sync_tools.AUTH_FAILED_MSG
    assert "SN-SECRET" not in json.dumps([results, rows[0]["notes"]])


def test_a_response_body_never_reaches_the_sync_log_or_a_tool(tmp_path, monkeypatch):
    """These notes are stored, then re-served to the model.

    empty_data_note reads the last row back and returns it as `note` on every
    empty get-tool result, so a Bosch error body quoting an account or a
    serial would be handed to the model on every later read.
    """
    import io
    import urllib.error

    import bosch_flow_mcp.db as db_module
    from bosch_flow_mcp import api
    from bosch_flow_mcp.helpers import empty_data_note

    monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(tmp_path / "test.db"))
    secret = '{"trace": "user=rider@example.invalid serial=FRAME999 token=eyJhbGciOi"}'

    def raising_get(*a, **k):
        error = urllib.error.HTTPError(
            "https://example.invalid", 500, "boom", {}, io.BytesIO(secret.encode())
        )
        raise api.BoschAPIError(f"API error {error.code} for /v1/bike-profile")

    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=raising_get):
        run_sync(["bikes"])

    conn = db_module.get_db(tmp_path / "test.db")
    notes = [r["notes"] for r in conn.execute("SELECT notes FROM sync_log").fetchall()]
    surfaced = empty_data_note(conn, "bikes")
    conn.close()

    assert notes and notes[0]
    for fragment in ("rider@example.invalid", "FRAME999", "eyJhbGciOi"):
        assert all(fragment not in (note or "") for note in notes)
        assert fragment not in str(surfaced)


def test_the_api_layer_never_puts_a_response_body_in_its_message(monkeypatch):
    """The source of the note above: api.get builds what run_sync stores."""
    import io
    import urllib.error

    import pytest

    from bosch_flow_mcp import api

    secret = '{"trace": "user=rider@example.invalid serial=FRAME999"}'
    monkeypatch.setattr(api, "refresh_token", lambda: "tok")
    monkeypatch.setattr(
        api.urllib.request,
        "urlopen",
        MagicMock(
            side_effect=urllib.error.HTTPError(
                "https://example.invalid", 500, "boom", {}, io.BytesIO(secret.encode())
            )
        ),
    )

    with pytest.raises(api.BoschAPIError) as exc_info:
        api.get("/v1/bike-profile")

    message = str(exc_info.value)
    assert "rider@example.invalid" not in message
    assert "FRAME999" not in message
    assert "500" in message


class TestNoRequestPathReachesTheSyncLogOrAModel:
    """sync_log keeps these, and empty_data_note serves them back.

    The capacity request path carries a part number and a battery serial, so
    the exception's own message is not something to store or repeat.
    """

    def _sync_with(self, exc, tmp_path, monkeypatch, dtype="bikes"):
        import bosch_flow_mcp.db as db_module
        from bosch_flow_mcp.helpers import empty_data_note

        monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(tmp_path / "test.db"))
        with (
            patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=exc),
            patch("bosch_flow_mcp.tools.sync_tools.auth.token_is_euda", return_value=True),
        ):
            results = run_sync([dtype])

        conn = db_module.get_db(tmp_path / "test.db")
        rows = [(r["status"], r["notes"]) for r in conn.execute("SELECT * FROM sync_log")]
        note = empty_data_note(conn, dtype)
        conn.close()
        return results, rows, note

    def test_an_api_failure_records_no_path(self, tmp_path, monkeypatch):
        from bosch_flow_mcp import api

        secret = (
            "Forbidden (403) for /diagnosis-field-data/capacity-testers"
            "?partNumber=PART-SECRET-123&serialNumber=SERIAL-SECRET-456"
        )
        results, rows, note = self._sync_with(
            api.BoschForbiddenError(secret), tmp_path, monkeypatch
        )

        # Non-vacuous: the sync must actually have failed, or absence of the
        # fragments says nothing.
        assert results["bikes"]["status"] == "error"
        assert rows and rows[0][0] == "error"
        assert note.get("data_status") == "error"

        blob = json.dumps([results, rows, note])
        for fragment in ("PART-SECRET-123", "SERIAL-SECRET-456", "partNumber", "capacity-testers"):
            assert fragment not in blob

    def test_a_rate_limit_leaves_a_row_instead_of_taking_the_run_out(self, tmp_path, monkeypatch):
        from bosch_flow_mcp import api

        results, rows, _ = self._sync_with(
            api.BoschRateLimitError("Rate limited on /v1/bike-profile"), tmp_path, monkeypatch
        )

        assert results["bikes"]["status"] == "rate_limited"
        assert rows and rows[0][0] == "error"

    def test_a_database_that_cannot_take_the_row_does_not_end_the_run(self, tmp_path, monkeypatch):
        """Writing a sync_log row is itself a database write.

        A locked database when recording a failure, as with a CLI sync alongside
        an MCP session, must not stop the remaining types. The other outcomes
        are in TestASyncLogRowThatCannotBeWritten.
        """
        from bosch_flow_mcp import api
        from bosch_flow_mcp.tools import sync_tools as st

        monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(tmp_path / "test.db"))
        attempted = []

        def failing_get(path, base=None, retries=3):
            attempted.append(path)
            raise api.BoschAuthError("token rejected")

        with (
            patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=failing_get),
            patch.object(st.db, "log_sync", side_effect=RuntimeError("database is locked")),
        ):
            results = run_sync(["bikes", "batteries"])

        # Both types attempted, both reported, nothing escaped.
        assert results["bikes"]["status"] == "auth_error"
        assert "batteries" in results
        assert len(attempted) >= 2

    def test_an_unanticipated_failure_leaves_a_row(self, tmp_path, monkeypatch):
        results, rows, _ = self._sync_with(KeyError("/etc/secret/path"), tmp_path, monkeypatch)

        assert results["bikes"]["status"] == "error"
        assert rows and rows[0][0] == "error"
        assert "/etc/secret/path" not in json.dumps([results, rows])


class TestASyncLogRowThatCannotBeWritten:
    """A type that finishes has committed its data before its row is written.

    So a locked database at that point loses that row and not the data: the
    result still says what happened, and the remaining types still run. The log
    shows the type's previous outcome until a later row lands, and auto-sync
    syncs the type again on the next read unless a settled row from earlier
    today is already there.
    """

    def _sync(self, types, tmp_path, monkeypatch, is_euda=False, get=_mock_api_get):
        from bosch_flow_mcp.tools import sync_tools as st

        monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(tmp_path / "test.db"))
        with (
            patch.object(st.api, "get", side_effect=get),
            patch.object(st.auth, "token_is_euda", return_value=is_euda),
            patch.object(
                st.db, "log_sync", side_effect=sqlite3.OperationalError("database is locked")
            ),
        ):
            return run_sync(types)

    def test_a_successful_sync_keeps_its_result_and_the_next_type_runs(self, tmp_path, monkeypatch):
        import bosch_flow_mcp.db as db_module

        results = self._sync(["bikes", "batteries"], tmp_path, monkeypatch)

        assert results["bikes"] == {"status": "ok", "records": 1}
        assert results["batteries"] == {"status": "ok", "records": 1}
        conn = db_module.get_db(tmp_path / "test.db")
        stored = [bike["bike_id"] for bike in db_module.query_bikes(conn)]
        conn.close()
        assert stored == [FAKE_BIKE_ID]

    @pytest.mark.parametrize(
        "dtype,table,seed",
        [
            ("bikes", "bikes", None),
            ("batteries", "batteries", "bike"),
            ("components", "components", None),
            ("service", "service_records", "bike"),
            ("software_updates", "software_updates", "bike"),
            ("capacity", "capacity_tests", "battery"),
        ],
    )
    def test_every_sync_function_commits_its_own_data(
        self, dtype, table, seed, tmp_path, monkeypatch
    ):
        """run_sync closes its connection, which discards anything uncommitted,
        and with no row written nothing else commits."""
        import bosch_flow_mcp.db as db_module

        conn = db_module.get_db(tmp_path / "test.db")
        if seed == "bike":
            db_module.save_bike(conn, FAKE_BIKE_ID, {"name": "Seeded"})
        if seed == "battery":
            battery = {"partNumber": "TESTPART003", "serialNumber": "BATTSN001"}
            db_module.save_component(conn, FAKE_BIKE_ID, "battery", battery)
        conn.commit()
        conn.close()

        def get(path, base=None, retries=3):
            if "service-records" in path:
                return [{"id": "SVC1", "date": "2026-03-15"}]
            if "installation-reports" in path:
                return [{"installedAt": "2026-03-20T12:00:00Z", "component": "driveUnit"}]
            if "capacity-testers" in path:
                return [{"testDate": "2026-03-10"}]
            return _mock_api_get(path, base, retries)

        results = self._sync([dtype], tmp_path, monkeypatch, is_euda=True, get=get)

        assert results[dtype] == {"status": "ok", "records": 1}
        conn = db_module.get_db(tmp_path / "test.db")
        held = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        conn.close()
        assert held == 1

    @pytest.mark.parametrize(
        "row_written,held", [(True, 1), (False, 0)], ids=["row-written", "row-refused"]
    )
    def test_a_failure_part_way_through_a_type_keeps_what_it_saved_only_with_its_row(
        self, row_written, held, tmp_path, monkeypatch
    ):
        """The sync functions commit at their end, so the failure row's commit
        is what persists an earlier bike's snapshot."""
        import bosch_flow_mcp.db as db_module
        from bosch_flow_mcp import api
        from bosch_flow_mcp.tools import sync_tools as st
        from tests.conftest import FAKE_BIKE_ID_2

        conn = db_module.get_db(tmp_path / "test.db")
        for bike_id in (FAKE_BIKE_ID, FAKE_BIKE_ID_2):
            db_module.save_bike(conn, bike_id, {"name": "Seeded"})
        conn.commit()
        conn.close()
        monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(tmp_path / "test.db"))
        if not row_written:
            monkeypatch.setattr(
                st.db, "log_sync", MagicMock(side_effect=sqlite3.OperationalError("locked"))
            )

        def get(path, base=None, retries=3):
            if FAKE_BIKE_ID_2 in path:
                raise api.BoschAPIError("boom")
            return FAKE_BIKE_DETAIL_RESPONSE

        with (
            patch.object(st.api, "get", side_effect=get),
            patch.object(st.auth, "token_is_euda", return_value=False),
        ):
            results = run_sync(["batteries"])

        assert results["batteries"]["status"] == "error"
        conn = db_module.get_db(tmp_path / "test.db")
        count = conn.execute("SELECT COUNT(*) FROM batteries").fetchone()[0]
        conn.close()
        assert count == held

    def test_a_refused_commit_releases_the_lock(self, tmp_path, caplog):
        """A reader holding the file refuses the commit, and the insert before
        it has taken the write lock. Left open, that lock stops every other
        writer, and the row goes out with whatever this connection commits next."""
        import bosch_flow_mcp.db as db_module
        from bosch_flow_mcp.tools import sync_tools as st

        path = tmp_path / "test.db"
        conn = db_module.get_db(path)
        conn.execute("PRAGMA busy_timeout = 0")
        reader = sqlite3.connect(path)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM sync_log").fetchall()

        st._write_row(conn, "bikes", "ok", 1)

        reader.rollback()
        reader.close()
        assert [record.getMessage() for record in caplog.records] == [
            "Could not record the sync of bikes (OperationalError)"
        ]
        other = sqlite3.connect(path, timeout=0)
        other.execute("BEGIN IMMEDIATE")
        other.rollback()
        other.close()
        st._write_row(conn, "batteries", "ok", 2)
        rows = [row["data_type"] for row in conn.execute("SELECT data_type FROM sync_log")]
        conn.close()
        assert rows == ["batteries"]

    def test_a_rollback_that_fails_does_not_end_the_run(self):
        from bosch_flow_mcp.tools import sync_tools as st

        conn = MagicMock()
        conn.rollback.side_effect = sqlite3.OperationalError("disk I/O error")
        with patch.object(
            st.db, "log_sync", side_effect=sqlite3.OperationalError("database is locked")
        ):
            st._write_row(conn, "bikes", "ok", 1)
        conn.rollback.assert_called_once_with()

    def test_an_unavailable_type_keeps_its_result_and_the_next_type_runs(
        self, tmp_path, monkeypatch
    ):
        results = self._sync(["service", "bikes"], tmp_path, monkeypatch)

        assert results["service"] == {
            "status": "unavailable",
            "records": 0,
            "code": "requires_euda",
            "message": sync_tools.REQUIRES_EUDA_MSG,
        }
        assert results["bikes"] == {"status": "ok", "records": 1}

    def test_a_euda_empty_result_keeps_its_result_and_the_next_type_runs(
        self, tmp_path, monkeypatch
    ):
        results = self._sync(
            ["bikes", "service"], tmp_path, monkeypatch, is_euda=True, get=lambda *a, **k: {}
        )

        expected = {
            "status": "empty",
            "records": 0,
            "code": "euda_empty",
            "message": sync_tools.EUDA_EMPTY_MSG,
        }
        assert results == {"bikes": expected, "service": expected}

    def test_the_lost_row_is_logged_by_type_and_not_by_the_exceptions_text(
        self, tmp_path, monkeypatch, caplog
    ):
        with caplog.at_level(logging.ERROR, logger="bosch_flow_mcp.tools.sync_tools"):
            self._sync(["bikes", "service"], tmp_path, monkeypatch)

        assert [record.getMessage() for record in caplog.records] == [
            "Could not record the sync of bikes (OperationalError)",
            "Could not record the sync of service (OperationalError)",
        ]
        assert all(record.exc_info is None for record in caplog.records)

    def test_a_locked_read_of_the_bikes_table_does_not_end_the_run(self, tmp_path, monkeypatch):
        """The euda-empty check reads the database after the sync function returns."""
        from bosch_flow_mcp.tools import sync_tools as st

        monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(tmp_path / "test.db"))
        with (
            patch.object(st.api, "get", return_value={}),
            patch.object(st.auth, "token_is_euda", return_value=True),
            patch.object(
                st.db,
                "query_bikes",
                side_effect=[sqlite3.OperationalError("database is locked"), [], [], []],
            ),
        ):
            results = run_sync(["bikes", "service"])

        assert results["bikes"]["status"] == "error"
        assert "OperationalError" in results["bikes"]["message"]
        assert results["service"]["code"] == "euda_empty"

    def test_a_locked_read_before_fetching_the_bike_list_does_not_end_the_run(
        self, tmp_path, monkeypatch
    ):
        from bosch_flow_mcp.tools import sync_tools as st

        monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(tmp_path / "test.db"))
        with (
            patch.object(st.api, "get", return_value={}),
            patch.object(st.auth, "token_is_euda", return_value=True),
            patch.object(
                st.db, "query_bikes", side_effect=sqlite3.OperationalError("database is locked")
            ),
        ):
            results = run_sync(["service", "components"])

        for dtype in ("service", "components"):
            assert results[dtype]["status"] == "error"
            assert "OperationalError" in results[dtype]["message"]

    def test_each_outcome_still_writes_its_row(self, tmp_path, monkeypatch):
        """The guard must not lose the rows auto-sync and empty_data_note read."""
        import bosch_flow_mcp.db as db_module
        from bosch_flow_mcp.tools import sync_tools as st

        def rows(types, name, is_euda, get):
            monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(tmp_path / name))
            with (
                patch.object(st.api, "get", side_effect=get),
                patch.object(st.auth, "token_is_euda", return_value=is_euda),
            ):
                run_sync(types)
            conn = db_module.get_db(tmp_path / name)
            found = conn.execute(
                "SELECT data_type, status, records_added, notes FROM sync_log ORDER BY id"
            ).fetchall()
            conn.close()
            return [tuple(row) for row in found]

        assert rows(["bikes", "service"], "app.db", False, _mock_api_get) == [
            ("bikes", "ok", 1, ""),
            ("service", "unavailable", 0, sync_tools.REQUIRES_EUDA_MSG),
        ]
        assert rows(["bikes"], "euda.db", True, lambda *a, **k: {}) == [
            ("bikes", "empty", 0, sync_tools.EUDA_EMPTY_MSG),
        ]


class TestTheDispatchOrderSatisfiesItsDependencies:
    """A comment on the dispatch declares the order.

    AGENTS.md also declares components before capacity.

    A dict literal is exactly the thing someone tidies alphabetically.
    Capacity reads part and serial numbers from the components table and
    fetches them itself when it holds no battery, so a breach costs a second
    registrations request rather than the data.
    """

    def _run(self, order, db_name, tmp_path, monkeypatch):
        # The shared mock returns [] for capacity-testers, so under it capacity
        # records zero in either order.
        def with_capacity(path, base=None, retries=3):
            if "capacity-testers" in path:
                return [{"testDate": "2026-03-10", "capacity": 90}]
            if "registrations" in path:
                # The shared fixture's only component is a driveUnit, and
                # capacity reads battery components.
                return {
                    "registrations": [
                        {
                            "bikeId": FAKE_BIKE_ID,
                            "components": [
                                {
                                    "componentType": "battery",
                                    "partNumber": "TESTPART003",
                                    "serialNumber": "BATTSN001",
                                    "productName": "Test Battery",
                                    "softwareVersion": "1.0.0",
                                }
                            ],
                        }
                    ]
                }
            return _mock_api_get(path, base, retries)

        monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(tmp_path / db_name))
        with (
            patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=with_capacity),
            patch("bosch_flow_mcp.tools.sync_tools.auth.token_is_euda", return_value=True),
        ):
            return run_sync(order)

    def test_capacity_after_components_records_rows(self, tmp_path, monkeypatch):
        results = self._run(
            ["bikes", "components", "capacity"], "ordered.db", tmp_path, monkeypatch
        )
        assert results["components"]["records"] >= 1
        assert results["capacity"]["records"] >= 1

    def test_capacity_before_components_fetches_them_itself(self, tmp_path, monkeypatch):
        results = self._run(
            ["bikes", "capacity", "components"], "reversed.db", tmp_path, monkeypatch
        )
        assert results["capacity"]["records"] == 1
        assert results["components"]["records"] == 1

    def test_the_declared_order_holds(self):
        """So a reorder fails here rather than as repeated requests."""
        from bosch_flow_mcp.tools.sync_tools import _ALL_TYPES

        assert _ALL_TYPES[0] == "bikes", "the types that need bike ids read what this stores"
        assert _ALL_TYPES.index("components") < _ALL_TYPES.index("capacity"), (
            "capacity reads part and serial numbers written by components"
        )
