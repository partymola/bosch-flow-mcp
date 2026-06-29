"""Tests for the ride-activity tools (parsing, windowing, pagination, error paths).

All values here are fictional - no real bike IDs, GPS coordinates, or telemetry.
"""

import json
from datetime import datetime, timezone
from unittest.mock import patch

from tests.conftest import FAKE_BIKE_ID

GET = "bosch_flow_mcp.tools.activity_tools.api.get"


def _epoch(y, mo, d):
    return int(datetime(y, mo, d, 12, 0, tzinfo=timezone.utc).timestamp())


def _summary_item(activity_id, start_epoch, **over):
    attrs = {
        "startTime": start_epoch,
        "endTime": start_epoch + 1500,
        "timeZoneOfActivity": "Europe/London",
        "durationWithoutStops": 1500,
        "title": "Commute",
        "bikeId": FAKE_BIKE_ID,
        "startOdometer": 5000,
        "distance": 10000,
        "averageSpeed": 24.0,
        "maximumSpeed": 45.0,
        "averageCadence": 50.0,
        "maximumCadence": 80.0,
        "averageRiderPower": 100.0,
        "maximumRiderPower": 300.0,
        "averageHeartRate": None,
        "maximumHeartRate": None,
        "elevationGain": 120,
        "elevationLoss": 110,
        "caloriesBurnt": 150.0,
        "riderEnergyShare": 30,
        "co2EmissionsGrams": 30.0,
        "co2EmissionsCarEquivalentGrams": 1650.0,
        "brakeEvents": {"amountOfNormalBrakeEvents": 1, "amountOfAbsInterventionEvents": 2},
        "assistModeUsage": [
            {"name": "AUTO", "assistModeUsage": 4000},
            {"name": "TURBO", "assistModeUsage": 6000},
        ],
    }
    attrs.update(over)
    return {"id": activity_id, "type": "ActivitySummaryDto", "attributes": attrs}


def _page(items, pages=1):
    return {"meta": {"total": len(items), "pages": pages}, "data": items}


# --- summary list: parsing + window filter ---


async def test_activities_parses_and_filters_window():
    from bosch_flow_mcp.tools import activity_tools

    payload = _page(
        [
            _summary_item("in-window", _epoch(2026, 6, 15)),
            _summary_item("too-old", _epoch(2026, 1, 5)),
        ]
    )
    with patch(GET, return_value=payload):
        data = json.loads(
            await activity_tools.bosch_get_activities(
                start_date="2026-06-01", end_date="2026-06-30"
            )
        )

    assert data["count"] == 1
    ride = data["activities"][0]
    assert ride["id"] == "in-window"
    assert ride["date"] == "2026-06-15"
    assert ride["distanceKm"] == 10.0
    assert ride["avgRiderPowerW"] == 100.0
    assert ride["riderEnergySharePct"] == 30
    assert ride["assistModeMeters"] == {"AUTO": 4000, "TURBO": 6000}
    assert ride["absInterventions"] == 2
    assert ride["avgHeartRate"] is None
    assert ride["startOdometerM"] == 5000
    # CO2: raw emissions + raw car-equivalent + computed saved (car - emissions)
    assert ride["co2EmissionsGrams"] == 30.0
    assert ride["co2CarEquivalentGrams"] == 1650.0
    assert ride["co2SavedGrams"] == 1620.0


async def test_activities_bike_filter_and_limit():
    from bosch_flow_mcp.tools import activity_tools

    payload = _page(
        [
            _summary_item("a", _epoch(2026, 6, 10)),
            _summary_item("b", _epoch(2026, 6, 12)),
            _summary_item(
                "other-bike",
                _epoch(2026, 6, 11),
                bikeId="99999999-0000-0000-0000-000000000009",
            ),
        ]
    )
    with patch(GET, return_value=payload):
        data = json.loads(
            await activity_tools.bosch_get_activities(
                start_date="2026-06-01", end_date="2026-06-30", bike_id=FAKE_BIKE_ID, limit=1
            )
        )
    assert data["count"] == 1
    # most-recent-first -> ride "b" (12 Jun) wins the limit
    assert data["activities"][0]["id"] == "b"


async def test_activities_multipage_pagination():
    from bosch_flow_mcp.tools import activity_tools

    page0 = _page([_summary_item("a", _epoch(2026, 6, 10))], pages=2)
    page1 = _page([_summary_item("b", _epoch(2026, 6, 11))], pages=2)
    with patch(GET, side_effect=[page0, page1]) as m:
        data = json.loads(
            await activity_tools.bosch_get_activities(
                start_date="2026-06-01", end_date="2026-06-30"
            )
        )
    assert data["count"] == 2
    assert {r["id"] for r in data["activities"]} == {"a", "b"}
    assert m.call_count == 2
    # first call has no page param; the second must fetch page 1 (no dup, no gap)
    assert "page=" not in m.call_args_list[0].args[0]
    assert "page=1" in m.call_args_list[1].args[0]


async def test_activities_empty_or_none_response():
    from bosch_flow_mcp.tools import activity_tools

    for empty in ({}, None):
        with patch(GET, return_value=empty):
            data = json.loads(await activity_tools.bosch_get_activities())
        assert data["count"] == 0
        assert data["activities"] == []


async def test_activities_null_distance_and_null_attributes():
    from bosch_flow_mcp.tools import activity_tools

    good = _summary_item("ok", _epoch(2026, 6, 15), distance=None)
    null_attrs = {"id": "broken", "attributes": None}  # must not crash the parser
    payload = _page([good, null_attrs])
    with patch(GET, return_value=payload):
        data = json.loads(
            await activity_tools.bosch_get_activities(
                start_date="2026-06-01", end_date="2026-06-30"
            )
        )
    # null-attributes ride has no date -> dropped; good ride survives with null distance
    assert data["count"] == 1
    assert data["activities"][0]["id"] == "ok"
    assert data["activities"][0]["distanceKm"] is None


async def test_activities_auth_error():
    from bosch_flow_mcp.tools import activity_tools

    with patch(GET, side_effect=activity_tools.api.BoschAuthError("expired")):
        data = json.loads(await activity_tools.bosch_get_activities())
    assert "error" in data
    assert "auth" in data["hint"].lower()


async def test_activities_forbidden_is_graceful():
    from bosch_flow_mcp.tools import activity_tools

    with patch(GET, side_effect=activity_tools.api.BoschForbiddenError("403")):
        data = json.loads(await activity_tools.bosch_get_activities())
    assert "error" in data
    assert "one-bike-app" in data["hint"]


# --- detail track ---


async def test_activity_detail_parses_points():
    from bosch_flow_mcp.tools import activity_tools

    detail = {
        "data": {
            "id": "ride1",
            "type": "ActivityDetailList",
            "attributes": {
                "activityData": [
                    {
                        "s": 0.0,
                        "h": None,
                        "v": None,
                        "c": 0.0,
                        "lat": 12.34,
                        "lon": 56.78,
                        "r": None,
                        "p": 0.0,
                    },
                    {
                        "s": 20.0,
                        "h": 80.0,
                        "v": 6.0,
                        "c": 0.0,
                        "lat": 12.34,
                        "lon": 56.78,
                        "r": None,
                        "p": 0.0,
                    },
                ]
            },
        }
    }
    with patch(GET, return_value=detail):
        data = json.loads(await activity_tools.bosch_get_activity_detail("ride1"))

    assert data["pointCount"] == 2
    assert data["points"][1]["v"] == 6.0
    assert "cumulative distance" in data["legend"]["s"]


async def test_activity_detail_not_found():
    from bosch_flow_mcp.tools import activity_tools

    with patch(GET, return_value=None):
        data = json.loads(await activity_tools.bosch_get_activity_detail("missing"))
    assert "error" in data


async def test_activity_detail_forbidden_and_auth():
    from bosch_flow_mcp.tools import activity_tools

    with patch(GET, side_effect=activity_tools.api.BoschForbiddenError("403")):
        data = json.loads(await activity_tools.bosch_get_activity_detail("x"))
    assert "one-bike-app" in data["hint"]

    with patch(GET, side_effect=activity_tools.api.BoschAuthError("expired")):
        data = json.loads(await activity_tools.bosch_get_activity_detail("x"))
    assert "auth" in data["hint"].lower()
