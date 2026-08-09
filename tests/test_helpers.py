"""Tests for shared helpers."""

import asyncio
import json
from datetime import date, timedelta

import pytest

from bosch_flow_mcp.helpers import format_response, parse_date


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


class TestTheAuthGate:
    """Every tool is wrapped in require_auth, and nothing pinned either half.

    The gate must refuse when there are no credentials, and must turn an
    exception into a tool result rather than letting it escape as a transport
    error - the four API exception types are siblings off Exception, so a live
    read catches none of them on its own.
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
