"""An enumerated argument must be refused, never answered under the wrong label.

`bosch_battery_trends(period="not-a-period")` fell through to monthly and
echoed the value it was given, so it returned correct monthly buckets under
`"period": "not-a-period"`. Nothing was empty, nothing raised, and nothing
about the reply looked wrong, which is worse than an error: a caller cannot
tell it from a correct one.

The calls here go through `mcp.call_tool`, the only layer that applies the
argument schema. Calling the Python function directly skips it, so a test
written that way passes whether the constraint is there or not. The rest read
the docstring or the key functions, which have no other reader.
"""

import asyncio
import inspect
import json
import re
from typing import get_args
from unittest.mock import patch

import pytest

from bosch_flow_mcp.mcp_instance import mcp
from bosch_flow_mcp.tools.analysis_tools import (
    _PERIOD_KEY_FNS,
    TrendPeriod,
    bosch_battery_trends,
)


@pytest.fixture(autouse=True)
def require_auth_bypass(monkeypatch, tmp_path):
    tokens_path = tmp_path / "tokens.json"
    tokens_path.write_text(json.dumps({"access_token": "fake", "expiry": 9999999999}))
    monkeypatch.setattr("bosch_flow_mcp.helpers.BOSCH_TOKENS_PATH", tokens_path)


@pytest.fixture(autouse=True)
def patch_auto_sync():
    with patch("bosch_flow_mcp.tools.analysis_tools.auto_sync_if_stale"):
        yield


@pytest.fixture(autouse=True)
def patch_db_path(populated_db, tmp_path, monkeypatch):
    import bosch_flow_mcp.db as db_module

    db_file = tmp_path / "test_argument_validation.db"
    conn = db_module.get_db(db_file)
    for table in ("batteries", "bikes", "sync_log"):
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
    monkeypatch.setenv("BOSCH_FLOW_MCP_DB_PATH", str(db_file))
    yield


def call(args):
    """Call the registered tool, returning (raised_message, result_text)."""
    try:
        result = asyncio.run(mcp.call_tool("bosch_battery_trends", args))
    except Exception as e:
        # The message is what is under test, so every type is caught.
        return str(e), None
    return None, "".join(c.text for c in result.content if getattr(c, "text", None))


# What a bucket key looks like per period. An independent oracle: the enum
# and the dispatch can agree with each other while the call site aggregates
# by something else entirely, and every label would still come back correct.
# The shapes have to stay mutually exclusive or the oracle stops
# discriminating without failing, which is what the test below catches.
PERIOD_KEY_SHAPE = {
    "weekly": r"^\d{4}-W\d{2}$",
    "monthly": r"^\d{4}-\d{2}$",
    "quarterly": r"^\d{4}-Q[1-4]$",
}

# Two dates that must land in different buckets of the same period. A shape
# alone cannot see a key function that returns a constant within its family,
# which is how `_quarter_key` could have answered Q1 for the whole year.
PERIOD_APART = {
    "weekly": ("2026-03-02T00:00:00Z", "2026-03-30T00:00:00Z"),
    "monthly": ("2026-03-15T00:00:00Z", "2026-04-15T00:00:00Z"),
    "quarterly": ("2026-02-15T00:00:00Z", "2026-08-15T00:00:00Z"),
}


def documented_argument(argument: str) -> str:
    """The docstring chunk describing one argument.

    Runs from the argument's own line to the next one at the same indent; a
    continuation line is indented further, so it stays inside the chunk.
    """
    doc = bosch_battery_trends.__doc__
    opening = re.search(rf"^(?P<indent> *){argument}:", doc, re.MULTILINE)
    assert opening, f"bosch_battery_trends' docstring documents no {argument!r} argument"
    rest = doc[opening.end() :]
    end = re.search(rf"^{opening['indent']}\w+:", rest, re.MULTILINE)
    return rest[: end.start()] if end else rest


def documented_values(argument: str) -> set[str]:
    """The values the docstring offers for one argument.

    The schema enum and the "Options:" list are the same fact written twice.
    Scoped to what follows `Options:`, since the rest of the chunk quotes
    other things and would hide a value dropped from the list itself.
    """
    chunk = documented_argument(argument)
    options = chunk.split("Options:", 1)
    assert len(options) == 2, f"{argument} documents no Options: list"
    return set(re.findall(r'"([^"]+)"', options[1]))


@pytest.mark.parametrize("value", ["not-a-period", "daily", "MONTHLY", ""])
def test_an_unrecognised_period_is_refused(value):
    raised, text = call({"period": value})
    assert raised is not None, (
        f"bosch_battery_trends accepted period={value!r} and answered as though it were valid: "
        f"{text}"
    )


@pytest.mark.parametrize("value", ["not-a-period", "daily"])
def test_a_refusal_names_the_periods_that_would_have_worked(value):
    """A refusal the model cannot act on is barely better than a wrong label."""
    raised, _ = call({"period": value})
    # An alias that stopped being a Literal yields no values, and every
    # assertion over them would then pass vacuously.
    assert get_args(TrendPeriod), "period has no enumerated values"
    assert all(p in raised for p in get_args(TrendPeriod)), (
        f"refusing period={value!r} did not name the accepted values: {raised!r}"
    )


def test_the_schema_carries_the_accepted_periods():
    """Refusing is not enough: the model reads the schema before it calls.

    Nothing above distinguishes a schema enum from a check inside the body,
    and only the schema is visible to a caller deciding what to send.
    """
    tool = {t.name: t for t in asyncio.run(mcp.list_tools())}["bosch_battery_trends"]
    assert tool.input_schema["properties"]["period"].get("enum") == list(get_args(TrendPeriod))


def test_the_documented_periods_are_the_accepted_ones():
    assert documented_values("period") == set(get_args(TrendPeriod))


def test_the_extractor_reads_the_argument_it_was_asked_for():
    """Otherwise the assertion above could be comparing empty sets."""
    assert "weekly" in documented_values("period")
    # bike_id offers no Options: list, so it is read as a whole chunk. What
    # is being checked is the boundary: period's values must not leak into
    # the argument before it.
    assert "weekly" not in documented_argument("bike_id")


def test_the_key_shapes_tell_the_periods_apart():
    """The shapes are hand-written, so they need a pin of their own.

    Loosened to anything, they still pass over every period and the check
    they exist for goes quiet. Mutual exclusivity is the property: a key of
    one period's shape must not satisfy any other's.
    """
    assert set(PERIOD_KEY_SHAPE) == set(get_args(TrendPeriod))
    for period, key_fn in _PERIOD_KEY_FNS.items():
        key = key_fn("2026-03-15T08:00:00Z")
        assert re.match(PERIOD_KEY_SHAPE[period], key)
        for other, shape in PERIOD_KEY_SHAPE.items():
            if other != period:
                assert not re.match(shape, key), (
                    f"a {period} key {key!r} also satisfies the {other} shape"
                )


def test_each_accepted_period_buckets_differently():
    """Two entries pointing at the same key function is a silent relabelling.

    The schema still offers the value and the answer still comes back under
    it, aggregated by something else.
    """
    keys = [fn("2026-03-15T08:00:00Z") for fn in _PERIOD_KEY_FNS.values()]
    assert len(set(keys)) == len(keys), f"two periods bucket alike: {keys}"


@pytest.mark.parametrize("period", get_args(TrendPeriod))
def test_a_period_separates_dates_that_belong_to_different_buckets(period):
    """A key function that collapses its own family relabels just as quietly.

    Distinctness between periods does not see it: a quarterly key answering
    Q1 for every date still differs from the weekly and monthly keys, while
    a year of snapshots piles into one bucket labelled as a quarter.
    """
    assert set(PERIOD_APART) == set(get_args(TrendPeriod))
    earlier, later = PERIOD_APART[period]
    key_fn = _PERIOD_KEY_FNS[period]
    assert key_fn(earlier) != key_fn(later), (
        f"{period} put {earlier} and {later} in the same bucket: {key_fn(earlier)}"
    )


@pytest.mark.parametrize("period", get_args(TrendPeriod))
def test_every_accepted_period_still_answers_under_its_own_label(period):
    """The refusals must not be so eager that real calls stop working.

    The bucket keys are asserted as well as the echoed label, because the
    echo comes from the argument and says nothing about what was aggregated:
    a call site that ignored the dispatch and always bucketed by month would
    answer every period correctly labelled and wrongly grouped.
    """
    assert set(PERIOD_KEY_SHAPE) == set(get_args(TrendPeriod))
    raised, text = call({"period": period})
    assert raised is None, raised
    data = json.loads(text)
    assert data["period"] == period
    assert data["trends"], "a valid period returned no buckets"
    for trend in data["trends"]:
        assert re.match(PERIOD_KEY_SHAPE[period], trend["period"]), (
            f"{period} produced the bucket key {trend['period']!r}"
        )


def test_the_default_is_a_value_the_schema_accepts():
    """A default is never validated, and the schema advertises it regardless.

    Pydantic checks what a caller sends and not what the signature falls back
    to, so a default outside the enum is published as this tool's default
    while its own enum rejects it, and everyone who omits the argument then
    reaches a dispatch lookup that raises. Measured. The docstring's
    `(default)` marker is the same value a third time.
    """
    default = inspect.signature(bosch_battery_trends).parameters["period"].default
    assert default in get_args(TrendPeriod)
    marked = re.search(r'"([^"]+)" \(default\)', documented_argument("period"))
    assert marked, "the period docstring marks no default"
    assert marked.group(1) == default

    raised, text = call({})
    assert raised is None, raised
    data = json.loads(text)
    assert data["period"] == default
    assert data["trends"]
    for trend in data["trends"]:
        assert re.match(PERIOD_KEY_SHAPE[default], trend["period"])
