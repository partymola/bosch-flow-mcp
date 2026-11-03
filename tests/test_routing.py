"""Tests for client-aware routing, components-from-mobile, and the euda-empty trap.

Routing is driven through the REAL current_client_id()/token_is_euda() chain off a
fictional token file (token_as fixture); only api.get (the HTTP boundary) is mocked,
and the mock records every (path, base) so tests can assert which API was actually hit.
"""

import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

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


# --- The euda-empty (non-EU) trap: a silent zero ---


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
    """Syncing a bike-derived type with no bikes synced first, on a euda non-EU account,
    must still signal, not report a silent ok/0."""
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


# --- A euda token with no bikes cached: the bike list answers for every type ---


NOT_BIKES = [dtype for dtype in sync_tools._ALL_TYPES if dtype != "bikes"]
EUDA_EMPTY_RESULT = {
    "status": "empty",
    "records": 0,
    "code": "euda_empty",
    "message": sync_tools.EUDA_EMPTY_MSG,
}
RATE_LIMITED_RESULT = {
    "status": "rate_limited",
    "records": 0,
    "code": "rate_limited",
    "message": sync_tools.RATE_LIMITED_MSG,
}


def _error_result(kind):
    return {
        "status": "error",
        "records": 0,
        "code": "error",
        "message": f"{sync_tools.API_FAILED_MSG} ({kind})",
    }


# HTTP status -> (per-type result, sync_log status)
FAILED_RESULTS = {
    429: (RATE_LIMITED_RESULT, "error"),
    500: (_error_result("BoschAPIError"), "error"),
    401: (
        {
            "status": "auth_error",
            "records": 0,
            "code": "auth",
            "message": sync_tools.AUTH_FAILED_MSG,
        },
        "auth_error",
    ),
    403: (_error_result("BoschForbiddenError"), "error"),
}
BIKE = {"id": FAKE_BIKE_ID, "brandName": "TestBrand"}


@pytest.fixture
def refuse_every_request(monkeypatch):
    """Answer every request with an HTTP status, through the real api.get."""

    def _refuse(status):
        requested = []

        def urlopen(req, timeout=None):
            requested.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, status, "refused", {}, None)

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        return requested

    return _refuse


def _sync_rows(db_path):
    conn = db.get_db(db_path)
    rows = conn.execute("SELECT data_type, status, notes FROM sync_log ORDER BY id").fetchall()
    conn.close()
    return [tuple(row) for row in rows]


def _last_sync_time(db_path, dtype):
    conn = db.get_db(db_path)
    last_sync = db.get_last_sync_time(conn, dtype)
    conn.close()
    return last_sync


def _seed_a_bike(db_path):
    conn = db.get_db(db_path)
    db.save_bike(conn, FAKE_BIKE_ID, {"name": "Seeded"})
    conn.commit()
    conn.close()


def _bike_list_requests(record):
    return [call for call in record if call[0] in (BES3_BIKES, MOBILE_BIKE_PROFILE_LIST)]


def _bike_list_answers(record, answer, *, empty_bikes=True):
    """make_api_mock, with this answer or exception from the Data Act bike list."""
    full = make_api_mock(record, empty_bikes=empty_bikes)

    def _get(path, base=MOBILE_API_BASE, retries=3):
        served = full(path, base, retries)
        if path != BES3_BIKES:
            return served
        if isinstance(answer, Exception):
            raise answer
        return answer

    return _get


@pytest.mark.parametrize("status", FAILED_RESULTS)
def test_a_failed_bike_list_is_each_types_own_unsettled_failure(
    token_as, routed_db, refuse_every_request, status
):
    token_as("euda-test")
    expected, row_status = FAILED_RESULTS[status]
    requested = refuse_every_request(status)

    results = sync_tools.run_sync(sync_tools._ALL_TYPES)

    assert results == dict.fromkeys(sync_tools._ALL_TYPES, expected)
    assert _sync_rows(routed_db) == [
        (dtype, row_status, expected["message"]) for dtype in sync_tools._ALL_TYPES
    ]
    for dtype in sync_tools._ALL_TYPES:
        assert _last_sync_time(routed_db, dtype) is None
    # api.get retries a 401, reading the token file again.
    assert len(requested) == len(sync_tools._ALL_TYPES) * (3 if status == 401 else 1)


def test_a_bike_list_that_will_not_parse_is_each_types_own_unsettled_failure(
    token_as, routed_db, monkeypatch
):
    token_as("euda-test")
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b"<html>not json</html>"
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: response)

    results = sync_tools.run_sync(sync_tools._ALL_TYPES)

    expected = _error_result("unexpected JSONDecodeError")
    assert results == dict.fromkeys(sync_tools._ALL_TYPES, expected)
    for dtype in sync_tools._ALL_TYPES:
        assert _last_sync_time(routed_db, dtype) is None


def test_a_failed_bike_list_does_not_stop_components_the_registrations_answer(token_as, routed_db):
    """Registrations carry their own bike ids."""
    token_as("euda-test")
    record = []
    mock = _bike_list_answers(
        record, sync_tools.api.BoschRateLimitError("Rate limited"), empty_bikes=False
    )
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=mock):
        results = sync_tools.run_sync(["components"])

    assert results == {"components": {"status": "ok", "records": 2}}
    conn = db.get_db(routed_db)
    stored = db.query_components(conn, FAKE_BIKE_ID, None)
    conn.close()
    assert len(stored) == 2
    assert _bike_list_requests(record) == []


@pytest.mark.parametrize("dtype", ["components", "capacity"])
def test_a_failed_bike_list_is_the_failure_when_nothing_else_answers(token_as, routed_db, dtype):
    """Only the list can say why there is nothing, so it is asked once there is nothing."""
    token_as("euda-test")
    record = []
    mock = _bike_list_answers(record, sync_tools.api.BoschRateLimitError("Rate limited"))
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=mock):
        sync_tools.auto_sync_if_stale(dtype)
        assert _sync_rows(routed_db) == [(dtype, "error", sync_tools.RATE_LIMITED_MSG)]
        assert _last_sync_time(routed_db, dtype) is None
        assert _bike_list_requests(record) == [(BES3_BIKES, DATA_ACT_API_BASE)]

        sync_tools.auto_sync_if_stale(dtype)
        assert len(_bike_list_requests(record)) == 2


@pytest.mark.parametrize("dtype", ["components", "capacity"])
def test_a_good_bike_list_leaves_nothing_found_as_ok(token_as, routed_db, dtype):
    """The account has bikes, so this is not the trap."""
    token_as("euda-test")
    mock = _bike_list_answers([], {"bikes": [BIKE]})
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=mock):
        results = sync_tools.run_sync([dtype])

    assert results == {dtype: {"status": "ok", "records": 0}}


NOT_A_BIKE_LIST = [None, {"meta": {"count": 0}}, {"error": "nope"}, {"bikes": None}, "nope"]
NOT_A_BIKE_LIST_IDS = ["404", "meta", "error", "null-bikes", "text"]


@pytest.mark.parametrize("answer", NOT_A_BIKE_LIST, ids=NOT_A_BIKE_LIST_IDS)
def test_a_bike_list_answer_that_is_not_a_list_is_a_failure_on_a_euda_token(
    token_as, routed_db, answer
):
    """Read as the non-EU trap, it would settle and tell the user to delete their config."""
    token_as("euda-test")
    with patch(
        "bosch_flow_mcp.tools.sync_tools.api.get", side_effect=_bike_list_answers([], answer)
    ):
        results = sync_tools.run_sync(sync_tools._ALL_TYPES)

    assert results == dict.fromkeys(sync_tools._ALL_TYPES, _error_result("BoschAPIError"))
    for dtype in sync_tools._ALL_TYPES:
        assert _last_sync_time(routed_db, dtype) is None


def test_a_real_404_from_the_bike_list_is_a_failure_on_a_euda_token(
    token_as, routed_db, refuse_every_request
):
    token_as("euda-test")
    refuse_every_request(404)

    results = sync_tools.run_sync(["bikes", "service"])

    expected = _error_result("BoschAPIError")
    assert results == {"bikes": expected, "service": expected}


@pytest.mark.parametrize("answer", NOT_A_BIKE_LIST, ids=NOT_A_BIKE_LIST_IDS)
@pytest.mark.parametrize("dtype", ["bikes", "batteries", "components"])
def test_a_standard_sign_in_still_reads_those_answers_as_no_bikes(
    token_as, routed_db, dtype, answer
):
    token_as("one-bike-app")

    def get(path, base=MOBILE_API_BASE, retries=3):
        return answer if path == MOBILE_BIKE_PROFILE_LIST else {}

    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=get):
        results = sync_tools.run_sync([dtype])

    assert results == {dtype: {"status": "ok", "records": 0}}


@pytest.mark.parametrize(
    "answer",
    [{}, [], {"bikes": []}, {"data": []}, {"data": {"attributes": {"bikes": []}}}],
    ids=["empty-body", "list", "bikes", "data", "wrapped"],
)
def test_a_bike_list_with_no_bikes_is_the_trap_however_it_is_shaped(token_as, routed_db, answer):
    token_as("euda-test")
    with patch(
        "bosch_flow_mcp.tools.sync_tools.api.get", side_effect=_bike_list_answers([], answer)
    ):
        results = sync_tools.run_sync(sync_tools._ALL_TYPES)

    assert results == dict.fromkeys(sync_tools._ALL_TYPES, EUDA_EMPTY_RESULT)


@pytest.mark.parametrize("client", ["euda-test", "one-bike-app"])
@pytest.mark.parametrize(
    "answer",
    [
        [BIKE],
        {"bikes": [BIKE]},
        {"data": [BIKE]},
        {"data": {"attributes": {"bikes": [BIKE]}}},
        {"data": {"bikes": [BIKE]}},
        {"bikes": [], "data": [BIKE]},
        BIKE,
    ],
    ids=["list", "bikes", "data", "wrapped", "half-wrapped", "empty-bikes-then-data", "single"],
)
def test_every_bike_list_shape_stores_its_bike(token_as, routed_db, client, answer):
    token_as(client)
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", return_value=answer):
        results = sync_tools.run_sync(["bikes"])

    assert results == {"bikes": {"status": "ok", "records": 1}}
    conn = db.get_db(routed_db)
    stored = [bike["bike_id"] for bike in db.query_bikes(conn)]
    conn.close()
    assert stored == [FAKE_BIKE_ID]


@pytest.mark.parametrize("client", ["euda-test", "one-bike-app"])
@pytest.mark.parametrize(
    "answer",
    [{"bikes": "nope"}, {"data": "nope"}, {"data": {"attributes": "nope"}}],
    ids=["bikes", "data", "attributes"],
)
def test_a_bikes_value_that_cannot_be_read_stays_an_error(token_as, routed_db, client, answer):
    token_as(client)
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", return_value=answer):
        results = sync_tools.run_sync(["bikes"])

    assert results == {"bikes": _error_result("unexpected AttributeError")}


@pytest.mark.parametrize("dtype", ["batteries", "service", "software_updates"])
def test_a_read_alone_retries_after_a_failed_bike_list(
    token_as, routed_db, refuse_every_request, dtype
):
    """Nothing else fetches the list on this route, and a settled row would last the UTC day."""
    token_as("euda-test")
    requested = refuse_every_request(429)

    sync_tools.auto_sync_if_stale(dtype)

    assert _sync_rows(routed_db) == [(dtype, "error", sync_tools.RATE_LIMITED_MSG)]
    assert _last_sync_time(routed_db, dtype) is None
    assert requested == [f"{DATA_ACT_API_BASE}{BES3_BIKES}"]

    sync_tools.auto_sync_if_stale(dtype)
    assert len(requested) == 2

    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=make_api_mock([])):
        sync_tools.auto_sync_if_stale(dtype)
    assert _sync_rows(routed_db)[-1] == (dtype, "ok", "")
    assert _last_sync_time(routed_db, dtype) is not None


def test_an_empty_bike_list_is_euda_empty_for_every_type(token_as, routed_db):
    token_as("euda-test")
    mock = make_api_mock([], empty_bikes=True)
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=mock):
        results = sync_tools.run_sync(sync_tools._ALL_TYPES)

    assert results == dict.fromkeys(sync_tools._ALL_TYPES, EUDA_EMPTY_RESULT)
    assert _sync_rows(routed_db) == [
        (dtype, "empty", sync_tools.EUDA_EMPTY_MSG) for dtype in sync_tools._ALL_TYPES
    ]


@pytest.mark.parametrize("dtype", NOT_BIKES)
def test_a_read_alone_settles_on_an_empty_bike_list(token_as, routed_db, dtype):
    """A 200 with no bikes will say the same for the rest of the day."""
    token_as("euda-test")
    record = []
    mock = make_api_mock(record, empty_bikes=True)
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=mock):
        sync_tools.auto_sync_if_stale(dtype)
        assert _sync_rows(routed_db) == [(dtype, "empty", sync_tools.EUDA_EMPTY_MSG)]
        assert _bike_list_requests(record) == [(BES3_BIKES, DATA_ACT_API_BASE)]
        requests_made = len(record)
        sync_tools.auto_sync_if_stale(dtype)
    assert len(record) == requests_made


def test_a_type_that_stored_records_is_not_euda_empty(token_as, routed_db):
    """Registrations carry their own bike ids, so they can answer while the bike list is empty."""
    token_as("euda-test")
    record = []
    mock = _bike_list_answers(record, {"bikes": []}, empty_bikes=False)
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=mock):
        results = sync_tools.run_sync(["components"])

    assert results["components"] == {"status": "ok", "records": 2}
    assert _bike_list_requests(record) == []


@pytest.mark.parametrize("dtype", ["service", "software_updates"])
def test_a_type_alone_reads_the_bikes_the_list_just_returned(token_as, routed_db, dtype):
    token_as("euda-test")
    record = []
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=make_api_mock(record)):
        results = sync_tools.run_sync([dtype])

    assert results[dtype] == {"status": "ok", "records": 0}
    assert [path for path, _ in record if f"bikeId={FAKE_BIKE_ID}" in path]


@pytest.mark.parametrize("dtype", NOT_BIKES)
def test_a_cached_bike_means_no_bike_list_request(token_as, routed_db, dtype):
    token_as("euda-test")
    _seed_a_bike(routed_db)
    record = []
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=make_api_mock(record)):
        results = sync_tools.run_sync([dtype])

    assert results[dtype]["status"] == "ok"
    assert _bike_list_requests(record) == []


@pytest.mark.parametrize("types", [["bikes"], ["bikes", "service"]], ids=["alone", "then-service"])
def test_the_bikes_type_fetches_the_list_once(token_as, routed_db, types):
    token_as("euda-test")
    record = []
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=make_api_mock(record)):
        sync_tools.run_sync(types)
    assert _bike_list_requests(record) == [(BES3_BIKES, DATA_ACT_API_BASE)]


@pytest.mark.parametrize("dtype", ["batteries", "components"])
def test_a_standard_sign_in_fetches_the_bike_list_once_and_stays_ok(token_as, routed_db, dtype):
    """Only a euda token has the non-EU trap to report."""
    token_as("one-bike-app")
    record = []
    mock = make_api_mock(record, empty_bikes=True)
    with patch("bosch_flow_mcp.tools.sync_tools.api.get", side_effect=mock):
        results = sync_tools.run_sync([dtype])

    assert results[dtype] == {"status": "ok", "records": 0}
    assert _bike_list_requests(record) == [(MOBILE_BIKE_PROFILE_LIST, MOBILE_API_BASE)]


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
