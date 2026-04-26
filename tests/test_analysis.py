"""Tests for battery trend analysis."""

import json
import pytest
from unittest.mock import patch

from tests.conftest import FAKE_BIKE_ID


@pytest.fixture(autouse=True)
def require_auth_bypass(monkeypatch, tmp_path):
    tokens_path = tmp_path / "tokens.json"
    tokens_path.write_text(json.dumps({"access_token": "fake", "expiry": 9999999999}))
    monkeypatch.setattr("bosch_flow_mcp.helpers.BOSCH_TOKENS_PATH", tokens_path)


@pytest.fixture(autouse=True)
def patch_auto_sync():
    with patch("bosch_flow_mcp.tools.analysis_tools.auto_sync_if_stale"):
        yield


@pytest.fixture(autouse=True)
def patch_db_path(populated_db, tmp_path, monkeypatch):
    import bosch_flow_mcp.db as db_module
    db_file = tmp_path / "test_analysis.db"
    conn = db_module.get_db(db_file)
    for table in ["batteries", "bikes", "sync_log"]:
        rows = populated_db.execute(f"SELECT * FROM {table}").fetchall()
        if rows:
            cols = [d[0] for d in populated_db.execute(f"SELECT * FROM {table} LIMIT 0").description]
            placeholders = ",".join(["?"] * len(cols))
            conn.executemany(
                f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
                [tuple(r) for r in rows],
            )
    conn.commit()
    conn.close()
    monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(db_file))
    yield


async def test_battery_trends_monthly():
    from bosch_flow_mcp.tools.analysis_tools import bosch_battery_trends
    result = await bosch_battery_trends(period="monthly")
    data = json.loads(result)
    assert "trends" in data
    assert data["period"] == "monthly"
    assert len(data["trends"]) > 0
    # Should have both March and April entries
    periods = {t["period"] for t in data["trends"]}
    assert "2026-03" in periods
    assert "2026-04" in periods


async def test_battery_trends_weekly():
    from bosch_flow_mcp.tools.analysis_tools import bosch_battery_trends
    result = await bosch_battery_trends(period="weekly")
    data = json.loads(result)
    assert len(data["trends"]) > 1  # Multiple weeks


async def test_battery_trends_shows_cycle_growth():
    from bosch_flow_mcp.tools.analysis_tools import bosch_battery_trends
    result = await bosch_battery_trends(period="monthly")
    data = json.loads(result)
    # Each period should show charge cycle growth
    for trend in data["trends"]:
        if trend["charge_cycles_added"] is not None:
            assert trend["charge_cycles_added"] >= 0


async def test_battery_trends_filtered_by_bike():
    from bosch_flow_mcp.tools.analysis_tools import bosch_battery_trends
    result = await bosch_battery_trends(bike_id=FAKE_BIKE_ID)
    data = json.loads(result)
    assert data["bike_id"] == FAKE_BIKE_ID
    for t in data["trends"]:
        assert t["bike_id"] == FAKE_BIKE_ID


async def test_battery_trends_no_data(tmp_path, monkeypatch):
    """Returns helpful message when no data is in the cache."""
    import bosch_flow_mcp.db as db_module
    db_file = tmp_path / "empty.db"
    db_module.get_db(db_file).close()
    monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(db_file))

    from bosch_flow_mcp.tools.analysis_tools import bosch_battery_trends
    result = await bosch_battery_trends()
    data = json.loads(result)
    assert "message" in data
    assert "bosch_sync" in data["message"]
