"""Battery health, state-of-charge, and capacity test tools."""

import anyio

from .. import api, db
from ..config import MOBILE_STATE_OF_CHARGE
from ..helpers import empty_data_note, format_response, parse_date, require_auth
from ..mcp_instance import mcp
from .sync_tools import auto_sync_if_stale


@mcp.tool()
@require_auth
async def bosch_get_batteries(
    bike_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    latest_only: bool = True,
) -> str:
    """Get battery state and health for your Bosch eBike.

    Returns charge level, remaining energy, total capacity, charge cycles
    (total / on-bike / off-bike), lifetime energy delivered, and software version.

    Each sync captures a snapshot, building a time series of battery health.
    Use latest_only=False with a date range to see history.

    Args:
        bike_id: Optional bike UUID to filter to one bike.
        start_date: Start date (YYYY-MM-DD, YYYY-MM, or Nd like "30d"). Default: 30 days ago.
        end_date: End date. Default: today.
        latest_only: If True (default), return only the most recent snapshot per bike.
            Set to False to return all snapshots in the date range.
    """
    await anyio.to_thread.run_sync(lambda: auto_sync_if_stale("batteries"))
    conn = db.get_db()
    try:
        if latest_only:
            snapshots = db.query_battery_latest(conn, bike_id)
        else:
            start, end = parse_date(start_date, end_date, default_days=30)
            snapshots = db.query_batteries(conn, bike_id, start.isoformat(), end.isoformat())
        note = empty_data_note(conn, "batteries", fallback_type="bikes") if not snapshots else {}
    finally:
        conn.close()

    return format_response({"batteries": snapshots, "count": len(snapshots), **note})


@mcp.tool()
@require_auth
async def bosch_get_soc(bike_id: str) -> str:
    """Get live state-of-charge from the Bosch ConnectModule.

    Returns real-time battery percentage, charging status, remaining energy,
    and reachable range per assist mode (eco/tour/sport/turbo).

    Requires a ConnectModule on the bike. Data is only available when the
    bike is powered on, charging, or recently active.

    Note: This calls the Bosch mobile API live - no caching.

    Args:
        bike_id: The bike UUID from bosch_get_bikes.
    """
    path = MOBILE_STATE_OF_CHARGE.format(bike_id=bike_id)

    try:
        soc = await anyio.to_thread.run_sync(lambda: api.get(path))
    except api.BoschAuthError as e:
        return format_response(
            {
                "error": str(e),
                "hint": "Run: bosch-flow-mcp auth",
            }
        )
    except api.BoschForbiddenError:
        return format_response(
            {
                "error": "Live state-of-charge is not available with your current sign-in.",
                "hint": "It needs the standard Bosch eBike Flow app sign-in (one-bike-app), "
                "not an EU Data Act (euda) client - or the bike may be offline.",
            }
        )
    except api.BoschAPIError as e:
        return format_response({"error": str(e)})

    if soc is None:
        return format_response(
            {
                "error": "State-of-charge not available.",
                "hint": "Bike may be offline or ConnectModule not installed.",
            }
        )

    # Save snapshot to DB for historical tracking
    conn = db.get_db()
    try:
        db.save_soc_snapshot(conn, bike_id, soc)
        conn.commit()
    finally:
        conn.close()

    return format_response(soc)


@mcp.tool()
@require_auth
async def bosch_get_capacity(
    part_number: str | None = None,
    serial_number: str | None = None,
) -> str:
    """Get battery capacity tester diagnostic results.

    Shows battery health data from Bosch's official capacity tester tool,
    typically done at dealer service appointments. Includes remaining capacity
    percentage vs. original specification.

    Capacity-tester data comes only from the EU Data Act API; with a standard Bosch
    eBike Flow sign-in this is empty and the result explains why.

    Args:
        part_number: Optional battery part number to filter results.
        serial_number: Optional battery serial number to filter results.
    """
    await anyio.to_thread.run_sync(lambda: auto_sync_if_stale("capacity"))
    conn = db.get_db()
    try:
        tests = db.query_capacity_tests(conn, part_number, serial_number)
        note = empty_data_note(conn, "capacity") if not tests else {}
    finally:
        conn.close()

    return format_response({"capacity_tests": tests, "count": len(tests), **note})
