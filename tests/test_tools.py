"""Tests for MCP tool output formatting."""

import json
from unittest.mock import patch

import pytest

from tests.conftest import FAKE_BIKE_ID


@pytest.fixture(autouse=True)
def require_auth_bypass(monkeypatch, tmp_path):
    """Make require_auth pass by creating a fake tokens file."""
    tokens_path = tmp_path / "tokens.json"
    tokens_path.write_text(json.dumps({"access_token": "fake", "expiry": 9999999999}))
    monkeypatch.setattr("bosch_flow_mcp.helpers.BOSCH_TOKENS_PATH", tokens_path)
    monkeypatch.setattr(
        "bosch_flow_mcp.tools.battery_tools.BOSCH_TOKENS_PATH", tokens_path, raising=False
    )


@pytest.fixture(autouse=True)
def patch_auto_sync():
    """Disable auto-sync in tool tests (use populated_db fixture instead)."""
    with patch("bosch_flow_mcp.tools.bike_tools.auto_sync_if_stale"):
        with patch("bosch_flow_mcp.tools.battery_tools.auto_sync_if_stale"):
            with patch("bosch_flow_mcp.tools.component_tools.auto_sync_if_stale"):
                with patch("bosch_flow_mcp.tools.service_tools.auto_sync_if_stale"):
                    with patch("bosch_flow_mcp.tools.analysis_tools.auto_sync_if_stale"):
                        yield


@pytest.fixture(autouse=True)
def patch_db_path(populated_db, tmp_path, monkeypatch):
    """Redirect db.get_db() to the populated test DB."""
    import bosch_flow_mcp.db as db_module

    original_get_db = db_module.get_db

    def _fake_get_db(db_path=None):
        return original_get_db(
            db_path or populated_db.execute("PRAGMA database_list").fetchone()[2]
        )

    # Instead, patch get_db to return the already-open populated_db connection
    # We need a fresh connection to the same file each time
    db_file = tmp_path / "test_tools.db"
    # Populate a fresh DB at db_file
    conn = db_module.get_db(db_file)
    # Copy data from populated_db
    for table in [
        "bikes",
        "batteries",
        "components",
        "service_records",
        "software_updates",
        "capacity_tests",
        "sync_log",
    ]:
        rows = populated_db.execute(f"SELECT * FROM {table}").fetchall()
        if rows:
            cols = [
                d[0] for d in populated_db.execute(f"SELECT * FROM {table} LIMIT 0").description
            ]
            placeholders = ",".join(["?"] * len(cols))
            conn.executemany(
                f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
                [tuple(r) for r in rows],
            )
    conn.commit()
    conn.close()

    monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(db_file))
    yield


async def test_bosch_get_bikes_returns_list():
    from bosch_flow_mcp.tools.bike_tools import bosch_get_bikes

    result = await bosch_get_bikes()
    data = json.loads(result)
    assert "bikes" in data
    assert data["count"] == 1
    assert data["bikes"][0]["bike_id"] == FAKE_BIKE_ID
    # raw_json should be stripped
    assert "raw_json" not in data["bikes"][0]


async def test_bosch_get_bike_found():
    from bosch_flow_mcp.tools.bike_tools import bosch_get_bike

    result = await bosch_get_bike(FAKE_BIKE_ID)
    data = json.loads(result)
    assert data["bike_id"] == FAKE_BIKE_ID


async def test_bosch_get_bike_not_found():
    from bosch_flow_mcp.tools.bike_tools import bosch_get_bike

    result = await bosch_get_bike("00000000-0000-0000-0000-999999999999")
    data = json.loads(result)
    assert "error" in data


async def test_bosch_get_batteries_latest():
    from bosch_flow_mcp.tools.battery_tools import bosch_get_batteries

    result = await bosch_get_batteries(latest_only=True)
    data = json.loads(result)
    assert "batteries" in data
    assert data["count"] >= 1
    # Should return the most recent snapshot
    assert data["batteries"][0]["charge_cycles_total"] == 15


async def test_bosch_get_batteries_history():
    from bosch_flow_mcp.tools.battery_tools import bosch_get_batteries

    result = await bosch_get_batteries(
        latest_only=False, start_date="2026-03-01", end_date="2026-04-30"
    )
    data = json.loads(result)
    assert data["count"] > 1  # Multiple snapshots over time


async def test_bosch_get_components_all():
    from bosch_flow_mcp.tools.component_tools import bosch_get_components

    result = await bosch_get_components()
    data = json.loads(result)
    assert "components" in data
    assert data["count"] >= 1
    assert "raw_json" not in data["components"][0]


async def test_bosch_get_service_records():
    from bosch_flow_mcp.tools.service_tools import bosch_get_service_records

    result = await bosch_get_service_records()
    data = json.loads(result)
    assert "service_records" in data
    assert data["count"] == 1
    assert data["service_records"][0]["description"] == "Annual service"


async def test_bosch_get_software_updates():
    from bosch_flow_mcp.tools.service_tools import bosch_get_software_updates

    result = await bosch_get_software_updates()
    data = json.loads(result)
    assert "software_updates" in data
    assert data["count"] == 1
    assert data["software_updates"][0]["from_version"] == "4.5.5"


async def test_bosch_get_capacity():
    from bosch_flow_mcp.tools.battery_tools import bosch_get_capacity

    result = await bosch_get_capacity()
    data = json.loads(result)
    assert "capacity_tests" in data
    assert data["count"] == 1


async def test_soc_forbidden_is_graceful():
    """A euda token 403s the mobile soc endpoint -> a client-aware hint, not a crash."""
    from bosch_flow_mcp.tools import battery_tools

    with patch(
        "bosch_flow_mcp.tools.battery_tools.api.get",
        side_effect=battery_tools.api.BoschForbiddenError("403"),
    ):
        data = json.loads(await battery_tools.bosch_get_soc(FAKE_BIKE_ID))
    assert "error" in data
    assert "one-bike-app" in data["hint"]


async def test_soc_none_keeps_offline_message():
    """Regression: a None response (404/offline) still gives the offline hint."""
    from bosch_flow_mcp.tools import battery_tools

    with patch("bosch_flow_mcp.tools.battery_tools.api.get", return_value=None):
        data = json.loads(await battery_tools.bosch_get_soc(FAKE_BIKE_ID))
    assert "error" in data
    assert "offline" in data["hint"].lower() or "ConnectModule" in data["hint"]
