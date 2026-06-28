"""Tests for SQLite schema and helpers."""

from datetime import datetime, timezone

import pytest

from bosch_flow_mcp import db
from tests.conftest import (
    FAKE_BATTERY_ID,
    FAKE_BIKE_ID,
    FAKE_PART_NUMBER,
    FAKE_SERIAL_NUMBER,
)


def test_get_db_creates_schema(tmp_path):
    """get_db creates all required tables."""
    conn = db.get_db(tmp_path / "test.db")
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "bikes" in tables
    assert "batteries" in tables
    assert "components" in tables
    assert "service_records" in tables
    assert "software_updates" in tables
    assert "soc_snapshots" in tables
    assert "capacity_tests" in tables
    assert "sync_log" in tables
    conn.close()


def test_save_and_query_bike(tmp_db):
    db.save_bike(
        tmp_db,
        FAKE_BIKE_ID,
        {
            "name": "My Test Bike",
            "brand_name": "TestBrand",
            "frame_number": "TEST123",
            "raw": {"extra": "data"},
        },
    )
    tmp_db.commit()
    bikes = db.query_bikes(tmp_db)
    assert len(bikes) == 1
    assert bikes[0]["bike_id"] == FAKE_BIKE_ID
    assert bikes[0]["brand_name"] == "TestBrand"


def test_save_battery_snapshot(tmp_db):
    now = datetime.now(timezone.utc).isoformat()
    battery = {
        "battery_id": FAKE_BATTERY_ID,
        "batteryLevel": 80,
        "remainingEnergy": 500.0,
        "totalEnergy": 625.0,
        "isCharging": False,
        "numberOfFullChargeCycles": {"total": 42, "onBike": 40, "offBike": 2},
        "deliveredWhOverLifetime": 21000.0,
        "softwareVersion": "1.2.3",
    }
    db.save_battery_snapshot(tmp_db, FAKE_BIKE_ID, battery, now)
    tmp_db.commit()
    rows = db.query_battery_latest(tmp_db, FAKE_BIKE_ID)
    assert len(rows) == 1
    assert rows[0]["battery_level"] == 80
    assert rows[0]["charge_cycles_total"] == 42


def test_save_battery_snapshot_ignore_duplicate(tmp_db):
    """Inserting same (bike_id, battery_id, captured_at) is silently ignored."""
    now = "2026-04-07T10:00:00+00:00"
    bat = {
        "battery_id": FAKE_BATTERY_ID,
        "batteryLevel": 80,
        "numberOfFullChargeCycles": {"total": 42},
    }
    db.save_battery_snapshot(tmp_db, FAKE_BIKE_ID, bat, now)
    bat["batteryLevel"] = 90  # change - should be ignored
    db.save_battery_snapshot(tmp_db, FAKE_BIKE_ID, bat, now)
    tmp_db.commit()
    rows = db.query_batteries(tmp_db, FAKE_BIKE_ID, "2026-04-01", "2026-04-30")
    assert len(rows) == 1
    assert rows[0]["battery_level"] == 80  # first insert wins


def test_save_and_query_component(tmp_db):
    comp = {
        "partNumber": FAKE_PART_NUMBER,
        "serialNumber": FAKE_SERIAL_NUMBER,
        "productName": "Test Drive Unit",
        "softwareVersion": "4.5.6",
    }
    db.save_component(tmp_db, FAKE_BIKE_ID, "driveUnit", comp)
    tmp_db.commit()
    comps = db.query_components(tmp_db, FAKE_BIKE_ID, "driveUnit")
    assert len(comps) == 1
    assert comps[0]["part_number"] == FAKE_PART_NUMBER
    assert comps[0]["software_version"] == "4.5.6"


def test_save_capacity_test(tmp_db):
    db.save_capacity_test(
        tmp_db, FAKE_PART_NUMBER, FAKE_SERIAL_NUMBER, "2026-03-01", {"result": "ok"}
    )
    tmp_db.commit()
    tests = db.query_capacity_tests(tmp_db, FAKE_PART_NUMBER, FAKE_SERIAL_NUMBER)
    assert len(tests) == 1
    assert tests[0]["test_date"] == "2026-03-01"


def test_log_sync_and_get_last_sync_time(tmp_db):
    db.log_sync(tmp_db, "batteries", "ok", 5)
    last = db.get_last_sync_time(tmp_db, "batteries")
    assert last is not None
    assert last.year >= 2026


def test_get_last_sync_time_no_entry(tmp_db):
    last = db.get_last_sync_time(tmp_db, "batteries")
    assert last is None


def test_query_batteries_date_range(populated_db):
    rows = db.query_batteries(populated_db, FAKE_BIKE_ID, "2026-03-01", "2026-03-31")
    # Only March snapshots
    assert len(rows) > 0
    for r in rows:
        assert r["captured_at"].startswith("2026-03")


def test_query_battery_latest_returns_one_per_battery(populated_db):
    rows = db.query_battery_latest(populated_db, FAKE_BIKE_ID)
    assert len(rows) == 1  # one battery_id
    # Should be the most recent snapshot (charge_cycles = 15)
    assert rows[0]["charge_cycles_total"] == 15


def test_query_service_records(populated_db):
    records = db.query_service_records(populated_db, FAKE_BIKE_ID, None, None)
    assert len(records) == 1
    assert records[0]["description"] == "Annual service"


def test_query_software_updates(populated_db):
    updates = db.query_software_updates(populated_db, FAKE_BIKE_ID, None, None)
    assert len(updates) == 1
    assert updates[0]["from_version"] == "4.5.5"
    assert updates[0]["to_version"] == "4.5.6"


# --- Component null-serial dedup + reconciliation ---


def test_save_component_null_serial_no_duplicates(tmp_db):
    """A component with no serial must not insert a fresh duplicate every sync.

    SQLite treats NULLs as distinct in the UNIQUE index, so without a dedup key the
    row would accumulate. save_component falls back serial -> part -> type.
    """
    comp = {"partNumber": "DUPART01", "productName": "No-Serial DU", "softwareVersion": "1.0"}
    for _ in range(3):
        db.save_component(tmp_db, FAKE_BIKE_ID, "driveUnit", comp)
    tmp_db.commit()
    rows = db.query_components(tmp_db, FAKE_BIKE_ID, "driveUnit")
    assert len(rows) == 1
    # dedup key falls back to part number when serial is absent
    assert rows[0]["serial_number"] == "DUPART01"


def test_save_component_null_serial_and_part_uses_type(tmp_db):
    """With neither serial nor part, the component type itself is the dedup key."""
    comp = {"productName": "Mystery part"}
    db.save_component(tmp_db, FAKE_BIKE_ID, "headUnit", comp)
    db.save_component(tmp_db, FAKE_BIKE_ID, "headUnit", comp)
    tmp_db.commit()
    rows = db.query_components(tmp_db, FAKE_BIKE_ID, "headUnit")
    assert len(rows) == 1
    assert rows[0]["serial_number"] == "headUnit"


def test_save_component_resync_replaces_version(tmp_db):
    """Re-syncing the same component (by serial) replaces in place, no duplicate."""
    comp = {"partNumber": "P1", "serialNumber": "S1", "softwareVersion": "1.0.0"}
    db.save_component(tmp_db, FAKE_BIKE_ID, "driveUnit", comp)
    comp["softwareVersion"] = "1.1.0"
    db.save_component(tmp_db, FAKE_BIKE_ID, "driveUnit", comp)
    tmp_db.commit()
    rows = db.query_components(tmp_db, FAKE_BIKE_ID, "driveUnit")
    assert len(rows) == 1
    assert rows[0]["software_version"] == "1.1.0"


def test_delete_components_for_bike(tmp_db):
    db.save_component(tmp_db, FAKE_BIKE_ID, "driveUnit", {"partNumber": "P1", "serialNumber": "S1"})
    db.save_component(tmp_db, FAKE_BIKE_ID, "battery", {"partNumber": "P2", "serialNumber": "S2"})
    db.save_component(tmp_db, "other-bike", "driveUnit", {"partNumber": "P3", "serialNumber": "S3"})
    tmp_db.commit()
    db.delete_components_for_bike(tmp_db, FAKE_BIKE_ID)
    tmp_db.commit()
    assert db.query_components(tmp_db, FAKE_BIKE_ID, None) == []
    # other bike's rows untouched
    assert len(db.query_components(tmp_db, "other-bike", None)) == 1


# --- Sync-log status set + note ---


@pytest.mark.parametrize(
    "status,expect_settled",
    [
        ("ok", True),
        ("unavailable", True),
        ("empty", True),
        ("error", False),
        ("auth_error", False),
    ],
)
def test_get_last_sync_time_counts_settled_statuses(tmp_db, status, expect_settled):
    db.log_sync(tmp_db, "service", status, 0, "note")
    result = db.get_last_sync_time(tmp_db, "service")
    assert (result is not None) is expect_settled


def test_get_last_sync_time_picks_latest_row(tmp_db):
    """Latest settled row wins even if an earlier ok exists."""
    db.log_sync(tmp_db, "service", "ok", 1)
    db.log_sync(tmp_db, "service", "unavailable", 0, "needs euda")
    result = db.get_last_sync_time(tmp_db, "service")
    assert result is not None  # the unavailable row still counts as "checked"


def test_last_sync_note_returns_reason_when_not_ok(tmp_db):
    db.log_sync(tmp_db, "service", "unavailable", 0, "needs euda client")
    note = db.last_sync_note(tmp_db, "service")
    assert note == ("unavailable", "needs euda client")


def test_last_sync_note_none_when_ok(tmp_db):
    """A genuine ok result must not produce a 'why empty' note (don't cry wolf)."""
    db.log_sync(tmp_db, "service", "ok", 0)
    assert db.last_sync_note(tmp_db, "service") is None


def test_last_sync_note_uses_latest_row(tmp_db):
    """An ok after an earlier unavailable clears the note."""
    db.log_sync(tmp_db, "components", "unavailable", 0, "old reason")
    db.log_sync(tmp_db, "components", "ok", 3)
    assert db.last_sync_note(tmp_db, "components") is None


def test_last_sync_note_no_rows(tmp_db):
    assert db.last_sync_note(tmp_db, "service") is None
