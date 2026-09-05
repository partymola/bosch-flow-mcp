"""A component_type the cache cannot match is refused, not answered empty.

`bosch_get_components(component_type="not-a-component")` came back as
`{"components": [], "count": 0}`, and so did `component_type="DRIVEUNIT"`.
Both read as a bike that has no such component rather than as a filter the
server could not use, and wrong case was enough to produce one.

The types are the provider's vocabulary rather than a set this server defines,
so an annotation cannot hold them and the cache is what says which exist. One
type can be held under more than one spelling, because a bike's profile keys
and its registrations describe the same part, so a caller naming a spelling
means every spelling of it.

The empty cache is its own case: with nothing stored there is no vocabulary to
judge against and nothing to name in a refusal. That answer stays thin, since
a sync that recorded `ok` with nothing to store carries no note either.
"""

import asyncio
import json
import sqlite3

import pytest

from bosch_flow_mcp.mcp_instance import mcp

from .conftest import FAKE_BIKE_ID, FAKE_BIKE_ID_2

STORED_TYPE = "driveUnit"
# Held by the second bike and not the first, so a per-bike vocabulary and an
# account-wide one answer differently. With one bike carrying everything the
# two are indistinguishable, and the difference is the whole invariant.
OTHER_BIKES_TYPE = "battery"
NOW = "2026-04-07T10:00:00+00:00"


@pytest.fixture(autouse=True)
def require_auth_bypass(monkeypatch, tmp_path):
    tokens_path = tmp_path / "tokens.json"
    tokens_path.write_text(json.dumps({"access_token": "fake", "expiry": 9999999999}))
    monkeypatch.setattr("bosch_flow_mcp.helpers.BOSCH_TOKENS_PATH", tokens_path)


@pytest.fixture(autouse=True)
def patch_auto_sync(monkeypatch):
    monkeypatch.setattr(
        "bosch_flow_mcp.tools.component_tools.auto_sync_if_stale", lambda *a, **k: None
    )


@pytest.fixture(autouse=True)
def db_file(populated_db, tmp_path, monkeypatch):
    import bosch_flow_mcp.db as db_module

    path = tmp_path / "test_component_type_filter.db"
    conn = db_module.get_db(path)
    for table in ("bikes", "components", "sync_log"):
        rows = populated_db.execute(f"SELECT * FROM {table}").fetchall()
        if not rows:
            continue
        cols = [d[0] for d in populated_db.execute(f"SELECT * FROM {table} LIMIT 0").description]
        conn.executemany(
            f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) "
            f"VALUES ({','.join(['?'] * len(cols))})",
            [tuple(r) for r in rows],
        )
    conn.commit()
    conn.close()
    add_component(path, FAKE_BIKE_ID_2, OTHER_BIKES_TYPE, "SN654321")
    monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(path))
    return path


def call(args):
    """Call the registered tool and return its parsed reply."""
    result = asyncio.run(mcp.call_tool("bosch_get_components", args))
    return json.loads("".join(c.text for c in result.content if getattr(c, "text", None)))


def add_component(path, bike_id: str, component_type: str, serial: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """INSERT OR REPLACE INTO components
        (bike_id, component_type, part_number, serial_number, product_name,
         software_version, raw_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (bike_id, component_type, "TESTPART003", serial, "Test Part", "1.0.0", "{}", NOW),
    )
    conn.commit()
    conn.close()


def test_the_fixture_holds_the_type_these_tests_name():
    """Every assertion below is vacuous against a cache without it.

    The two types must also sit on different bikes, which is what lets a
    per-bike vocabulary be told from an account-wide one.
    """
    data = call({})
    held = {c["component_type"]: c["bike_id"] for c in data["components"]}
    assert held == {STORED_TYPE: FAKE_BIKE_ID, OTHER_BIKES_TYPE: FAKE_BIKE_ID_2}


@pytest.mark.parametrize("value", ["not-a-component", "drive-unit", "battery pack", ""])
def test_an_unknown_component_type_is_refused_rather_than_answered_empty(value):
    data = call({"component_type": value})
    assert "error" in data, (
        f"component_type={value!r} was answered as a result rather than refused: {data}"
    )


def test_the_refusal_names_the_types_the_cache_holds():
    """A refusal the model cannot act on leaves it guessing at the vocabulary."""
    data = call({"component_type": "not-a-component"})
    for held in (STORED_TYPE, OTHER_BIKES_TYPE):
        assert held in data["error"], (
            f"the refusal did not name {held!r}, which would have worked: {data['error']!r}"
        )


@pytest.mark.parametrize("value", ["DRIVEUNIT", "driveunit", "DriveUnit"])
def test_a_type_differing_only_in_case_answers_the_stored_one(value):
    """The provider's spelling is camelCase, which is what a caller gets wrong."""
    data = call({"component_type": value})
    assert data.get("count") == 1, f"component_type={value!r} did not reach the stored rows: {data}"
    assert data["components"][0]["component_type"] == STORED_TYPE


def test_the_stored_spelling_still_answers():
    data = call({"component_type": STORED_TYPE})
    assert data["count"] == 1
    assert data["components"][0]["bike_id"] == FAKE_BIKE_ID


def test_a_known_type_the_named_bike_lacks_still_answers_empty():
    """The refusal is about the vocabulary, never about one bike's fitment.

    A real type that this bike has none of is a true empty answer, and turning
    it into an error would tell the model the type does not exist. The second
    bike carries a component of its own, so a vocabulary built from that bike
    alone is not empty and would refuse here.
    """
    data = call({"bike_id": FAKE_BIKE_ID_2, "component_type": STORED_TYPE})
    assert "error" not in data, data
    assert data["count"] == 0


@pytest.mark.parametrize("asked", ["battery", "Battery", "BATTERY"])
def test_every_spelling_of_one_type_answers_together(db_file, asked):
    """One type can be stored under two spellings, and both are the answer.

    A bike's profile keys and its registrations describe the same part, so
    the same battery reaches the cache as `battery` and as `Battery`.
    Resolving to whichever sorts first drops the rows under the other and
    replaces a correct answer with a confident partial one, which is worse
    than the empty list this filter was fixed to stop returning.
    """
    add_component(db_file, FAKE_BIKE_ID, "Battery", "SN111111")

    data = call({"component_type": asked})
    assert "error" not in data, data
    assert {c["component_type"] for c in data["components"]} == {"battery", "Battery"}


def test_a_type_both_bikes_carry_answers_for_both(db_file):
    """A one-type-one-bike fixture cannot tell a per-row filter from a per-bike one."""
    add_component(db_file, FAKE_BIKE_ID_2, STORED_TYPE, "SN222222")

    data = call({"component_type": STORED_TYPE})
    assert "error" not in data, data
    assert {c["bike_id"] for c in data["components"]} == {FAKE_BIKE_ID, FAKE_BIKE_ID_2}


def test_a_type_outside_ascii_answers_in_another_case(db_file):
    """The match and the query must fold the same way, which SQL cannot do.

    SQLite's `COLLATE NOCASE` folds ASCII only, so a query written that way
    over the caller's own spelling accepts `STRASSE` at the membership test
    and then returns nothing, which is the empty result this filter exists
    to stop. Resolving to the stored spelling in Python is what avoids it,
    and only a type outside ASCII can tell the two apart.
    """
    add_component(db_file, FAKE_BIKE_ID, "Straße", "SN333333")

    data = call({"component_type": "STRASSE"})
    assert "error" not in data, data
    assert [c["component_type"] for c in data["components"]] == ["Straße"]


def test_the_query_layer_reads_no_spellings_as_no_rows(db_file):
    """An empty list is a narrowed question, never an unfiltered one.

    Nothing reaches this today, since the tool refuses before the list can
    come out empty. The signature takes a sequence now, so the next caller
    to compute one is who this is for.
    """
    from bosch_flow_mcp import db

    conn = db.get_db(db_file)
    try:
        assert db.query_components(conn, None, []) == []
        assert len(db.query_components(conn, None, None)) == 2
    finally:
        conn.close()


def test_a_refusal_is_not_shaped_like_an_empty_result():
    """Carrying `components` and `count` would let a refusal be skimmed as data."""
    data = call({"component_type": "not-a-component"})
    assert "components" not in data and "count" not in data, data


def test_an_unknown_type_is_not_refused_when_no_components_are_cached(db_file):
    """With nothing stored there is no vocabulary and nothing to name.

    Refusing here would report a filter problem when the real one is that
    nothing has been synced yet. The answer is thin either way: a sync that
    recorded `ok` with nothing to store leaves `empty_data_note` silent too,
    so this reply carries no explanation of its own.
    """
    conn = sqlite3.connect(db_file)
    conn.execute("DELETE FROM components")
    conn.commit()
    conn.close()

    data = call({"component_type": "not-a-component"})
    assert "error" not in data, data
    assert data["count"] == 0
    # The thinness is asserted, so an explanation cannot be added to this
    # path while the docs above still say there is none.
    assert "note" not in data and "data_status" not in data, data
