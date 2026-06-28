"""Sync tool: fetch data from Bosch APIs and store in local SQLite cache."""

import logging
from datetime import datetime, timezone

import anyio

from .. import api, auth, db
from ..config import (
    BES3_BIKE,
    BES3_BIKES,
    BES3_CAPACITY_TESTERS,
    BES3_REGISTRATIONS,
    BES3_SERVICE_RECORDS,
    BES3_SW_UPDATES,
    DATA_ACT_API_BASE,
    MOBILE_BIKE_PROFILE_LIST,
    MOBILE_BIKE_PROFILE_V2,
)
from ..helpers import format_response, require_auth
from ..mcp_instance import mcp

logger = logging.getLogger(__name__)

# Data types only the EU Data Act API serves; the standard app (one-bike-app) API
# has no equivalent endpoint, so these are skipped (not silently 0) on that client.
DATA_ACT_ONLY = frozenset({"service", "software_updates", "capacity"})

# Types derived from the bike list. If a euda token returns no bikes (the non-EU
# trap), every one of these is empty too, so all three carry the euda_empty signal.
_BIKE_DERIVED = frozenset({"bikes", "batteries", "components"})

REQUIRES_EUDA_MSG = (
    "Requires the EU Data Act API. Register a euda client at "
    "portal.bosch-ebike.com/data-act; not available with the standard app sign-in."
)
EUDA_EMPTY_MSG = (
    "The EU Data Act API returned no bikes. If your Bosch account is registered "
    "outside the EU, the Data Act API shares no data for it - remove "
    "config/bosch_config.json and run `bosch-flow-mcp auth` to use the standard "
    "app sign-in, which works for any account."
)


# ---------------------------------------------------------------------------
# Individual sync functions
# ---------------------------------------------------------------------------


def _sync_bikes(conn, is_euda: bool) -> int:
    """Fetch all bikes from the API matching the active client.

    euda token -> Data Act API; one-bike-app token -> mobile app API. The two are
    not interchangeable (each 403s on the other host), so we route, not fall back.
    """
    if is_euda:
        response = api.get(BES3_BIKES, base=DATA_ACT_API_BASE)
    else:
        response = api.get(MOBILE_BIKE_PROFILE_LIST)
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


def _sync_batteries(conn, is_euda: bool) -> int:
    """Snapshot current battery state for all bikes, routed by the active client."""
    bike_rows = db.query_bikes(conn)
    if not bike_rows:
        # No bikes synced yet - try to fetch them first
        _sync_bikes(conn, is_euda)
        bike_rows = db.query_bikes(conn)

    count = 0
    now = datetime.now(timezone.utc).isoformat()

    for bike_row in bike_rows:
        bike_id = bike_row["bike_id"]
        if is_euda:
            response = api.get(BES3_BIKE.format(bike_id=bike_id), base=DATA_ACT_API_BASE)
        else:
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


# Singleton component sections in the mobile bike profile (one object each, vs the
# "batteries" list). antiLockBrakeSystem is null on bikes without ABS.
_MOBILE_COMPONENT_SECTIONS = (
    "driveUnit",
    "connectedModule",
    "headUnit",
    "remoteControl",
    "antiLockBrakeSystem",
)


def _is_real_component(obj) -> bool:
    """True if a profile section actually describes a component (not null/empty)."""
    return isinstance(obj, dict) and bool(
        obj.get("partNumber") or obj.get("serialNumber") or obj.get("productName")
    )


def _collect_components_euda() -> dict[str, list[tuple[str, dict]]]:
    """Map bike_id -> [(component_type, raw)] from the Data Act registrations endpoint."""
    response = api.get(BES3_REGISTRATIONS, base=DATA_ACT_API_BASE)
    result: dict[str, list[tuple[str, dict]]] = {}
    if not response:
        return result

    registrations = response if isinstance(response, list) else response.get("registrations", [])
    if not registrations and isinstance(response, dict):
        registrations = response.get("data", [])

    for reg in registrations:
        bike_id = reg.get("bikeId") or reg.get("bike_id")
        if not bike_id:
            continue
        comps = result.setdefault(bike_id, [])
        for comp in reg.get("components", []):
            if not isinstance(comp, dict):
                continue
            component_type = (
                comp.get("componentType") or comp.get("type") or comp.get("productGroup", "unknown")
            )
            comps.append((component_type, comp))
        # Some registrations carry bike-level component objects directly.
        for key in ("driveUnit", "battery", "display", "connectedModule", "remoteControl"):
            if isinstance(reg.get(key), dict):
                comps.append((key, reg[key]))
    return result


def _collect_components_mobile(conn, is_euda: bool) -> dict[str, list[tuple[str, dict]]]:
    """Map bike_id -> [(component_type, raw)] from each bike's mobile /v2 profile.

    The mobile bike profile carries every component inline (batteries, drive unit,
    ConnectModule, head unit, remote, ABS) with part/serial/firmware - so a standard
    app sign-in gets the same component data the Data Act registrations would.
    """
    bike_rows = db.query_bikes(conn)
    if not bike_rows:
        _sync_bikes(conn, is_euda)
        bike_rows = db.query_bikes(conn)

    result: dict[str, list[tuple[str, dict]]] = {}
    for bike_row in bike_rows:
        bike_id = bike_row["bike_id"]
        response = api.get(MOBILE_BIKE_PROFILE_V2.format(bike_id=bike_id))
        if not isinstance(response, dict):
            continue
        attrs = response.get("attributes", response)
        if not isinstance(attrs, dict):
            continue
        comps: list[tuple[str, dict]] = []
        for batt in attrs.get("batteries") or []:
            if _is_real_component(batt):
                comps.append(("battery", batt))
        for key in _MOBILE_COMPONENT_SECTIONS:
            section = attrs.get(key)
            # Normally a single object; tolerate a list defensively.
            candidates = section if isinstance(section, list) else [section]
            for item in candidates:
                if _is_real_component(item):
                    comps.append((key, item))
        if comps:
            result[bike_id] = comps
    return result


def _sync_components(conn, is_euda: bool) -> int:
    """Sync component registrations, routed by the active client.

    Components are current-state: each bike's set is replaced (not accumulated) so a
    switch between the euda and one-bike-app clients does not leave a stale union.
    """
    if is_euda:
        bike_components = _collect_components_euda()
    else:
        bike_components = _collect_components_mobile(conn, is_euda)

    count = 0
    for bike_id, comps in bike_components.items():
        if not comps:
            continue
        db.delete_components_for_bike(conn, bike_id)
        for component_type, raw in comps:
            db.save_component(conn, bike_id, component_type, raw)
            count += 1

    conn.commit()
    return count


def _sync_service(conn, is_euda: bool = True) -> int:
    """Fetch service book records for all bikes (Data Act only; euda-gated)."""
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


def _sync_software_updates(conn, is_euda: bool = True) -> int:
    """Fetch software update history for all bikes (Data Act only; euda-gated)."""
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


def _sync_capacity(conn, is_euda: bool = True) -> int:
    """Fetch battery capacity tester data for all battery components (Data Act only)."""
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

# Order matters: bikes first (others need bike IDs), components before capacity
# (capacity reads battery part/serial from components).
_SYNC_DISPATCH = {
    "bikes": _sync_bikes,
    "batteries": _sync_batteries,
    "components": _sync_components,
    "service": _sync_service,
    "software_updates": _sync_software_updates,
    "capacity": _sync_capacity,
}

_ALL_TYPES = list(_SYNC_DISPATCH)


def run_sync(data_types: list[str]) -> dict:
    """Run sync outside MCP context (for CLI/auto-sync use). Returns a per-type dict.

    Each request is routed by the active token's client (auth.token_is_euda): a euda
    token uses the EU Data Act API, the one-bike-app token uses the mobile app API.
    Per-type result: {"status", "records"} always, plus {"code", "message"} when not
    "ok". Statuses: ok | empty (euda non-EU trap) | unavailable (Data-Act-only on the
    app client) | error | auth_error - never a silent 0.
    """
    conn = db.get_db()
    results: dict[str, dict] = {}
    is_euda = auth.token_is_euda()

    try:
        for dtype in data_types:
            sync_fn = _SYNC_DISPATCH.get(dtype)
            if sync_fn is None:
                results[dtype] = {
                    "status": "error",
                    "records": 0,
                    "code": "unknown_type",
                    "message": f"Unknown type: {dtype}",
                }
                continue

            # Data-Act-only type on a non-euda client: skip the doomed call, say why.
            if dtype in DATA_ACT_ONLY and not is_euda:
                db.log_sync(conn, dtype, "unavailable", 0, REQUIRES_EUDA_MSG)
                results[dtype] = {
                    "status": "unavailable",
                    "records": 0,
                    "code": "requires_euda",
                    "message": REQUIRES_EUDA_MSG,
                }
                continue

            try:
                count = sync_fn(conn, is_euda)
            except api.BoschAuthError as e:
                results[dtype] = {
                    "status": "auth_error",
                    "records": 0,
                    "code": "auth",
                    "message": str(e),
                }
                continue
            except api.BoschAPIError as e:
                db.log_sync(conn, dtype, "error", 0, str(e))
                results[dtype] = {
                    "status": "error",
                    "records": 0,
                    "code": "error",
                    "message": str(e),
                }
                continue

            # A euda token with no bikes is the non-EU trap: surface it for bikes AND
            # everything derived from them (batteries/components), so calling a derived
            # tool first still explains the empty result instead of a bare ok/0.
            if count == 0 and is_euda and dtype in _BIKE_DERIVED and not db.query_bikes(conn):
                db.log_sync(conn, dtype, "empty", 0, EUDA_EMPTY_MSG)
                results[dtype] = {
                    "status": "empty",
                    "records": 0,
                    "code": "euda_empty",
                    "message": EUDA_EMPTY_MSG,
                }
                continue

            db.log_sync(conn, dtype, "ok", count)
            results[dtype] = {"status": "ok", "records": count}
    finally:
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

    Fetches data from Bosch and stores it in SQLite. The source depends on your
    sign-in: a standard Bosch eBike Flow account uses the mobile app API; an EU Data
    Act (euda) client uses the Data Act API. Run this to populate the cache before
    using other bosch_get_* tools, or to refresh after a ride or charge cycle.

    Bikes are identified automatically from your Bosch Flow account.
    Battery snapshots build a time series for health trend analysis.

    Service records, software-update history and capacity-tester data are only
    available with a euda (EU Data Act) client; with a standard sign-in they report
    status "unavailable" rather than an empty result.

    Args:
        data_types: What to sync. Options: "all", "bikes", "batteries",
            "components", "service", "software_updates", "capacity".
            Comma-separated for multiple, e.g. "bikes,batteries". Default: "all".

    Returns a per-type summary: status (ok/empty/unavailable/error), record count,
    and a message explaining any non-ok result.
    """
    types = [t.strip() for t in data_types.split(",")]
    if "all" in types:
        # Order matters: bikes first (others depend on bike IDs),
        # components before capacity (capacity needs part/serial from components)
        types = _ALL_TYPES

    results = await anyio.to_thread.run_sync(lambda: run_sync(types))
    return format_response(results)
