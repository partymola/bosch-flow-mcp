"""Tests for shared helpers."""

import json
import pytest
from datetime import date, timedelta

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
