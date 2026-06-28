"""Path resolution and constants for the Bosch Flow MCP server.

All paths are derived from environment variables or the package location.
No hardcoded personal paths.
"""

import os
from pathlib import Path

# Package root: bosch-flow-mcp/ (three levels up from this file)
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent

# Config directory: stores OAuth credentials and tokens (gitignored)
CONFIG_DIR = Path(os.environ.get("BOSCH_FLOW_MCP_CONFIG_DIR", _PACKAGE_ROOT / "config"))

# SQLite database path
DB_PATH = Path(os.environ.get("BOSCH_FLOW_MCP_DB_PATH", _PACKAGE_ROOT / "bosch_flow.db"))

# Token file path
BOSCH_TOKENS_PATH = CONFIG_DIR / "bosch_tokens.json"
# Optional config override (custom EUDA client_id / client_secret for Data Act API)
BOSCH_CONFIG_PATH = CONFIG_DIR / "bosch_config.json"

# --- OAuth (Bosch Keycloak) ---
BOSCH_AUTH_URL = "https://p9.authz.bosch.com/auth/realms/obc/protocol/openid-connect/auth"
BOSCH_TOKEN_URL = "https://p9.authz.bosch.com/auth/realms/obc/protocol/openid-connect/token"

# Primary client: one-bike-app (public client, PKCE, no secret needed)
CLIENT_ID = "one-bike-app"
REDIRECT_URI = "onebikeapp-ios://com.bosch.ebike.onebikeapp/oauth2redirect"
SCOPE = "openid offline_access"

# --- API base URLs ---
# Mobile API (primary - proven to work with one-bike-app tokens)
MOBILE_API_BASE = "https://obc-rider-profile.prod.connected-biking.cloud"
# Data Act API (secondary - may work with one-bike-app tokens, same Keycloak realm)
DATA_ACT_API_BASE = "https://api.bosch-ebike.com"

# --- Mobile API endpoint paths ---
MOBILE_BIKE_PROFILE_LIST = "/v1/bike-profile"
MOBILE_BIKE_PROFILE_V2 = "/v2/bike-profile/{bike_id}"
MOBILE_STATE_OF_CHARGE = "/v1/state-of-charge/{bike_id}"

# --- BES3 (Smart System) Data Act endpoint paths ---
BES3_BIKES = "/bike-profile/smart-system/v1/bikes"
BES3_BIKE = "/bike-profile/smart-system/v1/bikes/{bike_id}"
BES3_ACTIVITIES = "/activity/smart-system/v1/activities"
BES3_ACTIVITY_DETAILS = "/activity/smart-system/v1/activities/{activity_id}/details"
BES3_REGISTRATIONS = "/bike-registration/smart-system/v1/registrations"
BES3_CAPACITY_TESTERS = "/diagnosis-field-data/smart-system/v1/capacity-testers"
BES3_SERVICE_RECORDS = "/service-book/smart-system/v1/service-records"
BES3_BIKE_PASSES = "/bike-pass/smart-system/v1/bike-passes"
BES3_SW_UPDATES = "/software-update/smart-system/v1/installation-reports"
