"""Bike profile tools."""

import anyio

from .. import db
from ..helpers import format_response, require_auth
from ..mcp_instance import mcp
from .sync_tools import auto_sync_if_stale


@mcp.tool()
@require_auth
async def bosch_get_bikes() -> str:
    """List all Bosch eBikes registered to your Flow account.

    Returns bike names, brand, and frame numbers. Uses local cache
    (auto-syncs if stale). Run bosch_sync first if the list is empty.

    Returns a list of bikes with id, name, brand_name, and frame_number.
    """
    await anyio.to_thread.run_sync(lambda: auto_sync_if_stale("bikes"))
    conn = db.get_db()
    try:
        bikes = db.query_bikes(conn)
    finally:
        conn.close()

    # Strip raw_json from the list view (too verbose)
    for bike in bikes:
        bike.pop("raw_json", None)

    return format_response({"bikes": bikes, "count": len(bikes)})


@mcp.tool()
@require_auth
async def bosch_get_bike(bike_id: str) -> str:
    """Get detailed profile for a single Bosch eBike.

    Returns full bike details including brand, model, frame number,
    and the cached raw API response which may include component info.

    Args:
        bike_id: The bike UUID from bosch_get_bikes.
    """
    await anyio.to_thread.run_sync(lambda: auto_sync_if_stale("bikes"))
    conn = db.get_db()
    try:
        bike = db.query_bike(conn, bike_id)
    finally:
        conn.close()

    if not bike:
        return format_response({"error": f"Bike '{bike_id}' not found. Run bosch_sync first."})
    return format_response(bike)
