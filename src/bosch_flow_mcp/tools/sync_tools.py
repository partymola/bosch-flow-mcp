"""Sync tool: fetch data from Bosch APIs and store in local SQLite cache."""

import logging
from datetime import datetime, timezone

import anyio

from .. import api, db
from ..config import (
    BES3_BIKE,
    BES3_BIKES,
    BES3_CAPACITY_TESTERS,
    BES3_REGISTRATIONS,
    BES3_SERVICE_RECORDS,
    BES3_SW_UPDATES,
    DATA_ACT_API_BASE,
    MOBILE_BIKE_PROFILE_V2,
)
from ..helpers import format_response, require_auth
from ..mcp_instance import mcp

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Individual sync functions
# ---------------------------------------------------------------------------


def _sync_bikes(conn) -> int:
    """Fetch all bikes. Tries Data Act API first, falls back to mobile API."""
    # Data Act API (works with EUDA tokens)
    response = api.get(BES3_BIKES, base=DATA_ACT_API_BASE)
    if not response:
        # Fallback: mobile API (works with one-bike-app tokens)
        response = api.get("/v1/bike-profile")
    if not response:
        return 0

    # Handle various response shapes
    if isinstance(response, list):
        bikes = response
    elif isinstance(response, dict):
        bikes = response.get("bikes", []) or response.get("data", [])
        # v1 mobile API wraps in {"data": {"attributes": {"bikes": [...]}}}
        if not bikes:
            attrs = response.get("data", {})
            if isinstance(attrs, dict):
                attrs = attrs.get("attributes", attrs)
                bikes = attrs.get("bikes", [])
        # Single bike response - wrap in list
        if not bikes and ("id" in response or "bikeId" in response):
            bikes = [response]
    else:
        bikes = []

    count = 0
    for bike in bikes:
        bike_id = bike.get("id") or bike.get("bikeId")
        if not bike_id:
            continue
        attrs = bike.get("attributes", bike)  # v1 has attributes wrapper, v2 is flat
        db.save_bike(
            conn,
            bike_id,
            {
                "name": attrs.get("name") or attrs.get("brandName", ""),
                "brand_name": attrs.get("brandName"),
                "frame_number": attrs.get("frameNumber"),
                "raw": bike,
            },
        )
        count += 1

    conn.commit()
    return count


def _sync_batteries(conn) -> int:
    """Snapshot current battery state for all bikes."""
    bike_rows = db.query_bikes(conn)
    if not bike_rows:
        # No bikes synced yet - try to fetch them first
        _sync_bikes(conn)
        bike_rows = db.query_bikes(conn)

    count = 0
    now = datetime.now(timezone.utc).isoformat()

    for bike_row in bike_rows:
        bike_id = bike_row["bike_id"]
        # Try Data Act API first (works with EUDA tokens), fall back to mobile
        response = api.get(BES3_BIKE.format(bike_id=bike_id), base=DATA_ACT_API_BASE)
        if not response:
            response = api.get(MOBILE_BIKE_PROFILE_V2.format(bike_id=bike_id))
        if not response:
            continue

        # Both APIs may use flat or nested attributes
        attrs = response.get("attributes", response)
        batteries = attrs.get("batteries", [])

        for battery in batteries:
            # Derive a stable battery_id from part/serial or product name
            serial = battery.get("serialNumber") or ""
            part = battery.get("partNumber") or ""
            battery_id = (
                f"{part}_{serial}" if (part or serial) else battery.get("productName", "battery")
            )
            battery["battery_id"] = battery_id

            db.save_battery_snapshot(conn, bike_id, battery, now)
            count += 1

    conn.commit()
    return count


def _sync_components(conn) -> int:
    """Fetch component registrations and save to components table."""
    response = api.get(BES3_REGISTRATIONS, base=DATA_ACT_API_BASE)
    if not response:
        return 0

    # Registration endpoint returns list of registrations
    registrations = response if isinstance(response, list) else response.get("registrations", [])
    if not registrations and isinstance(response, dict):
        registrations = response.get("data", [])

    count = 0
    for reg in registrations:
        bike_id = reg.get("bikeId") or reg.get("bike_id")
        if not bike_id:
            continue
        components = reg.get("components", [])
        for comp in components:
            component_type = (
                comp.get("componentType") or comp.get("type") or comp.get("productGroup", "unknown")
            )
            db.save_component(conn, bike_id, component_type, comp)
            count += 1

        # Also save the bike-level components if present directly
        for key in ("driveUnit", "battery", "display", "connectedModule", "remoteControl"):
            if key in reg:
                db.save_component(conn, bike_id, key, reg[key])
                count += 1

    conn.commit()
    return count


def _sync_service(conn) -> int:
    """Fetch service book records for all bikes."""
    bike_rows = db.query_bikes(conn)
    count = 0
    for bike_row in bike_rows:
        bike_id = bike_row["bike_id"]
        response = api.get(f"{BES3_SERVICE_RECORDS}?bikeId={bike_id}", base=DATA_ACT_API_BASE)
        if not response:
            continue
        records = response if isinstance(response, list) else response.get("serviceRecords", [])
        if not records and isinstance(response, dict):
            records = response.get("data", [])
        for record in records:
            db.save_service_record(conn, bike_id, record)
            count += 1
    conn.commit()
    return count


def _sync_software_updates(conn) -> int:
    """Fetch software update history for all bikes."""
    bike_rows = db.query_bikes(conn)
    count = 0
    for bike_row in bike_rows:
        bike_id = bike_row["bike_id"]
        # Paginate: fetch up to 100 records
        path = f"{BES3_SW_UPDATES}?bikeId={bike_id}&limit=100&offset=0"
        response = api.get(path, base=DATA_ACT_API_BASE)
        if not response:
            continue
        updates = (
            response if isinstance(response, list) else response.get("installationReports", [])
        )
        if not updates and isinstance(response, dict):
            updates = response.get("data", [])
        for update in updates:
            db.save_software_update(conn, bike_id, update)
            count += 1
    conn.commit()
    return count


def _sync_capacity(conn) -> int:
    """Fetch battery capacity tester data for all known battery components."""
    battery_comps = db.query_components(conn, bike_id=None, component_type="battery")
    # Also try common type names
    for ctype in ("Battery", "batteries"):
        battery_comps.extend(db.query_components(conn, bike_id=None, component_type=ctype))

    count = 0
    seen = set()
    for comp in battery_comps:
        part = comp.get("part_number")
        serial = comp.get("serial_number")
        if not part or not serial:
            continue
        key = (part, serial)
        if key in seen:
            continue
        seen.add(key)

        response = api.get(
            f"{BES3_CAPACITY_TESTERS}?partNumber={part}&serialNumber={serial}",
            base=DATA_ACT_API_BASE,
        )
        if not response:
            continue
        tests = response if isinstance(response, list) else response.get("capacityTests", [])
        if not tests and isinstance(response, dict):
            tests = response.get("data", [response])
        for test in tests:
            test_date = test.get("testDate") or test.get("date")
            db.save_capacity_test(conn, part, serial, test_date, test)
            count += 1

    conn.commit()
    return count


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

_ALL_TYPES = ["bikes", "batteries", "components", "service", "software_updates", "capacity"]


def run_sync(data_types: list[str]) -> dict:
    """Run sync outside MCP context (for CLI use). Returns results dict."""
    conn = db.get_db()
    results = {}

    for dtype in data_types:
        try:
            if dtype == "bikes":
                count = _sync_bikes(conn)
            elif dtype == "batteries":
                count = _sync_batteries(conn)
            elif dtype == "components":
                count = _sync_components(conn)
            elif dtype == "service":
                count = _sync_service(conn)
            elif dtype == "software_updates":
                count = _sync_software_updates(conn)
            elif dtype == "capacity":
                count = _sync_capacity(conn)
            else:
                results[dtype] = {"status": "error", "message": f"Unknown type: {dtype}"}
                continue

            db.log_sync(conn, dtype, "ok", count)
            results[dtype] = {"status": "ok", "records": count}

        except api.BoschAuthError as e:
            results[dtype] = {"status": "auth_error", "message": str(e)}
        except api.BoschAPIError as e:
            db.log_sync(conn, dtype, "error", notes=str(e))
            results[dtype] = {"status": "error", "message": str(e)}

    conn.close()
    return results


def auto_sync_if_stale(data_type: str) -> None:
    """Sync data_type if never synced or last sync was before today.

    Failures are silently suppressed - the caller should still query the cache.
    """
    conn = db.get_db()
    last_sync = db.get_last_sync_time(conn, data_type)
    conn.close()

    today_utc = datetime.now(timezone.utc).date()
    if last_sync is not None and last_sync.date() >= today_utc:
        return

    try:
        run_sync([data_type])
    except Exception:
        logger.debug("Auto-sync failed for %s", data_type, exc_info=True)


# ---------------------------------------------------------------------------
# MCP tool
# ---------------------------------------------------------------------------


@mcp.tool()
@require_auth
async def bosch_sync(data_types: str = "all") -> str:
    """Sync Bosch eBike data to the local cache.

    Fetches data from the Bosch EU Data Act API and stores it in SQLite.
    Run this to populate the cache before using other bosch_get_* tools,
    or to refresh data after a ride or charge cycle.

    Bikes are identified automatically from your Bosch Flow account.
    Battery snapshots build a time series for health trend analysis.

    Args:
        data_types: What to sync. Options: "all", "bikes", "batteries",
            "components", "service", "software_updates", "capacity".
            Comma-separated for multiple, e.g. "bikes,batteries". Default: "all".

    Returns summary of records synced per data type.
    """
    types = [t.strip() for t in data_types.split(",")]
    if "all" in types:
        # Order matters: bikes first (others depend on bike IDs),
        # components before capacity (capacity needs part/serial from components)
        types = _ALL_TYPES

    results = await anyio.to_thread.run_sync(lambda: run_sync(types))
    return format_response(results)
