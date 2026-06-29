"""Ride activity tools (per-ride summaries and per-point tracks).

Live reads from the Bosch rider-activity service (no local cache). The data is
served on its own host and is reachable with the standard one-bike-app sign-in
(scope ``activity:user:read``); a euda-only token will 403.
"""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import anyio

from .. import api
from ..config import ACTIVITY_API_BASE, ACTIVITY_DETAIL, ACTIVITY_LIST
from ..helpers import format_response, parse_date, require_auth
from ..mcp_instance import mcp

_FORBIDDEN_HINT = (
    "Needs the standard Bosch eBike Flow app sign-in (one-bike-app) with the "
    "activity:user:read scope - not a euda-only client."
)


def _tz(tz_name: str | None) -> timezone | ZoneInfo:
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            pass
    return timezone.utc


def _iso(epoch: int | None, tz_name: str | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, _tz(tz_name)).isoformat()


def _local_date(epoch: int | None, tz_name: str | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, _tz(tz_name)).date().isoformat()


def _parse_summary(item: dict) -> dict:
    """Flatten one ActivitySummaryDto into a clean, unit-labelled dict."""
    a = item.get("attributes") or {}
    dist = a.get("distance")
    brake = a.get("brakeEvents") or {}
    co2_emit = a.get("co2EmissionsGrams")
    co2_car = a.get("co2EmissionsCarEquivalentGrams")
    co2_saved = (
        round(co2_car - co2_emit, 1) if co2_emit is not None and co2_car is not None else None
    )
    # assistModeUsage values are METRES ridden per mode (they approximately sum to
    # distance - independent integer-metre rounding, with occasional larger gaps).
    modes = {
        m.get("name"): m.get("assistModeUsage")
        for m in (a.get("assistModeUsage") or [])
        if m.get("name")
    }
    return {
        "id": item.get("id"),
        "date": _local_date(a.get("startTime"), a.get("timeZoneOfActivity")),
        "startEpoch": a.get("startTime"),
        "title": a.get("title") or None,
        "startTime": _iso(a.get("startTime"), a.get("timeZoneOfActivity")),
        "endTime": _iso(a.get("endTime"), a.get("timeZoneOfActivity")),
        "timeZone": a.get("timeZoneOfActivity"),
        "durationSec": a.get("durationWithoutStops"),
        "distanceM": dist,
        "distanceKm": round(dist / 1000, 2) if dist is not None else None,
        "avgSpeedKmh": a.get("averageSpeed"),
        "maxSpeedKmh": a.get("maximumSpeed"),
        "avgCadence": a.get("averageCadence"),
        "maxCadence": a.get("maximumCadence"),
        "avgRiderPowerW": a.get("averageRiderPower"),
        "maxRiderPowerW": a.get("maximumRiderPower"),
        "avgHeartRate": a.get("averageHeartRate"),
        "maxHeartRate": a.get("maximumHeartRate"),
        "elevationGainM": a.get("elevationGain"),
        "elevationLossM": a.get("elevationLoss"),
        "caloriesBurnt": a.get("caloriesBurnt"),
        "riderEnergySharePct": a.get("riderEnergyShare"),
        "co2EmissionsGrams": co2_emit,
        "co2CarEquivalentGrams": co2_car,
        "co2SavedGrams": co2_saved,
        "assistModeMeters": modes,
        "absInterventions": brake.get("amountOfAbsInterventionEvents"),
        "normalBrakeEvents": brake.get("amountOfNormalBrakeEvents"),
        "startOdometerM": a.get("startOdometer"),
        "bikeId": a.get("bikeId"),
    }


_PAGE_SIZE = 200


def _fetch_all_summaries(max_pages: int = 25) -> tuple[list[dict], bool]:
    """Fetch all activity summaries.

    Uses ``?size=200`` so a normal history returns in a single request; pages
    through only if the account has more rides than that. Returns
    ``(items, truncated)`` - truncated is True if the page cap was hit.
    """
    first = api.get(f"{ACTIVITY_LIST}?size={_PAGE_SIZE}", base=ACTIVITY_API_BASE)
    if not first:
        return [], False
    items = list(first.get("data", []))
    pages = (first.get("meta") or {}).get("pages", 1) or 1
    for page in range(1, min(pages, max_pages)):
        resp = api.get(f"{ACTIVITY_LIST}?size={_PAGE_SIZE}&page={page}", base=ACTIVITY_API_BASE)
        if not resp:
            break
        items.extend(resp.get("data", []))
    return items, pages > max_pages


@mcp.tool()
@require_auth
async def bosch_get_activities(
    start_date: str | None = None,
    end_date: str | None = None,
    bike_id: str | None = None,
    limit: int | None = None,
) -> str:
    """List e-bike rides with per-ride summary metrics.

    Live read from the Bosch rider-activity API (no cache). Each ride includes
    distance, elevation gain/loss, avg/max speed, cadence, measured rider power,
    calories, rider-vs-motor energy share, assist-mode distance split (metres
    per mode), CO2 (emissions / car-equivalent / saved), and ABS/brake events.
    Heart rate is not recorded by the bike (it has no HR sensor) - pair with a
    wrist device for HR.

    Args:
        start_date: Window start (YYYY-MM-DD, YYYY-MM, or "30d"). Default 30 days ago.
        end_date: Window end. Default today.
        bike_id: Optional bike UUID filter.
        limit: Optional cap on rides returned (most recent first).
    """
    start, end = parse_date(start_date, end_date, default_days=30)
    try:
        raw, truncated = await anyio.to_thread.run_sync(_fetch_all_summaries)
    except api.BoschAuthError as e:
        return format_response({"error": str(e), "hint": "Run: bosch-flow-mcp auth"})
    except api.BoschForbiddenError:
        return format_response(
            {
                "error": "Activities are not available with your current sign-in.",
                "hint": _FORBIDDEN_HINT,
            }
        )
    except api.BoschAPIError as e:
        return format_response({"error": str(e)})

    rides = []
    for item in raw:
        s = _parse_summary(item)
        if s["date"] is None:
            continue
        d = date.fromisoformat(s["date"])
        if d < start or d > end:
            continue
        if bike_id and s["bikeId"] != bike_id:
            continue
        rides.append(s)

    rides.sort(key=lambda r: r["startEpoch"] or 0, reverse=True)
    if limit is not None and limit > 0:
        rides = rides[:limit]

    result = {
        "activities": rides,
        "count": len(rides),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
    }
    if truncated:
        result["note"] = "Ride history exceeded the fetch cap; some older rides were not scanned."
    return format_response(result)


@mcp.tool()
@require_auth
async def bosch_get_activity_detail(activity_id: str) -> str:
    """Get the per-point track for one ride (GPS + speed/elevation/cadence/power).

    Live read. Returns a downsampled track (~500 points per ride): per-point
    cumulative distance (m), speed (km/h), elevation (m), cadence, rider power
    (W), and GPS lat/lon. Get the activity_id from bosch_get_activities.

    Args:
        activity_id: The ride UUID from bosch_get_activities.
    """
    path = ACTIVITY_DETAIL.format(activity_id=activity_id)
    try:
        det = await anyio.to_thread.run_sync(lambda: api.get(path, base=ACTIVITY_API_BASE))
    except api.BoschAuthError as e:
        return format_response({"error": str(e), "hint": "Run: bosch-flow-mcp auth"})
    except api.BoschForbiddenError:
        return format_response(
            {
                "error": "Activity detail is not available with your current sign-in.",
                "hint": _FORBIDDEN_HINT,
            }
        )
    except api.BoschAPIError as e:
        return format_response({"error": str(e)})

    if not det:
        return format_response({"error": f"Activity '{activity_id}' not found or has no detail."})

    points = ((det.get("data") or {}).get("attributes") or {}).get("activityData") or []
    return format_response(
        {
            "id": activity_id,
            "legend": {
                "s": "cumulative distance (m)",
                "v": "speed (km/h)",
                "h": "elevation (m)",
                "c": "cadence (rpm)",
                "p": "rider power (W)",
                "lat": "latitude",
                "lon": "longitude",
                "r": "heart rate (null - bike has no sensor)",
            },
            "pointCount": len(points),
            "points": points,
        }
    )
