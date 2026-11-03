"""Tests for MCP tool output formatting."""

import json
from datetime import date, datetime
from unittest.mock import patch

import pytest

from tests.conftest import FAKE_BATTERY_ID, FAKE_BIKE_ID


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


async def test_a_months_battery_history_includes_its_last_day():
    from bosch_flow_mcp import db
    from bosch_flow_mcp.tools.battery_tools import bosch_get_batteries

    last_day = "2026-04-30T08:00:00.123456+00:00"
    conn = db.get_db()
    db.save_battery_snapshot(conn, FAKE_BIKE_ID, {"battery_id": FAKE_BATTERY_ID}, last_day)
    conn.commit()
    conn.close()

    result = await bosch_get_batteries(latest_only=False, start_date="2026-04", end_date="2026-04")
    data = json.loads(result)
    assert [b["captured_at"] for b in data["batteries"]] == [
        "2026-04-01T08:00:00+00:00",
        "2026-04-07T08:00:00+00:00",
        last_day,
    ]


async def test_a_day_count_includes_today():
    from bosch_flow_mcp import db
    from bosch_flow_mcp.tools.battery_tools import bosch_get_batteries

    today = f"{date.today().isoformat()}T08:00:00.123456+00:00"
    conn = db.get_db()
    db.save_battery_snapshot(conn, FAKE_BIKE_ID, {"battery_id": FAKE_BATTERY_ID}, today)
    conn.commit()
    conn.close()

    result = await bosch_get_batteries(latest_only=False, start_date="1d")
    assert [b["captured_at"] for b in json.loads(result)["batteries"]] == [today]


async def test_a_single_day_of_software_updates_includes_that_day():
    from bosch_flow_mcp.tools.service_tools import bosch_get_software_updates

    result = await bosch_get_software_updates(start_date="2026-03-20", end_date="2026-03-20")
    assert json.loads(result)["count"] == 1


async def test_battery_history_with_only_an_end_date_counts_back_from_it():
    from bosch_flow_mcp import db
    from bosch_flow_mcp.tools.battery_tools import bosch_get_batteries

    # Exactly 30 days before the end date, and the moment before that day.
    conn = db.get_db()
    for stamp in ("2026-02-12T23:59:59+00:00", "2026-02-13T00:00:00+00:00"):
        db.save_battery_snapshot(conn, FAKE_BIKE_ID, {"battery_id": FAKE_BATTERY_ID}, stamp)
    conn.commit()
    conn.close()

    result = await bosch_get_batteries(latest_only=False, end_date="2026-03-15")
    assert [b["captured_at"] for b in json.loads(result)["batteries"]] == [
        "2026-02-13T00:00:00+00:00",
        "2026-03-01T08:00:00+00:00",
        "2026-03-08T08:00:00+00:00",
        "2026-03-15T08:00:00+00:00",
    ]


async def test_activities_with_only_an_end_date_count_back_from_it():
    from bosch_flow_mcp.tools.activity_tools import bosch_get_activities

    def ride(day):
        started = datetime.fromisoformat(f"{day}T12:00:00+00:00").timestamp()
        return {"id": day, "attributes": {"startTime": int(started)}}

    days = ["2026-02-28", "2026-03-01", "2026-03-31", "2026-04-01"]
    with patch(
        "bosch_flow_mcp.tools.activity_tools.api.get",
        return_value={"data": [ride(day) for day in days]},
    ):
        data = json.loads(await bosch_get_activities(end_date="2026-03-31"))
    assert data["window"] == {"start": "2026-03-01", "end": "2026-03-31"}
    assert [a["id"] for a in data["activities"]] == ["2026-03-31", "2026-03-01"]


async def test_service_records_with_only_an_end_date_go_back_to_the_first():
    from bosch_flow_mcp import db
    from bosch_flow_mcp.tools.service_tools import bosch_get_service_records

    conn = db.get_db()
    db.save_service_record(conn, FAKE_BIKE_ID, {"id": "SVC-OLD", "date": "2020-01-15"})
    conn.commit()
    conn.close()

    result = await bosch_get_service_records(end_date="2026-03-31")
    assert [r["record_id"] for r in json.loads(result)["service_records"]] == ["SVC001", "SVC-OLD"]
    result = await bosch_get_service_records(end_date="2026-03-01")
    assert [r["record_id"] for r in json.loads(result)["service_records"]] == ["SVC-OLD"]


async def test_software_updates_with_only_an_end_date_go_back_to_the_first():
    from bosch_flow_mcp import db
    from bosch_flow_mcp.tools.service_tools import bosch_get_software_updates

    old = "2020-01-15T12:00:00Z"
    conn = db.get_db()
    db.save_software_update(conn, FAKE_BIKE_ID, {"installedAt": old, "component": "driveUnit"})
    conn.commit()
    conn.close()

    result = await bosch_get_software_updates(end_date="2026-03-31")
    assert [u["installed_at"] for u in json.loads(result)["software_updates"]] == [
        "2026-03-20T12:00:00Z",
        old,
    ]
    result = await bosch_get_software_updates(end_date="2026-03-01")
    assert [u["installed_at"] for u in json.loads(result)["software_updates"]] == [old]


async def _call_tool(tool, **args):
    """Through the server's registry, as a client reaches it, with no rides to serve."""
    from bosch_flow_mcp.mcp_instance import mcp
    from bosch_flow_mcp.tools import (  # noqa: F401
        activity_tools,
        analysis_tools,
        battery_tools,
        service_tools,
    )

    with patch("bosch_flow_mcp.tools.activity_tools.api.get", return_value={"data": []}):
        result = await mcp.call_tool(tool, args)
    return json.loads("".join(c.text for c in result.content))


RANGE_TOOLS = [
    ("bosch_get_batteries", {"latest_only": False}, "batteries"),
    ("bosch_battery_trends", {}, "trends"),
    ("bosch_get_service_records", {}, "service_records"),
    ("bosch_get_software_updates", {}, "software_updates"),
    ("bosch_get_activities", {}, "activities"),
]
RANGE_TOOL_IDS = [tool for tool, _, _ in RANGE_TOOLS]
AUTO_SYNCING_TOOLS = {
    "bosch_get_batteries": ("battery_tools", {"latest_only": False}),
    "bosch_battery_trends": ("analysis_tools", {}),
    "bosch_get_service_records": ("service_tools", {}),
    "bosch_get_software_updates": ("service_tools", {}),
}
ALL_RECORDS_TOOLS = {
    "bosch_battery_trends": lambda data: [t["period"] for t in data["trends"]],
    "bosch_get_service_records": lambda data: [
        r["service_date"][:7] for r in data["service_records"]
    ],
    "bosch_get_software_updates": lambda data: [
        u["installed_at"][:7] for u in data["software_updates"]
    ],
}


def _seed_records_from_1999():
    from bosch_flow_mcp import db

    conn = db.get_db()
    db.save_battery_snapshot(
        conn, FAKE_BIKE_ID, {"battery_id": FAKE_BATTERY_ID}, "1999-06-15T08:00:00+00:00"
    )
    db.save_service_record(conn, FAKE_BIKE_ID, {"id": "SVC-1999", "date": "1999-06-15"})
    db.save_software_update(
        conn, FAKE_BIKE_ID, {"installedAt": "1999-06-15T12:00:00Z", "component": "driveUnit"}
    )
    conn.commit()
    conn.close()


@pytest.mark.parametrize("tool,args,_key", RANGE_TOOLS, ids=RANGE_TOOL_IDS)
async def test_every_date_tool_refuses_an_inverted_range(tool, args, _key):
    data = await _call_tool(tool, **args, start_date="2026-03-31", end_date="2026-03-01")
    assert data == {
        "error": (
            "Start date 2026-03-31 is after end date 2026-03-01. "
            "Use a start on or before that date."
        )
    }


@pytest.mark.parametrize("tool,args,_key", RANGE_TOOLS, ids=RANGE_TOOL_IDS)
async def test_every_date_tool_refuses_a_start_after_today_with_no_end(tool, args, _key):
    data = await _call_tool(tool, **args, start_date="2999-01-01")
    assert data == {
        "error": (
            f"Start date 2999-01-01 is after today ({date.today().isoformat()}). "
            "Use a start on or before that date."
        )
    }


@pytest.mark.parametrize("tool,args,key", RANGE_TOOLS, ids=RANGE_TOOL_IDS)
@pytest.mark.parametrize("end_date", ["1999-12", "0001-01-01"])
async def test_an_end_date_alone_is_never_refused(tool, args, key, end_date):
    """Nobody gave a start, so a refusal would name one the caller never sent."""
    data = await _call_tool(tool, **args, end_date=end_date)
    assert "error" not in data
    assert data[key] == []


@pytest.mark.parametrize("tool", AUTO_SYNCING_TOOLS)
@pytest.mark.parametrize(
    "dates",
    [{"start_date": "2026-03-31", "end_date": "2026-03-01"}, {"start_date": "not-a-date"}],
    ids=["inverted", "not-a-date"],
)
async def test_a_refused_date_costs_no_sync(tool, dates):
    module, args = AUTO_SYNCING_TOOLS[tool]
    with patch(f"bosch_flow_mcp.tools.{module}.auto_sync_if_stale") as auto_sync:
        data = await _call_tool(tool, **args, **dates)
    assert "error" in data
    auto_sync.assert_not_called()


@pytest.mark.parametrize("tool", AUTO_SYNCING_TOOLS)
@pytest.mark.parametrize(
    "dates",
    [{"start_date": "2026-03-01", "end_date": "2026-03-31"}, {}],
    ids=["range", "no-dates"],
)
async def test_an_accepted_date_still_syncs_first(tool, dates):
    module, args = AUTO_SYNCING_TOOLS[tool]
    with patch(f"bosch_flow_mcp.tools.{module}.auto_sync_if_stale") as auto_sync:
        data = await _call_tool(tool, **args, **dates)
    assert "error" not in data
    auto_sync.assert_called_once()


@pytest.mark.parametrize("tool,records", ALL_RECORDS_TOOLS.items(), ids=list(ALL_RECORDS_TOOLS))
async def test_an_end_date_before_2000_means_everything_up_to_it(tool, records):
    _seed_records_from_1999()
    assert records(await _call_tool(tool, end_date="1999-12")) == ["1999-06"]


@pytest.mark.parametrize("tool,records", ALL_RECORDS_TOOLS.items(), ids=list(ALL_RECORDS_TOOLS))
async def test_a_later_end_date_alone_still_reaches_back_to_1999(tool, records):
    _seed_records_from_1999()
    found = records(await _call_tool(tool, end_date="2026-03-31"))
    assert sorted(found) == ["1999-06", "2026-03"]


async def test_battery_trends_with_no_dates_cover_a_record_from_before_2000():
    _seed_records_from_1999()
    data = await _call_tool("bosch_battery_trends")
    assert [t["period"] for t in data["trends"]] == ["1999-06", "2026-03", "2026-04"]


async def test_battery_trends_with_no_dates_cover_a_future_dated_snapshot():
    from bosch_flow_mcp import db

    conn = db.get_db()
    db.save_battery_snapshot(
        conn, FAKE_BIKE_ID, {"battery_id": FAKE_BATTERY_ID}, "2030-01-15T08:00:00+00:00"
    )
    conn.commit()
    conn.close()

    data = await _call_tool("bosch_battery_trends")
    assert [t["period"] for t in data["trends"]] == ["2026-03", "2026-04", "2030-01"]


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
    """A None response (404/offline) gives the offline hint."""
    from bosch_flow_mcp.tools import battery_tools

    with patch("bosch_flow_mcp.tools.battery_tools.api.get", return_value=None):
        data = json.loads(await battery_tools.bosch_get_soc(FAKE_BIKE_ID))
    assert "error" in data
    assert "offline" in data["hint"].lower() or "ConnectModule" in data["hint"]
