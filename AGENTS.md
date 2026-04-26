# bosch-flow-mcp

## What this is

MCP server for Bosch eBike Flow (Smart System / BES3). Provides battery health tracking,
charge cycle history, component registrations, service records, and live state-of-charge
via the EU Data Act API and Bosch Mobile App API.

Licensed GPLv3+. Published at partymola/bosch-flow-mcp.

## Auth

```bash
.venv/bin/bosch-flow-mcp auth
```

Uses the `one-bike-app` public client with PKCE (same as the Bosch Flow mobile app).
Browser opens to Bosch login, but the redirect URI is an iOS deep link (`onebikeapp-ios://...`).

**Steps:**
1. Open browser DevTools (F12) > Network tab BEFORE clicking the auth URL
2. Log in with your Bosch Flow account
3. Browser shows a failed redirect - find `oauth2redirect` in the Network tab
4. Copy the full URL (right-click > Copy URL)
5. Paste it at the prompt

Tokens saved to `config/bosch_tokens.json` (0600). Expires in ~2 hours, auto-refreshed via
`offline_access` scope.

**Optional EUDA credentials:** If you register at the Bosch Data Act portal, place your
credentials in `config/bosch_config.json` for additional Data Act API endpoints (capacity
testers, service book, etc.).

## Sync

```bash
.venv/bin/bosch-flow-mcp sync                     # all types
.venv/bin/bosch-flow-mcp sync --types bikes batteries
```

Data types: `bikes`, `batteries`, `components`, `service`, `software_updates`, `capacity`

Capacity sync requires components to be synced first (needs part+serial numbers).

## MCP tools

| Tool | Description |
|------|-------------|
| `bosch_sync` | Sync all or specific data types |
| `bosch_get_bikes` | List registered bikes |
| `bosch_get_bike` | Single bike details |
| `bosch_get_batteries` | Battery snapshots (history or latest) |
| `bosch_get_soc` | Live state-of-charge from ConnectModule |
| `bosch_get_capacity` | Battery capacity tester results |
| `bosch_get_components` | Component registrations (drive unit, display, etc.) |
| `bosch_get_service_records` | Service book history |
| `bosch_get_software_updates` | Software update history |
| `bosch_battery_trends` | Battery health trends (weekly/monthly/quarterly) |

All `get_*` tools auto-sync if data is stale (no cron job needed).

## Data safety - CRITICAL for public repo

The `scripts/check-no-data.sh` pre-commit hook blocks `*.db`, `*tokens.json`, and secrets patterns. Install it: `ln -sf ../../scripts/check-no-data.sh .git/hooks/pre-commit`. With the hook installed, most of the checks below are enforced automatically; use the list when the hook is not yet installed or when adding new test data.

1. Verify `config/bosch_tokens.json`, `config/bosch_mobile_tokens.json`, `bosch_flow.db` are gitignored
2. Check `.gitignore` entries are actually working: `git status` should show no token/db files
3. Test fixtures in `tests/conftest.py` use fictional UUIDs (`00000000-0000-0000-0000-000000000001`) and round numbers only - no real bike data ever enters tests
4. `config/bosch_config.example.json` contains only the public EUDA client ID and blank fields - this IS committed to the repo, confirm it has no real credentials

## DB location

Default: `bosch_flow.db` in the package root (same dir as `pyproject.toml`).
Override: `BOSCH_FLOW_MCP_DB_PATH=/path/to/custom.db`

## Running tests

```bash
.venv/bin/python -m pytest tests/ -v
```

Tests use temporary SQLite databases (`tmp_path` fixture) and never touch the real DB.
All tests are offline - no real API calls, no real tokens needed.

## Environment

```
BOSCH_FLOW_MCP_DB_PATH         Override DB file path
BOSCH_FLOW_MCP_CONFIG_DIR      Override config directory (tokens, client config)
```

## Registering with Claude Code

```bash
claude mcp add -s user bosch-flow -- /path/to/bosch-flow-mcp/.venv/bin/bosch-flow-mcp
```
