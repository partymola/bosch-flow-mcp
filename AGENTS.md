# bosch-flow-mcp - agent guide

`CLAUDE.md` symlinks to this file. It orients AI agents and contributors working *in* the code, and deliberately does not repeat the user-facing docs:

- **What it is, install, auth, sync, tools, config, usage** -> [README.md](README.md)
- **Dev environment, running tests, pre-commit hook, PR & security process** -> [CONTRIBUTING.md](CONTRIBUTING.md)

**This is a public open-source repository.** Read the Data Safety Rules before committing.

## Data Safety Rules

The `scripts/check-no-data.sh` pre-commit hook blocks database files, real files under `config/`, and anything over 100KB (install per [CONTRIBUTING.md](CONTRIBUTING.md)). **It does not grep for secrets**, and it matches `config/` by path, not `tokens.json` by name - a credential file staged anywhere else passes. Use the list below regardless:

1. `config/bosch_tokens.json` and `bosch_flow.db` must stay gitignored - verify `git status` shows no token/db files
2. Test fixtures (`tests/conftest.py`) use fictional UUIDs (`00000000-0000-0000-0000-000000000001`) and round numbers only - no real bike data ever enters tests
3. `config/bosch_config.example.json` (committed) holds only the public EUDA client ID and blank fields - confirm no real credentials

## Architecture

- **Entry point**: `src/bosch_flow_mcp/cli.py` - routes `auth`/`sync` subcommands or starts the MCP stdio server
- **MCP server**: `mcp_instance.py` creates the shared `MCPServer` instance
- **Auth**: `auth.py` - two flows. The default `one-bike-app` PKCE flow (iOS deep-link redirect, DevTools copy-paste). If `config/bosch_config.json` holds a EUDA `client_id`, auth switches to the EUDA flow (plain `localhost:4200` callback). `token_is_euda()` reads the token file fresh each call, so routing follows the current sign-in without a restart
- **API**: `api.py` - GET wrapper with thread-safe token refresh (5-min expiry buffer) and typed exceptions: `BoschAuthError`, `BoschRateLimitError`, `BoschAPIError`, `BoschForbiddenError`. **Three are siblings off `Exception`; `BoschForbiddenError` is the one subclass, of `BoschAPIError`.** So `except BoschAPIError` catches a 403 but not a 429 - which is both why a 403's message reached `sync_log` (it was caught, and carried the request path) and why every layer that handles failures has to name `BoschRateLimitError` separately
- **Failure classification**: `refresh_token` is a boundary over `_refresh_token` and raises exactly two types. `TokenRefused` only where the server or the credential files judged the credentials unusable; `RefreshNetworkError` for everything else, via a catch-all, so an unanticipated failure lands there by construction rather than by listing exception types. **Never widen `TokenRefused` to a condition that can clear on its own** (a rate limit, a 403 from bot protection, an unreadable response): re-authorising rewrites the token file and spends a refresh token that was still working. Pinned by `TestTheRefreshBoundary` and `TestRefusalsAndNetworkConditions` in `tests/test_auth.py`
- **Client routing**: `current_client_id` and `token_is_euda` are called outside any handler and must never raise. They fall back to the **configured** client, not the hardcoded one - a EUDA user with a half-written token file would otherwise route as non-EUDA and be told to register a client they already have. `_get_client_id` carries its own guard for that promise; `_is_euda` is deliberately left unguarded, because its only caller is the interactive auth command where a malformed config should stop the user rather than send them through a browser login that yields the wrong client. Pinned by `TestTheFallbackKeepsTheConfiguredClient` and `TestTheRoutingHelpersNeverRaise`
- **Sync**: `tools/sync_tools.py` - routes each data type by the token's client. Standard mobile sign-in reads bikes/batteries/components/firmware/SoC; `service`/`software_updates`/`capacity` need the EUDA client and otherwise report `unavailable` (not a silent empty). Non-EU EUDA accounts report `empty`/`euda_empty` with remedy text
- **DB**: `db.py` - SQLite, default `bosch_flow.db` in the package root (`BOSCH_FLOW_MCP_DB_PATH` to override; `BOSCH_FLOW_MCP_CONFIG_DIR` for the config dir). Besides the per-domain data tables, a `sync_log` table records each sync's timestamp, data type, status, and rows added - query it when data looks stale
- **Tools**: `tools/` - `@mcp.tool` definitions grouped by domain (bike, battery, component, service, analysis, activity, sync). Cached `get_*` tools auto-sync if stale; `bosch_get_soc`, `bosch_get_activities`, and `bosch_get_activity_detail` are live reads

## Key invariants

- **Capacity sync depends on components** - it needs part + serial numbers, so `components` must be synced first
- **Route by token client, never call both hosts blindly** - the sync layer picks the host from the authenticated client_id
- **Nothing an exception carries reaches a stored note or a tool result - not the response body, and not the request path.** `run_sync` writes its note into `sync_log`, and `helpers.empty_data_note` reads that row back and returns it as `note` on every empty `bosch_get_*` result, so anything stored is repeated to the model indefinitely. The capacity request path carries a part number and a battery serial, which is why the messages are the fixed `AUTH_FAILED_MSG` / `RATE_LIMITED_MSG` / `API_FAILED_MSG` plus at most an exception type name. `api.get` raises a status code and a path, never the body. Pinned by `TestNoRequestPathReachesTheSyncLogOrAModel` and `test_the_api_layer_never_puts_a_response_body_in_its_message` in `tests/test_sync.py`
- **`require_auth` is the backstop every tool has.** `BoschRateLimitError` is a sibling of `BoschAPIError` rather than a kind of it, so `except BoschAPIError` does not catch a 429. The three live reads catch `BoschAuthError`, `BoschForbiddenError` and `BoschAPIError` themselves and keep their own wording; the nine cached tools catch nothing. `BoschRateLimitError` is the one type nothing caught, so before the gate had its own `try` a rate limit reached the MCP client as a transport error rather than a tool result. `run_sync` needs its own handlers for the same reason, including a trailing catch-all. `InvalidDateError` passes through with its message intact - it is the server's own text and it tells the model how to retry, which is why it is a distinct type rather than a bare `ValueError` a JSON decode could also raise. Pinned by `TestTheAuthGate` in `tests/test_helpers.py`, which also pins the refusal when there are no credentials, and by `test_every_tool_is_gated`; nothing covered any of it before

## Test conventions

Tests are fully offline - no real API calls or tokens - and use temporary SQLite databases (`tmp_path` fixture), never the real DB. See [CONTRIBUTING.md](CONTRIBUTING.md) for how to run them.
