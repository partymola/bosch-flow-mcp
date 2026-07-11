# bosch-flow-mcp - agent guide

`CLAUDE.md` symlinks to this file. It orients AI agents and contributors working *in* the code, and deliberately does not repeat the user-facing docs:

- **What it is, install, auth, sync, tools, config, usage** -> [README.md](README.md)
- **Dev environment, running tests, pre-commit hook, PR & security process** -> [CONTRIBUTING.md](CONTRIBUTING.md)

**This is a public open-source repository.** Read the Data Safety Rules before committing.

## Data Safety Rules

The `scripts/check-no-data.sh` pre-commit hook blocks `*.db`, `*tokens.json`, and secret patterns (install per [CONTRIBUTING.md](CONTRIBUTING.md)). With the hook installed most of this is automatic; use the list when it isn't yet installed or when adding test data:

1. `config/bosch_tokens.json`, `config/bosch_mobile_tokens.json`, and `bosch_flow.db` must stay gitignored - verify `git status` shows no token/db files
2. Test fixtures (`tests/conftest.py`) use fictional UUIDs (`00000000-0000-0000-0000-000000000001`) and round numbers only - no real bike data ever enters tests
3. `config/bosch_config.example.json` (committed) holds only the public EUDA client ID and blank fields - confirm no real credentials

## Architecture

- **Entry point**: `src/bosch_flow_mcp/cli.py` - routes `auth`/`sync` subcommands or starts the MCP stdio server
- **FastMCP**: `mcp_instance.py` creates the shared instance
- **Auth**: `auth.py` - two flows. The default `one-bike-app` PKCE flow (iOS deep-link redirect, DevTools copy-paste). If `config/bosch_config.json` holds a EUDA `client_id`, auth switches to the EUDA flow (plain `localhost:4200` callback). `token_is_euda()` reads the token file fresh each call, so routing follows the current sign-in without a restart
- **API**: `api.py` - GET wrapper with thread-safe token refresh (5-min expiry buffer) and a typed exception hierarchy (`BoschAuthError`, `BoschRateLimitError`, `BoschAPIError`, `BoschForbiddenError`)
- **Sync**: `tools/sync_tools.py` - routes each data type by the token's client. Standard mobile sign-in reads bikes/batteries/components/firmware/SoC; `service`/`software_updates`/`capacity` need the EUDA client and otherwise report `unavailable` (not a silent empty). Non-EU EUDA accounts report `empty`/`euda_empty` with remedy text
- **DB**: `db.py` - SQLite, default `bosch_flow.db` in the package root (`BOSCH_FLOW_MCP_DB_PATH` to override; `BOSCH_FLOW_MCP_CONFIG_DIR` for the config dir)
- **Tools**: `tools/` - `@mcp.tool` definitions grouped by domain (bike, battery, component, service, analysis, activity, sync). Cached `get_*` tools auto-sync if stale; `bosch_get_soc`, `bosch_get_activities`, and `bosch_get_activity_detail` are live reads

## Key invariants

- **Capacity sync depends on components** - it needs part + serial numbers, so `components` must be synced first
- **Route by token client, never call both hosts blindly** - the sync layer picks the host from the authenticated client_id

## Running tests

```bash
.venv/bin/python -m pytest tests/ -v
```

Tests use temporary SQLite databases (`tmp_path` fixture) and never touch the real DB. All tests are offline - no real API calls or tokens needed.
