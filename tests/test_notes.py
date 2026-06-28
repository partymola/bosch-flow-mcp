"""Tests for empty-result note injection - explaining WHY a get-tool returned 0."""

import json
from unittest.mock import patch

import pytest

from bosch_flow_mcp import db
from bosch_flow_mcp.helpers import empty_data_note
from tests.conftest import FAKE_BIKE_ID


@pytest.fixture
def note_db(tmp_path, monkeypatch):
    """An empty DB (no rows) wired into the get-tools, ready for sync_log rows."""
    db_path = tmp_path / "notes.db"
    monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(db_path))
    conn = db.get_db(db_path)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _bypass_auth_and_autosync(tmp_path, monkeypatch):
    tokens = tmp_path / "tok.json"
    tokens.write_text(json.dumps({"access_token": "x", "expiry": 9999999999}))
    monkeypatch.setattr("bosch_flow_mcp.helpers.BOSCH_TOKENS_PATH", tokens)
    patches = [
        patch(f"bosch_flow_mcp.tools.{mod}.auto_sync_if_stale")
        for mod in ("bike_tools", "battery_tools", "component_tools", "service_tools")
    ]
    for p in patches:
        p.start()
    yield
    for p in patches:
        p.stop()


# --- Unit: empty_data_note ---


def test_empty_data_note_unavailable(note_db):
    db.log_sync(note_db, "service", "unavailable", 0, "needs euda")
    assert empty_data_note(note_db, "service") == {
        "data_status": "unavailable",
        "note": "needs euda",
    }


def test_empty_data_note_ok_returns_empty(note_db):
    db.log_sync(note_db, "service", "ok", 0)
    assert empty_data_note(note_db, "service") == {}


def test_empty_data_note_no_sync_returns_empty(note_db):
    assert empty_data_note(note_db, "service") == {}


def test_empty_data_note_fallback_to_bikes(note_db):
    db.log_sync(note_db, "bikes", "empty", 0, "non-EU; switch client")
    note = empty_data_note(note_db, "batteries", fallback_type="bikes")
    assert note["data_status"] == "empty"
    assert "switch client" in note["note"]


# --- Integration: the three-way per get-tool ---


async def test_service_records_note_when_unavailable(note_db):
    db.log_sync(note_db, "service", "unavailable", 0, "needs euda client")
    from bosch_flow_mcp.tools.service_tools import bosch_get_service_records

    data = json.loads(await bosch_get_service_records())
    assert data["count"] == 0
    assert data["data_status"] == "unavailable"
    assert "euda" in data["note"]


async def test_service_records_no_note_when_ok(note_db):
    db.log_sync(note_db, "service", "ok", 0)
    from bosch_flow_mcp.tools.service_tools import bosch_get_service_records

    data = json.loads(await bosch_get_service_records())
    assert data["count"] == 0
    assert "note" not in data


async def test_bikes_note_when_euda_empty(note_db):
    db.log_sync(note_db, "bikes", "empty", 0, "remove config/bosch_config.json and re-auth")
    from bosch_flow_mcp.tools.bike_tools import bosch_get_bikes

    data = json.loads(await bosch_get_bikes())
    assert data["count"] == 0
    assert data["data_status"] == "empty"
    assert "bosch_config.json" in data["note"]


async def test_batteries_note_inherits_bikes_euda_empty(note_db):
    """Empty batteries inherit the bikes euda-empty diagnostic via the fallback."""
    db.log_sync(note_db, "bikes", "empty", 0, "non-EU; switch to one-bike-app")
    from bosch_flow_mcp.tools.battery_tools import bosch_get_batteries

    data = json.loads(await bosch_get_batteries())
    assert data["count"] == 0
    assert data["data_status"] == "empty"


async def test_software_updates_note_when_unavailable(note_db):
    db.log_sync(note_db, "software_updates", "unavailable", 0, "needs euda client")
    from bosch_flow_mcp.tools.service_tools import bosch_get_software_updates

    data = json.loads(await bosch_get_software_updates())
    assert data["count"] == 0
    assert data["data_status"] == "unavailable"


async def test_capacity_note_when_unavailable(note_db):
    db.log_sync(note_db, "capacity", "unavailable", 0, "needs euda client")
    from bosch_flow_mcp.tools.battery_tools import bosch_get_capacity

    data = json.loads(await bosch_get_capacity())
    assert data["count"] == 0
    assert data["data_status"] == "unavailable"


async def test_components_no_note_when_populated(note_db):
    """A populated result must not carry a note even if an old sync row exists."""
    db.log_sync(note_db, "components", "unavailable", 0, "stale")
    db.save_component(note_db, FAKE_BIKE_ID, "driveUnit", {"partNumber": "P", "serialNumber": "S"})
    note_db.commit()
    from bosch_flow_mcp.tools.component_tools import bosch_get_components

    data = json.loads(await bosch_get_components())
    assert data["count"] == 1
    assert "note" not in data
