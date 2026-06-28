"""Battery trend analysis tool."""

from collections import defaultdict
from datetime import date

import anyio

from .. import db
from ..helpers import format_response, require_auth
from ..mcp_instance import mcp
from .sync_tools import auto_sync_if_stale


def _week_key(dt_str: str) -> str:
    """Return ISO week key (YYYY-Www) from an ISO datetime string."""
    d = date.fromisoformat(dt_str[:10])
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _month_key(dt_str: str) -> str:
    return dt_str[:7]  # YYYY-MM


def _quarter_key(dt_str: str) -> str:
    d = date.fromisoformat(dt_str[:10])
    q = (d.month - 1) // 3 + 1
    return f"{d.year}-Q{q}"


@mcp.tool()
@require_auth
async def bosch_battery_trends(
    bike_id: str | None = None,
    period: str = "monthly",
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Analyse battery health trends for your Bosch eBike over time.

    Computes per-period averages and deltas for:
    - Charge cycle count (total, rate of accumulation)
    - Average battery level at time of sync
    - Lifetime energy delivered (total kWh ever pushed through the battery)
    - Remaining energy trend (indicates capacity degradation over time)

    A declining remaining_energy_wh at a constant charge level indicates
    the battery capacity is degrading. Compare early and recent snapshots
    for a long-term health picture.

    Args:
        bike_id: Optional bike UUID. If omitted, includes all bikes.
        period: Aggregation period. Options: "weekly", "monthly" (default), "quarterly".
        start_date: Start date (YYYY-MM-DD, YYYY-MM, Nd, or None for all data).
        end_date: End date. Default: today.
    """
    await anyio.to_thread.run_sync(lambda: auto_sync_if_stale("batteries"))
    conn = db.get_db()
    try:
        # Fetch all snapshots for the date range
        if start_date or end_date:
            from ..helpers import parse_date

            start, end = parse_date(start_date, end_date, default_days=180)
            start_str, end_str = start.isoformat(), end.isoformat()
        else:
            start_str, end_str = "2000-01-01", "2999-12-31"
        snapshots = db.query_batteries(conn, bike_id, start_str, end_str)
    finally:
        conn.close()

    if not snapshots:
        return format_response(
            {
                "message": "No battery data found. Run bosch_sync first.",
                "trends": [],
            }
        )

    # Choose period key function
    if period == "weekly":
        key_fn = _week_key
    elif period == "quarterly":
        key_fn = _quarter_key
    else:
        key_fn = _month_key

    # Group by (bike_id, battery_id, period)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for snap in snapshots:
        k = (snap["bike_id"], snap.get("battery_id") or "battery", key_fn(snap["captured_at"]))
        groups[k].append(snap)

    trends = []
    for (b_id, bat_id, p), snaps in sorted(groups.items()):
        valid_levels = [s["battery_level"] for s in snaps if s["battery_level"] is not None]
        valid_cycles = [
            s["charge_cycles_total"] for s in snaps if s["charge_cycles_total"] is not None
        ]
        valid_remaining = [
            s["remaining_energy_wh"] for s in snaps if s["remaining_energy_wh"] is not None
        ]
        valid_lifetime = [
            s["delivered_wh_lifetime"] for s in snaps if s["delivered_wh_lifetime"] is not None
        ]

        trends.append(
            {
                "bike_id": b_id,
                "battery_id": bat_id,
                "period": p,
                "snapshots": len(snaps),
                "avg_battery_level_pct": round(sum(valid_levels) / len(valid_levels), 1)
                if valid_levels
                else None,
                "charge_cycles_end": max(valid_cycles) if valid_cycles else None,
                "charge_cycles_start": min(valid_cycles) if valid_cycles else None,
                "charge_cycles_added": (max(valid_cycles) - min(valid_cycles))
                if len(valid_cycles) >= 2
                else None,
                "avg_remaining_energy_wh": round(sum(valid_remaining) / len(valid_remaining), 1)
                if valid_remaining
                else None,
                "max_delivered_wh_lifetime": max(valid_lifetime) if valid_lifetime else None,
            }
        )

    return format_response(
        {
            "period": period,
            "bike_id": bike_id,
            "data_points": len(snapshots),
            "trends": trends,
        }
    )
