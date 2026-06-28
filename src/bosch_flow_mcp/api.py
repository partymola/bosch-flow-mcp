"""Bosch Flow API client with automatic token refresh.

All requests use the same one-bike-app token. The base URL parameter selects
which API to hit:
  - MOBILE_API_BASE (default): bike profiles, SoC, activities
  - DATA_ACT_API_BASE: capacity testers, service book, registrations, etc.
"""

import json
import logging
import urllib.error
import urllib.request

from .auth import invalidate_token_cache, refresh_token
from .config import MOBILE_API_BASE

logger = logging.getLogger(__name__)


class BoschAuthError(Exception):
    """Token expired or invalid; re-auth needed."""


class BoschRateLimitError(Exception):
    """Rate limited (429)."""


class BoschAPIError(Exception):
    """General API error."""


def get(path: str, base: str = MOBILE_API_BASE, retries: int = 3) -> dict | list | None:
    """Make an authenticated GET request to a Bosch API.

    Handles:
    - Automatic token refresh before each call
    - 401: invalidate cache, refresh, retry once
    - 429: raise BoschRateLimitError
    - 404: return None (resource not found / bike offline)
    - Other errors: raise BoschAPIError

    Returns the parsed JSON response body.
    """
    for attempt in range(retries):
        try:
            token = refresh_token()
        except RuntimeError as e:
            raise BoschAuthError(str(e)) from e

        url = f"{base}{path}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read().decode()
                return json.loads(body) if body.strip() else {}

        except urllib.error.HTTPError as e:
            if e.code == 401:
                if attempt < retries - 1:
                    logger.info("Token expired (401), refreshing and retrying")
                    invalidate_token_cache()
                    continue
                raise BoschAuthError("Authentication failed after retry. Run: bosch-flow-mcp auth")

            if e.code == 403:
                logger.info("Forbidden (403) for %s - token not accepted by this endpoint", path)
                return None

            if e.code == 404:
                logger.debug("Resource not found (404): %s", path)
                return None

            if e.code == 429:
                raise BoschRateLimitError(f"Rate limited on {path}")

            body = ""
            try:
                body = e.read().decode()[:200]
            except Exception:
                pass
            raise BoschAPIError(f"API error {e.code} for {path}: {body}")

        except urllib.error.URLError as e:
            raise BoschAPIError("Network error. Check your connection.") from e

    raise BoschAuthError("Authentication failed after retry. Run: bosch-flow-mcp auth")
