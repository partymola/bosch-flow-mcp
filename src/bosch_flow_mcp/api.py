"""Bosch Flow API client with automatic token refresh.

Requests carry whichever token the user authenticated with (one-bike-app for the
standard app sign-in, or a euda-* client for the EU Data Act API). The base URL
parameter selects which API to hit:
  - MOBILE_API_BASE (default): bike profiles, SoC, activities (one-bike-app token)
  - DATA_ACT_API_BASE: capacity testers, service book, registrations (euda token)

Each Keycloak client is accepted only by its own API host (a one-bike-app token
403s on the Data Act API and vice versa), so the sync layer routes by the token's
client_id rather than calling both.
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


class BoschForbiddenError(BoschAPIError):
    """403 Forbidden - this token's client is not accepted by the target API host.

    Raised (rather than swallowed) so the sync layer can tell "you used the wrong
    client for this endpoint" apart from "you genuinely have no data" (an empty 200).
    """


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
                raise BoschForbiddenError(
                    f"Forbidden (403) for {path}: this sign-in's client is not accepted by {base}"
                ) from e

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
