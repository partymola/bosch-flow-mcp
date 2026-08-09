"""Shared utilities for the Bosch Flow MCP server."""

import functools
import json
import logging
import re
from datetime import date, timedelta
from typing import Any

from . import api, db
from .config import BOSCH_TOKENS_PATH

logger = logging.getLogger(__name__)

# --- Response formatting ---


def empty_data_note(conn, data_type: str, fallback_type: str | None = None) -> dict:
    """Explain an empty get-tool result, or {} when the data is genuinely current.

    When a get-tool returns no rows, the latest sync may have been skipped (the type
    needs the EU Data Act client), euda-empty (a non-EU account the Data Act API
    shares nothing for), or errored. Surfacing that as data_status + note stops an
    empty result from reading to the model as "you have none" when it really means
    "couldn't fetch with this sign-in". Returns {} when the last sync was 'ok'.

    fallback_type lets a downstream type (batteries/components) inherit the bikes
    diagnostic: if bikes came back euda-empty, everything derived from it is empty too.
    """
    note = db.last_sync_note(conn, data_type)
    if note is None and fallback_type:
        note = db.last_sync_note(conn, fallback_type)
    if note is None:
        return {}
    status, message = note
    return {"data_status": status, "note": message}


def format_response(result: Any) -> str:
    """JSON-serialize a result for MCP transport."""
    if isinstance(result, (dict, list)):
        return json.dumps(result, indent=2, default=str)
    elif result is None:
        return json.dumps(None)
    else:
        return json.dumps({"result": str(result)})


# --- Date parsing ---


class InvalidDateError(ValueError):
    """A tool argument that is not a date this server accepts.

    Its own text, not the API's, and it tells the model how to retry - so
    require_auth passes it through where it reports every other failure as a
    type name. A bare ValueError would not do: that is also what a JSON
    decode raises, and those carry response content.
    """


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

    raise InvalidDateError(
        f"Invalid date '{date_str}'. Use YYYY-MM-DD, YYYY-MM, or Nd (e.g. '30d')."
    )


# --- Auth decorator ---


def require_auth(func):
    """Gate every tool on the credentials existing, and on nothing escaping.

    See AGENTS.md for which failures reach here and why the messages are fixed.
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        if not BOSCH_TOKENS_PATH.exists():
            return json.dumps(
                {
                    "error": "Bosch not configured. Run: bosch-flow-mcp auth",
                }
            )
        try:
            return await func(*args, **kwargs)
        except InvalidDateError as e:
            return json.dumps({"error": str(e)})
        except api.BoschAuthError:
            return json.dumps(
                {"error": "Bosch rejected the stored credentials. Run: bosch-flow-mcp auth"}
            )
        except api.BoschRateLimitError:
            return json.dumps({"error": "Bosch is rate limiting this client. Try again shortly."})
        except api.BoschAPIError as e:
            return json.dumps(
                {"error": f"Bosch could not answer this request ({type(e).__name__})."}
            )
        except Exception as e:
            logger.error("Tool %s failed: %s", func.__name__, type(e).__name__)
            return json.dumps({"error": f"Unexpected error ({type(e).__name__})."})

    return wrapper
