"""Shared utilities for the Bosch Flow MCP server."""

import calendar
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
        return json.dumps(result, default=str)
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


# The start a tool covering all records passes. No end date precedes it, so an
# end date alone is never refused over a start the caller did not send.
EARLIEST_DATE = date.min.isoformat()

_RELATIVE_RE = re.compile(r"(\d+)d")


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
        None          -> default_days before the end date or today, whichever is
                         earlier (start), or today (end)

    A start after the end is refused, because the empty answer reads as no data.
    """
    # Read once, or `0d` across midnight lands after the end date it defaults to.
    today = date.today()
    end_date = _parse_single_date(end_str, today, today, is_end=True)
    # Count from the earlier of the two. From the end alone the span moves forward
    # with the end, and misses today once the end is more than default_days ahead.
    # By ordinal, because the date arithmetic raises past year 1.
    default_start = date.fromordinal(max(min(end_date, today).toordinal() - default_days, 1))
    start_date = _parse_single_date(start_str, default_start, today, is_end=False)
    if start_date > end_date:
        end_name = f"end date {end_date}" if end_str is not None else f"today ({end_date})"
        raise InvalidDateError(
            f"Start date {start_date} is after {end_name}. Use a start on or before that date."
        )
    return start_date, end_date


def _parse_single_date(date_str: str | None, default: date, today: date, is_end: bool) -> date:
    if date_str is None:
        return default

    # The regexes match the shape; the stdlib rejects the value. A month of 13,
    # a 30th of February or a count of days reaching past year 1 passes the
    # pattern and raises a bare ValueError or OverflowError. Left bare it would
    # reach the caller as "Unexpected error".
    try:
        m = _RELATIVE_RE.fullmatch(date_str)
        if m:
            return today - timedelta(days=int(m.group(1)))

        if re.fullmatch(r"\d{4}-\d{2}", date_str):
            year, month = int(date_str[:4]), int(date_str[5:7])
            return date(year, month, calendar.monthrange(year, month)[1] if is_end else 1)

        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
            return date.fromisoformat(date_str)
    except (ValueError, OverflowError) as e:
        raise InvalidDateError(
            f"Invalid date '{date_str}'. Use YYYY-MM-DD, YYYY-MM, or Nd (e.g. '30d')."
        ) from e

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

    # Named marker rather than functools.wraps' __wrapped__, so the coverage
    # test pins that THIS decorator is applied, not merely that something is.
    wrapper.__requires_auth__ = True
    return wrapper
