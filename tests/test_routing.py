"""Tests for client-aware routing, components-from-mobile, and the euda-empty trap.

Routing is driven through the REAL current_client_id()/token_is_euda() chain off a
fictional token file (token_as fixture); only api.get (the HTTP boundary) is mocked,
and the mock records every (path, base) so tests can assert which API was actually hit.
"""

from unittest.mock import patch

import pytest

from bosch_flow_mcp import db
from bosch_flow_mcp.config import (
    BES3_BIKES,
    DATA_ACT_API_BASE,
    MOBILE_API_BASE,
    MOBILE_BIKE_PROFILE_LIST,
)
from bosch_flow_mcp.tools import sync_tools
from tests.conftest import FAKE_BIKE_ID, FAKE_V2_PROFILE

DATA_ACT_ONLY_TYPES = ["service", "software_updates", "capacity"]
MOBILE_TYPES = ["bikes", "batteries", "components"]


@pytest.fixture
def routed_db(tmp_path, monkeypatch):
    """Point run_sync's db at a temp file."""
    db_path = tmp_path / "routing.db"
    monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(db_path))
    return db_path


def make_api_mock(record, *, empty_bikes=False):
    """A route-strict api.get: returns per-base fixtures and records every call.

    Mobile and Data Act paths return different shapes, so a test asserting "only the
    mobile base was hit" actually fails if the code calls the wrong host.
    """

    def _get(path, base=MOBILE_API_BASE, retries=3):
        record.append((path, base))
        if base == MOBILE_API_BASE:
            if path == MOBILE_BIKE_PROFILE_LIST:
                if empty_bikes:
                    return []
                return [
                    {
                        "id": FAKE_BIKE_ID,
                        "brandName": "TestBrand",
                        "name": "Test Bike",
                        "frameNumber": "F1",
                    }
                ]
            if path.startswith("/v2/bike-profile/"):
                return dict(FAKE_V2_PROFILE)
            return {}
        if base == DATA_ACT_API_BASE:
            if path == BES3_BIKES:
                return {"bikes": [] if empty_bikes else [{"id": FAKE_BIKE_ID, "attributes": {}}]}
            if "/bikes/" in path:  # BES3_BIKE single
                return {
                    "attributes": {
                        "batteries": [
                            {
                                "serialNumber": "DABATT1",
                                "partNumber": "DAPART1",
                                "totalEnergy": 600.0,
                                "numberOfFullChargeCycles": {"total": 2},
                            }
                        ]
                    }
                }
            if "registrations" in path:
                if empty_bikes:  # non-EU euda account: Data Act shares nothing
                    return {"registrations": []}
                return {
                    "registrations": [
                        {
                            "bikeId": FAKE_BIKE_ID,
                            "components": [
                                {
                                    "componentType": "driveUnit",
                                    "partNumber": "DA-DU-1",
                                    "serialNumber": "DA-DU-SN1",
                                    "productName": "DataAct Drive Unit",
                                    "softwareVersion": "4.0",
                                },
                                {
                                    "componentType": "battery",
                                    "partNumber": "DA-BATT-1",
                                    "serialNumber": "DA-BATT-SN1",
                                    "productName": "DataAct Battery",
                                    "softwareVersion": "4.0",
                                },
                            ],
                        }
                    ]
                }
            if "service-records" in path:
                return {"serviceRecords": []}
            if "installation-reports" in path:
                return {"installationReports": []}
            if "capacity-testers" in path:
                return []
            return {}
        return {}

    return _get


def _bases_used(record):
    return {base for _, base in record}


# --- Routing: which API each client hits ---


def test_one_bike_app_bikes_uses_mobile(token_as, routed_db):
    token_as("one-bike-app")
    record = []
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=make_api_mock(record)):
        results = sync_tools.run_sync(["bikes"])
    assert results["bikes"]["status"] == "ok"
    assert results["bikes"]["records"] == 1
    assert DATA_ACT_API_BASE not in _bases_used(record)
    assert MOBILE_API_BASE in _bases_used(record)


def test_euda_bikes_uses_data_act(token_as, routed_db):
    token_as("euda-test")
    record = []
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=make_api_mock(record)):
        results = sync_tools.run_sync(["bikes"])
    assert results["bikes"]["status"] == "ok"
    assert MOBILE_API_BASE not in _bases_used(record)
    assert DATA_ACT_API_BASE in _bases_used(record)


def test_one_bike_app_components_and_batteries_use_mobile(token_as, routed_db):
    token_as("one-bike-app")
    record = []
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=make_api_mock(record)):
        sync_tools.run_sync(["bikes", "batteries", "components"])
    assert DATA_ACT_API_BASE not in _bases_used(record)


@pytest.mark.parametrize("dtype", DATA_ACT_ONLY_TYPES)
def test_one_bike_app_data_act_only_is_unavailable_no_call(token_as, routed_db, dtype):
    """Data-Act-only types skip the doomed call and report unavailable (not error/0)."""
    token_as("one-bike-app")
    record = []
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=make_api_mock(record)):
        results = sync_tools.run_sync([dtype])
    assert results[dtype]["status"] == "unavailable"
    assert results[dtype]["code"] == "requires_euda"
    assert "Data Act" in results[dtype]["message"]
    assert record == []  # structurally no network call was made


@pytest.mark.parametrize("dtype", DATA_ACT_ONLY_TYPES)
def test_euda_data_act_only_runs_on_data_act(token_as, routed_db, dtype):
    """With a euda client the same types route to the Data Act API."""
    token_as("euda-test")
    record = []
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=make_api_mock(record)):
        # sync bikes/components first so service/capacity have something to iterate
        results = sync_tools.run_sync(["bikes", "components", dtype])
    assert results[dtype]["status"] == "ok"
    assert MOBILE_API_BASE not in _bases_used(record)


# --- The euda-empty (non-EU) trap: the original 2-month silent failure ---


def test_euda_empty_bikes_emits_signal_not_silent_zero(token_as, routed_db):
    token_as("euda-test")
    record = []
    mock = make_api_mock(record, empty_bikes=True)
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=mock):
        results = sync_tools.run_sync(["bikes"])
    assert results["bikes"]["status"] == "empty"
    assert results["bikes"]["code"] == "euda_empty"
    assert results["bikes"]["records"] == 0
    assert "config/bosch_config.json" in results["bikes"]["message"]


@pytest.mark.parametrize("dtype", ["batteries", "components"])
def test_euda_empty_derived_type_emits_signal_when_called_first(token_as, routed_db, dtype):
    """Calling a bike-derived tool directly on a euda non-EU account must still signal,
    not return a silent ok/0 (regression for the call-order gap)."""
    token_as("euda-test")
    record = []
    mock = make_api_mock(record, empty_bikes=True)
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=mock):
        results = sync_tools.run_sync([dtype])
    assert results[dtype]["status"] == "empty"
    assert results[dtype]["code"] == "euda_empty"


def test_one_bike_app_empty_bikes_is_plain_ok(token_as, routed_db):
    """A standard account with genuinely no bikes is ok/0, not the euda_empty trap."""
    token_as("one-bike-app")
    record = []
    mock = make_api_mock(record, empty_bikes=True)
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=mock):
        results = sync_tools.run_sync(["bikes"])
    assert results["bikes"]["status"] == "ok"
    assert results["bikes"]["records"] == 0
    assert "code" not in results["bikes"]


# --- Components from the mobile profile ---


def test_components_from_mobile_extracts_all_sections(token_as, routed_db):
    token_as("one-bike-app")
    record = []
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=make_api_mock(record)):
        results = sync_tools.run_sync(["components"])
    assert results["components"]["status"] == "ok"
    conn = db.get_db(routed_db)
    comps = db.query_components(conn, FAKE_BIKE_ID, None)
    conn.close()
    types = sorted(c["component_type"] for c in comps)
    # 2 batteries + driveUnit + connectedModule + remoteControl; ABS null & headUnit absent skipped
    assert types == ["battery", "battery", "connectedModule", "driveUnit", "remoteControl"]
    assert results["components"]["records"] == 5


def test_components_from_mobile_multi_battery_distinct(token_as, routed_db):
    token_as("one-bike-app")
    record = []
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=make_api_mock(record)):
        sync_tools.run_sync(["components"])
    conn = db.get_db(routed_db)
    batteries = db.query_components(conn, FAKE_BIKE_ID, "battery")
    conn.close()
    serials = {c["serial_number"] for c in batteries}
    assert serials == {"BATTSN0001", "BATTSN0002"}


def test_components_from_mobile_resync_idempotent(token_as, routed_db):
    """Re-syncing replaces the set; no duplicate rows accumulate."""
    token_as("one-bike-app")
    record = []
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=make_api_mock(record)):
        sync_tools.run_sync(["components"])
        sync_tools.run_sync(["components"])
    conn = db.get_db(routed_db)
    comps = db.query_components(conn, FAKE_BIKE_ID, None)
    conn.close()
    assert len(comps) == 5


def test_components_resync_removes_vanished_component(token_as, routed_db):
    """A component absent from a later profile must be reconciled away (delete-then-
    insert), not left as a stale row. Locks in delete_components_for_bike."""
    token_as("one-bike-app")
    record = []
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=make_api_mock(record)):
        sync_tools.run_sync(["components"])

    reduced = dict(FAKE_V2_PROFILE)
    reduced.pop("remoteControl")  # this component "vanishes" on the next sync

    def mock_reduced(path, base=MOBILE_API_BASE, retries=3):
        if path == MOBILE_BIKE_PROFILE_LIST:
            return [{"id": FAKE_BIKE_ID, "brandName": "TestBrand"}]
        if path.startswith("/v2/bike-profile/"):
            return reduced
        return {}

    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=mock_reduced):
        sync_tools.run_sync(["components"])

    conn = db.get_db(routed_db)
    comps = db.query_components(conn, FAKE_BIKE_ID, None)
    conn.close()
    types = [c["component_type"] for c in comps]
    assert "remoteControl" not in types
    assert len(comps) == 4


def test_components_from_mobile_skips_empty_section_dict(token_as, routed_db):
    """A section present as a dict but with no part/serial/name is not a component."""
    token_as("one-bike-app")
    profile = dict(FAKE_V2_PROFILE)
    profile["headUnit"] = {"productCode": "X"}  # dict, but no identifying fields

    def mock(path, base=MOBILE_API_BASE, retries=3):
        if path == MOBILE_BIKE_PROFILE_LIST:
            return [{"id": FAKE_BIKE_ID}]
        if path.startswith("/v2/bike-profile/"):
            return profile
        return {}

    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=mock):
        sync_tools.run_sync(["components"])
    conn = db.get_db(routed_db)
    head_units = db.query_components(conn, FAKE_BIKE_ID, "headUnit")
    conn.close()
    assert head_units == []


def test_components_from_mobile_null_serial_no_duplicate_on_resync(token_as, routed_db):
    """End-to-end: a mobile component with no serial does not accumulate duplicates."""
    token_as("one-bike-app")
    profile = {
        "id": FAKE_BIKE_ID,
        "driveUnit": {"partNumber": "NOSERIAL-DU", "productName": "DU no serial"},
    }

    def mock(path, base=MOBILE_API_BASE, retries=3):
        if path == MOBILE_BIKE_PROFILE_LIST:
            return [{"id": FAKE_BIKE_ID}]
        if path.startswith("/v2/bike-profile/"):
            return profile
        return {}

    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=mock):
        sync_tools.run_sync(["components"])
        sync_tools.run_sync(["components"])
    conn = db.get_db(routed_db)
    comps = db.query_components(conn, FAKE_BIKE_ID, "driveUnit")
    conn.close()
    assert len(comps) == 1


def test_euda_batteries_uses_data_act(token_as, routed_db):
    token_as("euda-test")
    record = []
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=make_api_mock(record)):
        results = sync_tools.run_sync(["bikes", "batteries"])
    assert results["batteries"]["status"] == "ok"
    assert MOBILE_API_BASE not in _bases_used(record)


def test_components_euda_uses_registrations(token_as, routed_db):
    token_as("euda-test")
    record = []
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=make_api_mock(record)):
        sync_tools.run_sync(["bikes", "components"])
    conn = db.get_db(routed_db)
    comps = db.query_components(conn, FAKE_BIKE_ID, None)
    conn.close()
    assert any(c["part_number"] == "DA-DU-1" for c in comps)
    assert MOBILE_API_BASE not in _bases_used(record)


def test_batteries_from_mobile_v2_multi(token_as, routed_db):
    token_as("one-bike-app")
    record = []
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=make_api_mock(record)):
        results = sync_tools.run_sync(["bikes", "batteries"])
    assert results["batteries"]["records"] == 2  # two batteries in the profile


# --- Status model robustness ---


def test_run_sync_continues_past_forbidden(token_as, routed_db):
    """A stray 403 in one type becomes status error but does not abort the run."""
    token_as("one-bike-app")

    def _raise_forbidden(path, base=MOBILE_API_BASE, retries=3):
        raise sync_tools.api.BoschForbiddenError("403")

    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=_raise_forbidden):
        results = sync_tools.run_sync(["bikes", "service"])
    assert results["bikes"]["status"] == "error"
    assert results["service"]["status"] == "unavailable"  # gated before the call


def test_unknown_type_reports_error_shape(routed_db, token_as):
    token_as("one-bike-app")
    results = sync_tools.run_sync(["nope"])
    assert results["nope"]["status"] == "error"
    assert results["nope"]["code"] == "unknown_type"


def test_result_shape_contract(token_as, routed_db):
    """Every result carries records:int; every non-ok carries code+message."""
    token_as("one-bike-app")
    record = []
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=make_api_mock(record)):
        results = sync_tools.run_sync(["bikes", "service"])
    for res in results.values():
        assert isinstance(res["records"], int)
        if res["status"] != "ok":
            assert "code" in res and "message" in res
