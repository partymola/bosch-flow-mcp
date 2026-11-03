"""Answer shapes at the edges of what the sync parsers read.

Every shape is invented: no real Data Act payload has been seen.
"""

import threading
import time
from unittest.mock import patch

import pytest

from bosch_flow_mcp import db
from bosch_flow_mcp.tools import sync_tools
from tests.test_routing import (
    BIKE,
    _bike_list_answers,
    _error_result,
    _last_sync_time,
    _sync_capacity_twice,
)


@pytest.fixture
def routed_db(tmp_path, monkeypatch):
    db_path = tmp_path / "shapes.db"
    monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(db_path))
    return db_path


@pytest.mark.parametrize(
    "answer",
    [
        {"data": {"attributes": []}},
        {"data": {"attributes": [BIKE]}},
        {"data": {"attributes": None}},
        {"data": {"attributes": "x"}},
    ],
    ids=["empty-list", "list", "null", "text"],
)
def test_attributes_that_are_not_an_object_are_no_type_s_non_eu_trap(token_as, routed_db, answer):
    token_as("euda-test")
    with patch(
        "bosch_flow_mcp.tools.sync_tools.api.get", side_effect=_bike_list_answers([], answer)
    ):
        results = sync_tools.run_sync(sync_tools._ALL_TYPES)

    assert results == dict.fromkeys(sync_tools._ALL_TYPES, _error_result("BoschAPIError"))
    for dtype in sync_tools._ALL_TYPES:
        assert _last_sync_time(routed_db, dtype) is None


@pytest.mark.parametrize(
    "answer,stored",
    [
        ([{"testDate": 1700000000}], "1700000000"),
        ({"testDate": 1700000000, "capacity": 90}, "1700000000"),
        ([{"testDate": 1700000000.5}], "1700000000.5"),
        ([{"testDate": {"day": 10}, "date": "2026-01-01"}], "2026-01-01"),
        ([{"testDate": "", "date": "2026-01-01"}], "2026-01-01"),
        ([{"testDate": True}], None),
    ],
    ids=[
        "number-in-a-list",
        "bare-number",
        "fraction",
        "text-beside-an-object",
        "empty-text",
        "bool",
    ],
)
def test_a_test_date_is_the_first_one_that_can_be_stored(token_as, routed_db, answer, stored):
    token_as("euda-test")
    first, dates = _sync_capacity_twice(routed_db, answer)
    assert first == {"capacity": {"status": "ok", "records": 1}}
    assert dates == [stored]


def test_two_writers_store_an_undated_test_once(tmp_path):
    """The second writer's check must run after the first commits, not before.

    It can only fail to catch a regression, if the thread has not reached its
    statement within the wait; it cannot fail a correct implementation.
    """
    path = tmp_path / "race.db"
    first = db.get_db(path)
    try:
        db.save_capacity_test(first, "P1", "S1", None, {"capacity": 90})

        def write():
            second = db.get_db(path)
            try:
                db.save_capacity_test(second, "P1", "S1", None, {"capacity": 90})
                second.commit()
            finally:
                second.close()

        writer = threading.Thread(target=write)
        writer.start()
        time.sleep(0.5)
        first.commit()
        writer.join(10)
        assert not writer.is_alive()
        assert first.execute("SELECT COUNT(*) FROM capacity_tests").fetchone()[0] == 1
    finally:
        first.close()
