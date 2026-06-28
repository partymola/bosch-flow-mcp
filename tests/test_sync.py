"""Tests for sync orchestration."""

from unittest.mock import patch

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
