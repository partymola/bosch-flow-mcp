"""Shared utilities for the Bosch Flow MCP server."""

import functools
import json
import re
from datetime import date, timedelta
from typing import Any

from .config import BOSCH_TOKENS_PATH


# --- Response formatting ---

def format_response(result: Any) -> str:
    """JSON-serialize a result for MCP transport."""
    if isinstance(result, (dict, list)):
        return json.dumps(result, indent=2, default=str)
    elif result is None:
        return json.dumps(None)
    else:
        return json.dumps({"result": str(result)})


# --- Date parsing ---

_RELATIVE_RE = re.compile(r"^(\d+)d$")


def parse_date(
    start_str: str | None,
    end_str: str | None = None,
    default_days: int = 30,
) -> tuple[date, date]:
    """Parse flexible date inputs into a (start_date, end_date) tuple.

    Accepted formats:
        "YYYY-MM-DD"  -> exact date
        "YYYY-MM"     -> first of month (start) or last of month (end)
        "30d"         -> 30 days ago from today
        None          -> default_days ago from today (start) or today (end)
    """
    today = date.today()
    end_date = _parse_single_date(end_str, today, is_end=True)
    start_date = _parse_single_date(start_str, today - timedelta(days=default_days), is_end=False)
    return start_date, end_date


def _parse_single_date(date_str: str | None, default: date, is_end: bool) -> date:
    if date_str is None:
        return default

    m = _RELATIVE_RE.match(date_str)
    if m:
        return date.today() - timedelta(days=int(m.group(1)))

    if re.match(r"^\d{4}-\d{2}$", date_str):
        year, month = int(date_str[:4]), int(date_str[5:7])
        if is_end:
            if month == 12:
                return date(year + 1, 1, 1) - timedelta(days=1)
            return date(year, month + 1, 1) - timedelta(days=1)
        return date(year, month, 1)

    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return date.fromisoformat(date_str)

    raise ValueError(
        f"Invalid date '{date_str}'. Use YYYY-MM-DD, YYYY-MM, or Nd (e.g. '30d')."
    )


# --- Auth decorator ---

def require_auth(func):
    """Decorator that checks EUDA tokens exist before calling a tool."""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        if not BOSCH_TOKENS_PATH.exists():
            return json.dumps({
                "error": "Bosch not configured. Run: bosch-flow-mcp auth",
            })
        return await func(*args, **kwargs)
    return wrapper
