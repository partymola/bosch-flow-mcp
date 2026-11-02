"""What the suite's own isolation guarantees.

No input to the server exercises this: it is a claim about a fixture in
`conftest.py`, and every other test still passes if that fixture is deleted.
"""

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from bosch_flow_mcp import api, auth

# A port nothing listens on, so a dropped refusal fails these with no egress.
NOTHING_LISTENS = "http://127.0.0.1:1"


def test_the_suite_cannot_reach_the_network():
    with pytest.raises(pytest.fail.Exception, match="reached the network"):
        urllib.request.urlopen(NOTHING_LISTENS)


def test_the_refusal_crosses_the_catch_all_around_a_refresh(tmp_path, monkeypatch):
    """`refresh_token` ends in `except Exception`, which reports an
    `AssertionError` as an ordinary network failure. `Failed` derives from
    `BaseException`."""
    tokens = tmp_path / "bosch_tokens.json"
    tokens.write_text(json.dumps({"access_token": "a", "refresh_token": "r", "expiry": 0}))
    monkeypatch.setattr(auth, "BOSCH_TOKENS_PATH", tokens)
    monkeypatch.setattr(auth, "BOSCH_TOKEN_URL", NOTHING_LISTENS)
    with pytest.raises(pytest.fail.Exception, match="reached the network"):
        auth.refresh_token()


def test_a_data_request_is_refused_too(monkeypatch):
    monkeypatch.setattr(api, "refresh_token", lambda: "fictional")
    with pytest.raises(pytest.fail.Exception, match="reached the network"):
        api.get("/v1/bike-profile", base=NOTHING_LISTENS)


def test_no_fake_token_is_left_behind_by_another_module():
    """Run alone this proves little, so the suite's order is what it relies on:
    `tests/test_api.py` runs first and replaces this name for its own tests."""
    assert api.refresh_token is auth.refresh_token


def test_the_default_database_is_the_tests_own(tmp_path):
    """Read from the environment, not by opening it: without the fixture,
    `db.get_db()` would open the real database."""
    assert Path(os.environ["BOSCH_FLOW_MCP_DB_PATH"]).parent == tmp_path


def test_the_default_database_wins_over_one_set_in_the_shell(tmp_path):
    """A developer's shell can name the real database, and a fixture that only
    filled in a missing value would leave every test using it."""
    real = tmp_path / "real.db"
    run = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            f"{__file__}::test_the_default_database_is_the_tests_own",
        ],
        cwd=Path(__file__).parents[1],
        env={**os.environ, "BOSCH_FLOW_MCP_DB_PATH": str(real)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stdout
    assert not real.exists()


def test_the_config_turns_an_unhandled_thread_exception_into_an_error(request):
    """A thread keeps `Failed` to itself, and pytest reports that only as a
    warning, so the refusal needs this filter to fail anything. This reads the
    setting; it does not drive a thread."""
    filters = request.config.getini("filterwarnings")
    assert "error::pytest.PytestUnhandledThreadExceptionWarning" in filters
