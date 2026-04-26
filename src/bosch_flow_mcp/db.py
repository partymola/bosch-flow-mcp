"""SQLite database schema and helpers for the Bosch Flow local cache."""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import DB_PATH, _PACKAGE_ROOT

SCHEMA = """
CREATE TABLE IF NOT EXISTS bikes (
    bike_id TEXT PRIMARY KEY,
    name TEXT,
    brand_name TEXT,
    frame_number TEXT,
    raw_json TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batteries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bike_id TEXT NOT NULL,
    battery_id TEXT,
    battery_level INTEGER,
    remaining_energy_wh REAL,
    total_energy_wh REAL,
    is_charging INTEGER,
    charge_cycles_total INTEGER,
    charge_cycles_on_bike INTEGER,
    charge_cycles_off_bike INTEGER,
    delivered_wh_lifetime REAL,
    software_version TEXT,
    captured_at TEXT NOT NULL,
    UNIQUE(bike_id, battery_id, captured_at)
);

CREATE INDEX IF NOT EXISTS idx_batteries_bike_date ON batteries(bike_id, captured_at);

CREATE TABLE IF NOT EXISTS capacity_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    part_number TEXT NOT NULL,
    serial_number TEXT NOT NULL,
    test_date TEXT,
    raw_json TEXT,
    UNIQUE(part_number, serial_number, test_date)
);

CREATE TABLE IF NOT EXISTS components (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bike_id TEXT NOT NULL,
    component_type TEXT NOT NULL,
    part_number TEXT,
    serial_number TEXT,
    product_name TEXT,
    software_version TEXT,
    raw_json TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(bike_id, component_type, serial_number)
);

CREATE INDEX IF NOT EXISTS idx_components_bike ON components(bike_id);

CREATE TABLE IF NOT EXISTS service_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bike_id TEXT NOT NULL,
    record_id TEXT,
    service_date TEXT,
    description TEXT,
    raw_json TEXT,
    UNIQUE(bike_id, record_id)
);

CREATE TABLE IF NOT EXISTS software_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bike_id TEXT NOT NULL,
    installed_at TEXT,
    component TEXT,
    from_version TEXT,
    to_version TEXT,
    raw_json TEXT,
    UNIQUE(bike_id, installed_at, component)
);

CREATE INDEX IF NOT EXISTS idx_sw_updates_bike ON software_updates(bike_id);

CREATE TABLE IF NOT EXISTS soc_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bike_id TEXT NOT NULL,
    state_of_charge INTEGER,
    charging_active INTEGER,
    charger_connected INTEGER,
    remaining_energy_wh REAL,
    odometer_m INTEGER,
    reachable_range_json TEXT,
    captured_at TEXT NOT NULL,
    UNIQUE(bike_id, captured_at)
);

CREATE INDEX IF NOT EXISTS idx_soc_bike_date ON soc_snapshots(bike_id, captured_at);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    synced_at TEXT NOT NULL,
    data_type TEXT NOT NULL,
    status TEXT NOT NULL,
    records_added INTEGER,
    notes TEXT
);
"""


def get_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a database connection and ensure the schema exists."""
    if db_path is not None:
        path = Path(db_path)
    else:
        env_val = os.environ.get("BOSCH_FLOW_MCP_DB_PATH")
        path = Path(env_val) if env_val else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Save helpers
# ---------------------------------------------------------------------------

def save_bike(conn: sqlite3.Connection, bike_id: str, row: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT OR REPLACE INTO bikes (bike_id, name, brand_name, frame_number, raw_json, updated_at)
        VALUES (:bike_id, :name, :brand_name, :frame_number, :raw_json, :updated_at)""",
        {
            "bike_id": bike_id,
            "name": row.get("name"),
            "brand_name": row.get("brand_name"),
            "frame_number": row.get("frame_number"),
            "raw_json": json.dumps(row.get("raw")),
            "updated_at": now,
        },
    )


def save_battery_snapshot(conn: sqlite3.Connection, bike_id: str, battery: dict,
                           captured_at: str) -> None:
    cycles = battery.get("numberOfFullChargeCycles") or {}
    conn.execute(
        """INSERT OR IGNORE INTO batteries
        (bike_id, battery_id, battery_level, remaining_energy_wh, total_energy_wh,
         is_charging, charge_cycles_total, charge_cycles_on_bike, charge_cycles_off_bike,
         delivered_wh_lifetime, software_version, captured_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            bike_id,
            battery.get("battery_id"),
            battery.get("batteryLevel"),
            battery.get("remainingEnergy"),
            battery.get("totalEnergy"),
            1 if battery.get("isCharging") else 0,
            cycles.get("total"),
            cycles.get("onBike"),
            cycles.get("offBike"),
            battery.get("deliveredWhOverLifetime"),
            battery.get("softwareVersion"),
            captured_at,
        ),
    )


def save_capacity_test(conn: sqlite3.Connection, part_number: str, serial_number: str,
                        test_date: str | None, raw: dict) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO capacity_tests (part_number, serial_number, test_date, raw_json)
        VALUES (?, ?, ?, ?)""",
        (part_number, serial_number, test_date, json.dumps(raw)),
    )


def save_component(conn: sqlite3.Connection, bike_id: str, component_type: str,
                    row: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT OR REPLACE INTO components
        (bike_id, component_type, part_number, serial_number, product_name,
         software_version, raw_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            bike_id,
            component_type,
            row.get("partNumber") or row.get("part_number"),
            row.get("serialNumber") or row.get("serial_number"),
            row.get("productName") or row.get("product_name"),
            row.get("softwareVersion") or row.get("software_version"),
            json.dumps(row),
            now,
        ),
    )


def save_service_record(conn: sqlite3.Connection, bike_id: str, record: dict) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO service_records
        (bike_id, record_id, service_date, description, raw_json)
        VALUES (?, ?, ?, ?, ?)""",
        (
            bike_id,
            record.get("id") or record.get("record_id"),
            record.get("date") or record.get("service_date"),
            record.get("description"),
            json.dumps(record),
        ),
    )


def save_software_update(conn: sqlite3.Connection, bike_id: str, update: dict) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO software_updates
        (bike_id, installed_at, component, from_version, to_version, raw_json)
        VALUES (?, ?, ?, ?, ?, ?)""",
        (
            bike_id,
            update.get("installedAt") or update.get("installed_at") or update.get("createdAt"),
            update.get("component"),
            update.get("fromVersion") or update.get("from_version"),
            update.get("toVersion") or update.get("to_version"),
            json.dumps(update),
        ),
    )


def save_soc_snapshot(conn: sqlite3.Connection, bike_id: str, soc: dict) -> None:
    reachable = soc.get("reachableRange")
    conn.execute(
        """INSERT OR IGNORE INTO soc_snapshots
        (bike_id, state_of_charge, charging_active, charger_connected,
         remaining_energy_wh, odometer_m, reachable_range_json, captured_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            bike_id,
            soc.get("stateOfCharge"),
            1 if soc.get("chargingActive") else 0,
            1 if soc.get("chargerConnected") else 0,
            soc.get("remainingEnergyForRider"),
            soc.get("odometer"),
            json.dumps(reachable) if reachable is not None else None,
            soc.get("stateOfChargeLatestUpdate") or datetime.now(timezone.utc).isoformat(),
        ),
    )


def log_sync(conn: sqlite3.Connection, data_type: str, status: str,
             records_added: int = 0, notes: str = "") -> None:
    conn.execute(
        """INSERT INTO sync_log (synced_at, data_type, status, records_added, notes)
        VALUES (?, ?, ?, ?, ?)""",
        (datetime.now(timezone.utc).isoformat(), data_type, status, records_added, notes),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Sync log helpers
# ---------------------------------------------------------------------------

def get_last_sync_time(conn: sqlite3.Connection, data_type: str) -> datetime | None:
    """Return timestamp of most recent successful sync for a data type."""
    row = conn.execute(
        "SELECT MAX(synced_at) AS t FROM sync_log WHERE data_type = ? AND status = 'ok'",
        (data_type,),
    ).fetchone()
    if row and row["t"]:
        return datetime.fromisoformat(row["t"])
    return None


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def _rows_to_dicts(rows) -> list[dict]:
    result = []
    for r in rows:
        d = dict(r)
        for key in ("raw_json", "reachable_range_json"):
            if key in d and d[key]:
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        result.append(d)
    return result


def query_bikes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM bikes ORDER BY brand_name, bike_id").fetchall()
    return _rows_to_dicts(rows)


def query_bike(conn: sqlite3.Connection, bike_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM bikes WHERE bike_id = ?", (bike_id,)).fetchone()
    return dict(row) if row else None


def query_batteries(conn: sqlite3.Connection, bike_id: str | None,
                     start_date: str, end_date: str) -> list[dict]:
    if bike_id:
        rows = conn.execute(
            """SELECT * FROM batteries WHERE bike_id = ?
            AND captured_at >= ? AND captured_at <= ? ORDER BY captured_at""",
            (bike_id, start_date, end_date),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM batteries WHERE captured_at >= ? AND captured_at <= ?
            ORDER BY bike_id, captured_at""",
            (start_date, end_date),
        ).fetchall()
    return _rows_to_dicts(rows)


def query_battery_latest(conn: sqlite3.Connection, bike_id: str | None) -> list[dict]:
    """Return the most recent battery snapshot per bike."""
    if bike_id:
        rows = conn.execute(
            """SELECT b.* FROM batteries b
            INNER JOIN (
                SELECT bike_id, battery_id, MAX(captured_at) AS latest
                FROM batteries WHERE bike_id = ?
                GROUP BY bike_id, battery_id
            ) m ON b.bike_id = m.bike_id AND b.battery_id IS m.battery_id
               AND b.captured_at = m.latest""",
            (bike_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT b.* FROM batteries b
            INNER JOIN (
                SELECT bike_id, battery_id, MAX(captured_at) AS latest
                FROM batteries GROUP BY bike_id, battery_id
            ) m ON b.bike_id = m.bike_id AND b.battery_id IS m.battery_id
               AND b.captured_at = m.latest
            ORDER BY b.bike_id""",
        ).fetchall()
    return _rows_to_dicts(rows)


def query_components(conn: sqlite3.Connection, bike_id: str | None,
                      component_type: str | None) -> list[dict]:
    params: list = []
    where: list[str] = []
    if bike_id:
        where.append("bike_id = ?")
        params.append(bike_id)
    if component_type:
        where.append("component_type = ?")
        params.append(component_type)
    sql = "SELECT * FROM components"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY bike_id, component_type"
    rows = conn.execute(sql, params).fetchall()
    return _rows_to_dicts(rows)


def query_service_records(conn: sqlite3.Connection, bike_id: str | None,
                           start_date: str | None, end_date: str | None) -> list[dict]:
    params: list = []
    where: list[str] = []
    if bike_id:
        where.append("bike_id = ?")
        params.append(bike_id)
    if start_date:
        where.append("service_date >= ?")
        params.append(start_date)
    if end_date:
        where.append("service_date <= ?")
        params.append(end_date)
    sql = "SELECT * FROM service_records"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY service_date DESC"
    rows = conn.execute(sql, params).fetchall()
    return _rows_to_dicts(rows)


def query_software_updates(conn: sqlite3.Connection, bike_id: str | None,
                            start_date: str | None, end_date: str | None) -> list[dict]:
    params: list = []
    where: list[str] = []
    if bike_id:
        where.append("bike_id = ?")
        params.append(bike_id)
    if start_date:
        where.append("installed_at >= ?")
        params.append(start_date)
    if end_date:
        where.append("installed_at <= ?")
        params.append(end_date)
    sql = "SELECT * FROM software_updates"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY installed_at DESC"
    rows = conn.execute(sql, params).fetchall()
    return _rows_to_dicts(rows)


def query_capacity_tests(conn: sqlite3.Connection,
                          part_number: str | None, serial_number: str | None) -> list[dict]:
    params: list = []
    where: list[str] = []
    if part_number:
        where.append("part_number = ?")
        params.append(part_number)
    if serial_number:
        where.append("serial_number = ?")
        params.append(serial_number)
    sql = "SELECT * FROM capacity_tests"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY test_date DESC"
    rows = conn.execute(sql, params).fetchall()
    return _rows_to_dicts(rows)


def query_soc_snapshots(conn: sqlite3.Connection, bike_id: str,
                         start_date: str, end_date: str) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM soc_snapshots
        WHERE bike_id = ? AND captured_at >= ? AND captured_at <= ?
        ORDER BY captured_at""",
        (bike_id, start_date, end_date),
    ).fetchall()
    return _rows_to_dicts(rows)
