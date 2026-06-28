"""Service book and software update history tools."""

import anyio

from .. import db
from ..helpers import empty_data_note, format_response, parse_date, require_auth
from ..mcp_instance import mcp
from .sync_tools import auto_sync_if_stale


@mcp.tool()
@require_auth
async def bosch_get_service_records(
    bike_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Get the digital service book history for your Bosch eBike.

    Returns all service records logged by Bosch dealers and service centres,
    including dates and descriptions of work performed.

    Service records come only from the EU Data Act API; with a standard Bosch eBike
    Flow sign-in this is empty and the result explains why (register a euda client).

    Args:
        bike_id: Optional bike UUID to filter to one bike.
        start_date: Start date (YYYY-MM-DD, YYYY-MM, or Nd). Default: all records.
        end_date: End date. Default: today.
    """
    await anyio.to_thread.run_sync(lambda: auto_sync_if_stale("service"))
    conn = db.get_db()
    try:
        start_str = None
        end_str = None
        if start_date or end_date:
            start, end = parse_date(start_date, end_date, default_days=365)
            start_str = start.isoformat()
            end_str = end.isoformat()
        records = db.query_service_records(conn, bike_id, start_str, end_str)
        note = empty_data_note(conn, "service") if not records else {}
    finally:
        conn.close()

    for r in records:
        r.pop("raw_json", None)

    return format_response({"service_records": records, "count": len(records), **note})


@mcp.tool()
@require_auth
async def bosch_get_software_updates(
    bike_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Get software update installation history for your Bosch eBike.

    Returns all firmware/software update reports, showing which components
    were updated, from which version to which version, and when.

    This history comes only from the EU Data Act API; with a standard Bosch eBike
    Flow sign-in it is empty (current firmware is still available via components).

    Args:
        bike_id: Optional bike UUID to filter to one bike.
        start_date: Start date (YYYY-MM-DD, YYYY-MM, or Nd). Default: all records.
        end_date: End date. Default: today.
    """
    await anyio.to_thread.run_sync(lambda: auto_sync_if_stale("software_updates"))
    conn = db.get_db()
    try:
        start_str = None
        end_str = None
        if start_date or end_date:
            start, end = parse_date(start_date, end_date, default_days=365)
            start_str = start.isoformat()
            end_str = end.isoformat()
        updates = db.query_software_updates(conn, bike_id, start_str, end_str)
        note = empty_data_note(conn, "software_updates") if not updates else {}
    finally:
        conn.close()

    for u in updates:
        u.pop("raw_json", None)

    return format_response({"software_updates": updates, "count": len(updates), **note})
