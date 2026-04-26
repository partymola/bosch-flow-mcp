"""Component registration and version tools."""

import anyio

from ..mcp_instance import mcp
from ..helpers import format_response, require_auth
from .. import db
from .sync_tools import auto_sync_if_stale


@mcp.tool()
@require_auth
async def bosch_get_components(
    bike_id: str | None = None,
    component_type: str | None = None,
) -> str:
    """List registered components for your Bosch eBike with software versions.

    Shows all components registered via the Bosch EU Data Act API: drive unit,
    battery, display, connect module, remote control, and any other registered
    parts. Includes part numbers, serial numbers, and firmware versions.

    Useful for tracking firmware versions and identifying components for
    warranty or service purposes.

    Args:
        bike_id: Optional bike UUID to filter to one bike.
        component_type: Optional component type filter, e.g. "driveUnit",
            "battery", "display", "connectedModule", "remoteControl".
    """
    await anyio.to_thread.run_sync(lambda: auto_sync_if_stale("components"))
    conn = db.get_db()
    try:
        components = db.query_components(conn, bike_id, component_type)
    finally:
        conn.close()

    # Remove verbose raw_json from list view
    for comp in components:
        comp.pop("raw_json", None)

    return format_response({"components": components, "count": len(components)})
