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

# A masked, fictional mobile /v2/bike-profile response, mirroring the real shape:
# batteries list + singleton component sections, ABS null (no-ABS bike), one section
# (headUnit) absent entirely. All values fictional.
FAKE_V2_PROFILE = {
    "id": FAKE_BIKE_ID,
    "brandName": "TestBrand",
    "antiLockBrakeSystem": None,  # bike without ABS -> must be skipped
    "batteries": [
        {
            "partNumber": "TESTBATT01",
            "productCode": "TBP3000",
            "serialNumber": "BATTSN0001",
            "productName": "PowerTube Test 800",
            "softwareVersion": "19.0.1",
            "hardwareVersion": "4.0.0",
            "numberOfFullChargeCycles": {"total": 5, "onBike": 0, "offBike": 5},
            "deliveredWhOverLifetime": 1000,
            "totalEnergy": 800.0,
            "batteryLevel": None,
            "remainingEnergy": None,
            "isCharging": None,
        },
        {
            "partNumber": "TESTBATT02",
            "productCode": "TBP2000",
            "serialNumber": "BATTSN0002",
            "productName": "PowerTube Test 625",
            "softwareVersion": "19.0.1",
            "numberOfFullChargeCycles": {"total": 3, "onBike": 1, "offBike": 2},
            "deliveredWhOverLifetime": 500,
            "totalEnergy": 625.0,
        },
    ],
    "driveUnit": {
        "partNumber": "TESTDU001",
        "productCode": "TDU3000",
        "serialNumber": "DUSN0001",
        "productName": "Test Drive Unit PX",
        "softwareVersion": "19.5.0",
        "hardwareVersion": "1.2.0",
    },
    "connectedModule": {
        "partNumber": "TESTCM001",
        "productCode": "TCM3000",
        "serialNumber": "CMSN0001",
        "productName": "Test ConnectModule",
        "softwareVersion": "19.1.0",
    },
    "remoteControl": {
        "partNumber": "TESTRC001",
        "productCode": "TRC3000",
        "serialNumber": "RCSN0001",
        "productName": "Test Remote",
        "softwareVersion": "19.2.0",
    },
    # "headUnit" key intentionally absent -> exercises the missing-section path.
}


@pytest.fixture(autouse=True)
def _isolate_credentials(tmp_path, monkeypatch):
    """Hermetic credentials: never read the developer's real token/config file.

    Points auth at non-existent paths so current_client_id() falls back to the
    default (one-bike-app) unless a test writes its own token via token_as(). Resets
    the in-memory token cache around every test. This is both correctness (the new
    routing reads the token file) and public-repo data safety.
    """
    import bosch_flow_mcp.auth as auth_module

    monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", tmp_path / "_no_tokens.json")
    monkeypatch.setattr(auth_module, "BOSCH_CONFIG_PATH", tmp_path / "_no_config.json")
    # require_auth only checks the file EXISTS (never reads it); point it at a fictional
    # present file so no tool test silently depends on the real token file existing.
    present = tmp_path / "_auth_present.json"
    present.write_text(json.dumps({"access_token": "fake", "expiry": 9999999999}))
    monkeypatch.setattr("bosch_flow_mcp.helpers.BOSCH_TOKENS_PATH", present)
    auth_module._tokens = None
    yield
    auth_module._tokens = None


@pytest.fixture
def token_as(tmp_path, monkeypatch):
    """Factory: write a fictional token file with a chosen client_id and route to it.

    Drives the REAL current_client_id()/token_is_euda() chain off an actual file, so
    routing tests exercise the client_id->route decision instead of mocking it away.
    """

    def _set(client_id: str):
        import bosch_flow_mcp.auth as auth_module

        path = tmp_path / "tokens.json"
        path.write_text(
            json.dumps(
                {
                    "access_token": "fake_access",
                    "refresh_token": "fake_refresh",
                    "expiry": 9999999999,
                    "client_id": client_id,
                }
            )
        )
        monkeypatch.setattr(auth_module, "BOSCH_TOKENS_PATH", path)
        auth_module._tokens = None
        return path

    return _set


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
