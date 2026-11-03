"""Answer shapes at the edges of what the sync parsers read.

Every shape is invented: no real Data Act payload has been seen.
"""

from unittest.mock import patch

import pytest

from bosch_flow_mcp.tools import sync_tools
from tests.test_routing import BIKE, _bike_list_answers, _error_result, _last_sync_time


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
