"""Tests for shared helpers."""

import asyncio
import json
from datetime import date, timedelta

import pytest

from bosch_flow_mcp.helpers import EARLIEST_DATE, InvalidDateError, format_response, parse_date


def test_format_response_dict():
    result = format_response({"key": "value"})
    assert json.loads(result) == {"key": "value"}


def test_format_response_list():
    result = format_response([1, 2, 3])
    assert json.loads(result) == [1, 2, 3]


def test_format_response_none():
    result = format_response(None)
    assert json.loads(result) is None


def test_format_response_scalar():
    result = format_response("hello")
    assert json.loads(result) == {"result": "hello"}


def test_parse_date_defaults():
    start, end = parse_date(None, None, default_days=30)
    today = date.today()
    assert end == today
    assert start == today - timedelta(days=30)


def test_parse_date_counts_a_missing_start_back_from_the_end_date():
    assert parse_date(None, "2026-03-31", default_days=30) == (date(2026, 3, 1), date(2026, 3, 31))


def test_parse_date_counts_a_missing_start_back_from_today_when_the_end_is_later(monkeypatch):
    """From the end alone, the span moves forward with the end, so even an end
    one day ahead covers fewer of the days before today."""

    class Today(date):
        @classmethod
        def today(cls):
            return date(2026, 3, 20)

    monkeypatch.setattr("bosch_flow_mcp.helpers.date", Today)
    assert parse_date(None, "2026-04-30", default_days=30) == (date(2026, 2, 18), date(2026, 4, 30))
    assert parse_date(None, "2026-03-21", default_days=30) == (date(2026, 2, 18), date(2026, 3, 21))
    assert parse_date(None, "2026-03-20", default_days=30) == (date(2026, 2, 18), date(2026, 3, 20))


def test_parse_date_counting_back_stops_at_the_first_date_there_is():
    """`date` raises OverflowError past year 1, which reaches the caller with no
    guidance, for an end date the tools accept."""
    assert parse_date(None, "0001-01-15", default_days=30) == (date.min, date(1, 1, 15))
    assert parse_date("0001-01-02", "0001-01-15") == (date(1, 1, 2), date(1, 1, 15))


def test_parse_date_iso_format():
    start, end = parse_date("2026-03-01", "2026-03-31")
    assert start == date(2026, 3, 1)
    assert end == date(2026, 3, 31)


def test_parse_date_month_format():
    start, end = parse_date("2026-03", "2026-03")
    assert start == date(2026, 3, 1)
    assert end == date(2026, 3, 31)


def test_parse_date_relative():
    start, end = parse_date("7d", None)
    today = date.today()
    assert start == today - timedelta(days=7)
    assert end == today


def test_parse_date_invalid_raises():
    with pytest.raises(ValueError, match="Invalid date"):
        parse_date("not-a-date", None)


def test_parse_date_december_month_end():
    start, end = parse_date("2026-12", "2026-12")
    assert end == date(2026, 12, 31)


class TestAnInvertedRange:
    """An empty answer reads as no data, when the dates were out of order."""

    @pytest.mark.parametrize(
        "start,end,named_start,named_end",
        [
            ("2026-03-31", "2026-03-01", "2026-03-31", "2026-03-01"),
            ("2026-04", "2026-03", "2026-04-01", "2026-03-31"),
            ("2026-03-02", "2026-03-01", "2026-03-02", "2026-03-01"),
        ],
    )
    def test_it_is_refused_naming_both_dates(self, start, end, named_start, named_end):
        with pytest.raises(InvalidDateError) as exc_info:
            parse_date(start, end)
        assert str(exc_info.value) == (
            f"Start date {named_start} is after end date {named_end}. "
            "Use a start on or before that date."
        )

    def test_a_day_count_is_named_as_the_date_it_comes_to(self):
        with pytest.raises(InvalidDateError) as exc_info:
            parse_date("30d", "2020-01")
        counted = (date.today() - timedelta(days=30)).isoformat()
        assert str(exc_info.value) == (
            f"Start date {counted} is after end date 2020-01-31. "
            "Use a start on or before that date."
        )

    def test_a_start_after_today_with_no_end_names_today_as_what_it_is(self):
        with pytest.raises(InvalidDateError) as exc_info:
            parse_date("2999-01-01")
        assert str(exc_info.value) == (
            f"Start date 2999-01-01 is after today ({date.today().isoformat()}). "
            "Use a start on or before that date."
        )

    @pytest.mark.parametrize(
        "start,end,day",
        [
            ("2026-03-15", "2026-03-15", date(2026, 3, 15)),
            ("2026-03", "2026-03-01", date(2026, 3, 1)),
            ("2026-03-31", "2026-03", date(2026, 3, 31)),
            ("0d", None, date(2026, 3, 20)),
        ],
    )
    def test_a_range_of_one_day_is_not_inverted(self, start, end, day, monkeypatch):
        # A fixed today, so a run crossing midnight cannot move the answer.
        class Today(date):
            @classmethod
            def today(cls):
                return date(2026, 3, 20)

        monkeypatch.setattr("bosch_flow_mcp.helpers.date", Today)
        assert parse_date(start, end) == (day, day)

    @pytest.mark.parametrize(
        "start,end,expected",
        [
            ("0d", None, (date(2026, 3, 1), date(2026, 3, 1))),
            ("0d", "0d", (date(2026, 3, 1), date(2026, 3, 1))),
            (None, "2026-03-02", (date(2026, 1, 30), date(2026, 3, 2))),
        ],
    )
    def test_a_range_of_one_day_is_not_inverted_across_midnight(
        self, start, end, expected, monkeypatch
    ):
        """Read twice, `0d` lands after the end date it defaults to, and a missing
        start compares the end with another day. A second read raises here."""
        days = iter([date(2026, 3, 1)])

        class Midnight(date):
            @classmethod
            def today(cls):
                return next(days)

        monkeypatch.setattr("bosch_flow_mcp.helpers.date", Midnight)
        assert parse_date(start, end, default_days=30) == expected

    def test_a_refusal_names_the_today_it_read(self, monkeypatch):
        days = iter([date(2026, 3, 1)])

        class Midnight(date):
            @classmethod
            def today(cls):
                return next(days)

        monkeypatch.setattr("bosch_flow_mcp.helpers.date", Midnight)
        with pytest.raises(InvalidDateError) as exc_info:
            parse_date("2026-03-02")
        assert str(exc_info.value) == (
            "Start date 2026-03-02 is after today (2026-03-01). Use a start on or before that date."
        )

    def test_a_missing_start_is_never_inverted(self):
        assert parse_date(None, "0001-01-01") == (date.min, date.min)

    @pytest.mark.parametrize("end", ["0001-01-01", "0001-01", "1999-12", "2026-03-01", "0d"])
    def test_the_start_meaning_all_records_precedes_every_end(self, end):
        assert parse_date(EARLIEST_DATE, end)[0] == date.min


class TestTheAuthGate:
    """Every tool is wrapped in require_auth, and its two halves are pinned here.

    The gate must refuse when there are no credentials, and must turn an
    exception into a tool result rather than letting it escape. The cached tools
    catch nothing themselves, and the live reads do not catch a rate limit.
    """

    def _tool(self, monkeypatch, tokens_exist, body):
        import json as _json

        from bosch_flow_mcp import helpers as helpers_module

        class _Path:
            def exists(self):
                return tokens_exist

        monkeypatch.setattr(helpers_module, "BOSCH_TOKENS_PATH", _Path())

        @helpers_module.require_auth
        async def tool():
            return body()

        return _json.loads(asyncio.run(tool()))

    def test_it_refuses_when_there_are_no_credentials(self, monkeypatch):
        called = []
        result = self._tool(monkeypatch, False, lambda: called.append(1) or "{}")
        assert "Run: bosch-flow-mcp auth" in result["error"]
        assert called == []

    def test_a_rate_limit_becomes_a_tool_result(self, monkeypatch):
        from bosch_flow_mcp import api

        def boom():
            raise api.BoschRateLimitError("Rate limited on /v1/bike-profile")

        result = self._tool(monkeypatch, True, boom)
        assert "rate limiting" in result["error"]
        assert "/v1/bike-profile" not in result["error"]

    def test_an_api_failure_becomes_a_tool_result_without_its_path(self, monkeypatch):
        from bosch_flow_mcp import api

        def boom():
            raise api.BoschForbiddenError("Forbidden (403) for /x?serialNumber=SN-SECRET")

        result = self._tool(monkeypatch, True, boom)
        assert "SN-SECRET" not in result["error"]
        assert "serialNumber" not in result["error"]

    def test_an_unanticipated_failure_becomes_a_tool_result(self, monkeypatch):
        def boom():
            raise KeyError("/etc/secret/path")

        result = self._tool(monkeypatch, True, boom)
        assert "KeyError" in result["error"]
        assert "/etc/secret/path" not in result["error"]

    def test_a_healthy_tool_is_untouched(self, monkeypatch):
        result = self._tool(monkeypatch, True, lambda: '{"ok": true}')
        assert result == {"ok": True}

    def test_a_bad_date_keeps_its_message_so_the_model_can_retry(self, monkeypatch):
        """The catch-all must not swallow the server's own guidance.

        A bare ValueError would not do as the carrier: that is also what a
        JSON decode raises, and those carry response content.
        """
        from bosch_flow_mcp.helpers import parse_date

        def boom():
            parse_date("last week")

        result = self._tool(monkeypatch, True, boom)
        assert "YYYY-MM-DD" in result["error"]
        assert "last week" in result["error"]


def test_every_tool_is_gated():
    """`require_auth` on every tool is the first line of the gate's own claim."""
    import importlib
    import pkgutil

    import bosch_flow_mcp.tools as tools_pkg

    ungated = []
    for module_info in pkgutil.iter_modules(tools_pkg.__path__):
        module = importlib.import_module(f"bosch_flow_mcp.tools.{module_info.name}")
        for name in dir(module):
            obj = getattr(module, name)
            if not (callable(obj) and name.startswith("bosch_")):
                continue
            # The marker require_auth sets, not functools.wraps' __wrapped__:
            # any decorator sets that, so a refactor adding a timing wrapper
            # while dropping the gate would go unnoticed.
            if not getattr(obj, "__requires_auth__", False):
                ungated.append(f"{module_info.name}.{name}")

    assert ungated == [], f"tools not wrapped in require_auth: {ungated}"


class TestDateValuesThatPassTheShapeCheck:
    """A month of 13 passes the regex and fails in the constructor.

    That is a likelier model slip than a word. Left as a bare ValueError it
    would reach the caller as "Unexpected error (ValueError)", with no guidance
    to retry on.
    """

    @pytest.mark.parametrize("position", ["start", "end"])
    @pytest.mark.parametrize(
        "bad",
        [
            "2026-13-01",
            "2026-02-30",
            "2026-13",
            "2026-00",
            "2026-00-01",
            "0000-01",
            "0000-12",
            "99999999d",
            "9999999999d",
            pytest.param("9" * 5000 + "d", id="more-digits-than-int-reads"),
            "last week",
            pytest.param("2026-03-01\n", id="day-with-a-newline"),
            pytest.param("2026-03\n", id="month-with-a-newline"),
            pytest.param("30d\n", id="count-with-a-newline"),
        ],
    )
    def test_it_keeps_the_guidance(self, bad, position):
        from bosch_flow_mcp.helpers import InvalidDateError, parse_date

        with pytest.raises(InvalidDateError) as exc_info:
            if position == "start":
                parse_date(bad)
            else:
                parse_date(None, bad)
        assert "YYYY-MM-DD" in str(exc_info.value)
        assert bad in str(exc_info.value)

    @pytest.mark.parametrize(
        "month,last_day",
        [
            ("2026-01", 31),
            ("2026-02", 28),
            ("2024-02", 29),
            ("2026-04", 30),
            ("2026-11", 30),
            ("2026-12", 31),
            ("9999-12", 31),
        ],
    )
    def test_a_month_runs_from_its_first_day_to_its_last(self, month, last_day):
        year, number = int(month[:4]), int(month[5:])
        assert parse_date(month, month) == (date(year, number, 1), date(year, number, last_day))
