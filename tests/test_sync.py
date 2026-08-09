"""Tests for sync orchestration."""

import json
from unittest.mock import MagicMock, patch

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
    """Every failure run_sync swallows must leave a row behind.

    The API-error branch logged and the auth branch did not, so the one
    failure that will not clear itself was the one leaving no trace: the run
    reported it once and afterwards queries carried on serving the cache with
    nothing saying why it had stopped growing.
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
        """Writing the row is itself a database write.

        A locked database - a CLI sync alongside an MCP session - raised
        here, escaping the very catch-all that exists to keep the remaining
        types going, so the rest of the sync was never attempted.
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
