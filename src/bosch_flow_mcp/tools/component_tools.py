"""Component registration and version tools."""

import anyio

from .. import db
from ..helpers import empty_data_note, format_response, require_auth
from ..mcp_instance import mcp
from .sync_tools import auto_sync_if_stale


@mcp.tool()
@require_auth
async def bosch_get_components(
    bike_id: str | None = None,
    component_type: str | None = None,
) -> str:
    """List registered components for your Bosch eBike with software versions.

    Shows the bike's components - drive unit, battery, ConnectModule, head unit,
    remote control, ABS - with part numbers, serial numbers, and firmware versions.
    The source depends on your sign-in: a standard Bosch eBike Flow account reads
    them from the bike profile (mobile app API); an EU Data Act (euda) client reads
    them from the Data Act registrations endpoint.

    Useful for tracking firmware versions and identifying components for
    warranty or service purposes.

    Args:
        bike_id: Optional bike UUID to filter to one bike.
        component_type: Optional component type filter, e.g. "driveUnit",
            "battery", "headUnit", "connectedModule", "remoteControl".
    """
    await anyio.to_thread.run_sync(lambda: auto_sync_if_stale("components"))
    conn = db.get_db()
    try:
        components = db.query_components(conn, bike_id, component_type)
        note = empty_data_note(conn, "components", fallback_type="bikes") if not components else {}
    finally:
        conn.close()

    # Remove verbose raw_json from list view
    for comp in components:
        comp.pop("raw_json", None)

    return format_response({"components": components, "count": len(components), **note})
