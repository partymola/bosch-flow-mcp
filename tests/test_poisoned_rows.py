"""A row whose stored JSON cannot be parsed must not stop a read or a sync."""

from unittest.mock import patch

import pytest

from bosch_flow_mcp import db
from bosch_flow_mcp.tools import sync_tools as st
from tests.conftest import FAKE_BIKE_ID, FAKE_BIKE_ID_2
from tests.test_token_replies import DEEP_BODY

_POISONS = pytest.mark.parametrize(
    "poison",
    [DEEP_BODY.decode(), b"\xe9\xff not utf-8", "{not json"],
    ids=["too-deep", "bad-utf-8", "not-json"],
)


def _bikes_with_one_poisoned(conn, poison):
    db.save_bike(conn, FAKE_BIKE_ID, {"name": "Good", "raw": {"kept": True}})
    conn.execute(
        "INSERT INTO bikes (bike_id, name, raw_json, updated_at) VALUES (?, ?, ?, ?)",
        (FAKE_BIKE_ID_2, "Poisoned", poison, "2026-03-01T08:00:00+00:00"),
    )
    conn.commit()


@_POISONS
def test_a_read_returns_every_row_and_leaves_the_poisoned_value_as_stored(tmp_db, poison):
    _bikes_with_one_poisoned(tmp_db, poison)
    rows = {row["bike_id"]: row["raw_json"] for row in db.query_bikes(tmp_db)}
    assert rows == {FAKE_BIKE_ID: {"kept": True}, FAKE_BIKE_ID_2: poison}


@_POISONS
def test_a_poisoned_reachable_range_does_not_stop_a_read(tmp_db, poison):
    tmp_db.execute(
        "INSERT INTO soc_snapshots (bike_id, reachable_range_json, captured_at) VALUES (?, ?, ?)",
        (FAKE_BIKE_ID, poison, "2026-03-01T08:00:00+00:00"),
    )
    tmp_db.commit()
    rows = db.query_soc_snapshots(tmp_db, FAKE_BIKE_ID, "2026-03-01", "2026-03-01")
    assert [row["reachable_range_json"] for row in rows] == [poison]


@_POISONS
def test_a_sync_reading_a_poisoned_row_still_runs(poison, tmp_path, monkeypatch):
    path = tmp_path / "poisoned.db"
    conn = db.get_db(path)
    _bikes_with_one_poisoned(conn, poison)
    conn.close()
    monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(path))
    profile = {"batteries": [{"partNumber": "P", "serialNumber": "S"}]}
    with (
        patch.object(st.api, "get", return_value=profile),
        patch.object(st.auth, "token_is_euda", return_value=False),
    ):
        results = st.run_sync(["batteries"])
    assert results["batteries"] == {"status": "ok", "records": 2}
