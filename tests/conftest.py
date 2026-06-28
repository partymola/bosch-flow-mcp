"""Shared test fixtures for the Bosch Flow MCP tests.

All fixtures use fictional data - no real bike IDs, serial numbers,
frame numbers, or personal information.
"""

import json

import pytest

from bosch_flow_mcp import db

# Fictional test data - no real values
FAKE_BIKE_ID = "00000000-0000-0000-0000-000000000001"
FAKE_BIKE_ID_2 = "00000000-0000-0000-0000-000000000002"
FAKE_BATTERY_ID = "TESTPART001_SN123456"
FAKE_FRAME_NUMBER = "TESTFRAME001"
FAKE_PART_NUMBER = "TESTPART001"
FAKE_SERIAL_NUMBER = "SN123456"


@pytest.fixture
def tmp_db(tmp_path):
    """Temporary SQLite database with schema applied."""
    db_path = tmp_path / "test_bosch.db"
    conn = db.get_db(db_path)
    yield conn
    conn.close()


@pytest.fixture
def populated_db(tmp_db):
    """Database pre-loaded with fictional bike, battery, and component data."""
    conn = tmp_db
    now = "2026-04-07T10:00:00+00:00"

    # Insert a fake bike
    conn.execute(
        """INSERT OR REPLACE INTO bikes
        (bike_id, name, brand_name, frame_number, raw_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)""",
        (FAKE_BIKE_ID, "Test Bike", "TestBrand", FAKE_FRAME_NUMBER, json.dumps({}), now),
    )

    # Insert battery snapshots across multiple dates (time series)
    for i, (captured_at, level, cycles) in enumerate(
        [
            ("2026-03-01T08:00:00+00:00", 85, 10),
            ("2026-03-08T08:00:00+00:00", 80, 11),
            ("2026-03-15T08:00:00+00:00", 78, 12),
            ("2026-03-22T08:00:00+00:00", 82, 13),
            ("2026-04-01T08:00:00+00:00", 75, 14),
            ("2026-04-07T08:00:00+00:00", 90, 15),
        ]
    ):
        conn.execute(
            """INSERT OR IGNORE INTO batteries
            (bike_id, battery_id, battery_level, remaining_energy_wh, total_energy_wh,
             is_charging, charge_cycles_total, charge_cycles_on_bike, charge_cycles_off_bike,
             delivered_wh_lifetime, software_version, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                FAKE_BIKE_ID,
                FAKE_BATTERY_ID,
                level,
                round(500 * level / 100),
                625,
                0,
                cycles,
                cycles - 1,
                1,
                round(cycles * 500.0),
                "1.2.3",
                captured_at,
            ),
        )

    # Insert a component
    conn.execute(
        """INSERT OR REPLACE INTO components
        (bike_id, component_type, part_number, serial_number, product_name,
         software_version, raw_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            FAKE_BIKE_ID,
            "driveUnit",
            FAKE_PART_NUMBER,
            FAKE_SERIAL_NUMBER,
            "Test Drive Unit CX",
            "4.5.6",
            json.dumps({}),
            now,
        ),
    )

    # Insert a capacity test
    conn.execute(
        """INSERT OR IGNORE INTO capacity_tests (part_number, serial_number, test_date, raw_json)
        VALUES (?, ?, ?, ?)""",
        (FAKE_PART_NUMBER, FAKE_SERIAL_NUMBER, "2026-03-01", json.dumps({"result": "ok"})),
    )

    # Insert a service record
    conn.execute(
        """INSERT OR IGNORE INTO service_records
        (bike_id, record_id, service_date, description, raw_json)
        VALUES (?, ?, ?, ?, ?)""",
        (FAKE_BIKE_ID, "SVC001", "2026-03-15", "Annual service", json.dumps({})),
    )

    # Insert a software update
    conn.execute(
        """INSERT OR IGNORE INTO software_updates
        (bike_id, installed_at, component, from_version, to_version, raw_json)
        VALUES (?, ?, ?, ?, ?, ?)""",
        (FAKE_BIKE_ID, "2026-03-20T12:00:00Z", "driveUnit", "4.5.5", "4.5.6", json.dumps({})),
    )

    # Sync log entries
    for dtype in ["bikes", "batteries", "components", "service", "software_updates", "capacity"]:
        conn.execute(
            """INSERT INTO sync_log (synced_at, data_type, status, records_added, notes)
            VALUES (?, ?, ?, ?, ?)""",
            (now, dtype, "ok", 1, ""),
        )

    conn.commit()
    return conn
