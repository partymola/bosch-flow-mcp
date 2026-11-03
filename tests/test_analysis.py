"""Tests for battery trend analysis."""

import json
from unittest.mock import patch

import pytest

from tests.conftest import FAKE_BATTERY_ID, FAKE_BIKE_ID


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


async def test_battery_trends_include_the_end_date():
    from bosch_flow_mcp.tools.analysis_tools import bosch_battery_trends

    result = await bosch_battery_trends(start_date="2026-04-07", end_date="2026-04-07")
    data = json.loads(result)
    assert data["data_points"] == 1
    assert [t["period"] for t in data["trends"]] == ["2026-04"]


async def test_battery_trends_with_only_an_end_date_go_back_to_the_first():
    from bosch_flow_mcp import db
    from bosch_flow_mcp.tools.analysis_tools import bosch_battery_trends

    conn = db.get_db()
    db.save_battery_snapshot(
        conn, FAKE_BIKE_ID, {"battery_id": FAKE_BATTERY_ID}, "2020-01-15T08:00:00+00:00"
    )
    conn.commit()
    conn.close()

    result = await bosch_battery_trends(end_date="2026-03-31")
    assert [t["period"] for t in json.loads(result)["trends"]] == ["2020-01", "2026-03"]


async def test_battery_trends_no_data(tmp_path, monkeypatch):
    """Returns helpful message when no data is in the cache."""
    import bosch_flow_mcp.db as db_module

    db_file = tmp_path / "empty.db"
    db_module.get_db(db_file).close()
    monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(db_file))

    from bosch_flow_mcp.tools.analysis_tools import bosch_battery_trends

    result = await bosch_battery_trends()
    data = json.loads(result)
    assert data["message"] == "No battery data found. Run bosch_sync first."


@pytest.mark.parametrize(
    "dates",
    [
        {"start_date": "2026-05"},
        {"start_date": "2026-03-20", "end_date": "2026-03-21"},
        {"end_date": "2020-01"},
    ],
    ids=["start-only", "both", "end-only"],
)
async def test_a_range_with_no_data_says_so_rather_than_to_sync(dates):
    """A sync adds nothing to a range the cache already covers."""
    from bosch_flow_mcp.tools.analysis_tools import bosch_battery_trends

    data = json.loads(await bosch_battery_trends(**dates))
    assert data["message"] == "No battery data in that date range."
    assert data["trends"] == []


@pytest.mark.parametrize("dates", [{}, {"start_date": "30d"}], ids=["no-range", "range"])
async def test_an_empty_cache_says_why_the_last_sync_left_it_empty(tmp_path, monkeypatch, dates):
    """A range says the range is empty, so without this a failed sync goes unmentioned."""
    from bosch_flow_mcp import db
    from bosch_flow_mcp.tools.analysis_tools import bosch_battery_trends

    db_file = tmp_path / "empty.db"
    conn = db.get_db(db_file)
    db.log_sync(conn, "batteries", "auth_error", 0, "Run: bosch-flow-mcp auth")
    conn.close()
    monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(db_file))

    data = json.loads(await bosch_battery_trends(**dates))
    assert data["data_status"] == "auth_error"
    assert data["note"] == "Run: bosch-flow-mcp auth"
    assert data["trends"] == []


async def test_an_empty_range_adds_no_note_after_a_good_sync():
    from bosch_flow_mcp.tools.analysis_tools import bosch_battery_trends

    data = json.loads(await bosch_battery_trends(start_date="2026-05"))
    assert data["trends"] == []
    assert "note" not in data
