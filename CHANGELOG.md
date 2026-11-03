# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- A date range whose start is after its end is refused, naming both dates. It got an empty answer with no explanation. `start_date="30d", end_date="2020-01"` reached it. So does a start date after today with no end date. A refused date now costs no sync.
- The tools covering all records no longer stop at the year 2000. `bosch_battery_trends`, `bosch_get_service_records` and `bosch_get_software_updates` left out anything older. An end date before 2000 made an empty range.
- A failed bike list on an EU Data Act sign-in with an empty cache no longer records the other types as `ok` with 0 records. A 429 was enough, and the tools then reported no data for the rest of the UTC day. A type that finds nothing now fetches the list and records its failure as its own, so the next read tries again. Registrations that hold rows do not depend on the list.
- An EU Data Act sign-in whose bike list is empty reports `empty` with the non-EU explanation for every data type that stored nothing. `service`, `software_updates` and `capacity` recorded `ok` with 0 records. `components` no longer gives that explanation when the bike list failed.
- A 404 or an answer that is not a bike list is a failure on an EU Data Act sign-in, and the next read tries again. Each read as an empty list, which told you to delete your config for the rest of the UTC day.
- A bike list wrapped as `{"data": {"attributes": {"bikes": [...]}}}` is read. It ended in an unexpected error.
- A bike list whose entries carry no bike id in text is a failure, and the next read tries again. On an EU Data Act sign-in it read as an empty list, which told you to delete your config for the rest of the UTC day. On the standard sign-in it, and a `bikes` or `data` value that is not a list, recorded `ok` with 0 records for the rest of the UTC day. An error answer whose `error` or `errors` is set is no longer stored as a bike when it carries an `id`.
- `capacity` fetches components itself when no battery is cached. It recorded `ok` with 0 records, so `bosch_get_capacity` on an empty cache answered with nothing for the rest of the UTC day, and so did a failed components sync. A failed fetch is now recorded as `capacity`'s own failure, and the next read tries again. A bike with no battery component still reports `ok` with 0 records. A battery registered in other capitals, such as `BATTERY`, is now recognised.
- A capacity tester answer holding no test stores nothing. `{"capacityTests": []}` was stored as a test with no date and counted as one record, and it added another row on every sync.
- A capacity tester answer of `{"data": null}` or `{"data": {...}}` is read. Each ended in an unexpected error on every sync, so `bosch_get_capacity` never filled.
- A capacity test with no date is stored once while Bosch sends it unchanged. It added another row on every sync, so `bosch_get_capacity` reported one test as many. Rows already stored that way stay. A test date that is an object or a list is stored as none rather than failing every sync.
- A locked or unreadable database while auto-sync opens the cache no longer stops a read tool before its own query. It answered with an unexpected error even when the lock had cleared.
- `bosch-flow-mcp auth` reports a failure in one line on stderr and exits 1. So does `bosch-flow-mcp sync` when it fails before any data type runs. A malformed config, a config directory that cannot be created, no input at the prompt, an unparseable pasted URL and a locked or unreadable database each ended in a traceback. That traceback named the install's paths. An interrupt now prints one line and exits 130. A data type that fails during `sync` still prints on stdout, and the command exits 0. A request to the EU Data Act sign-in's callback whose URL cannot be parsed is answered with a 400 rather than a traceback, and a connection dropped mid-request prints no traceback.

### Packaging

- The container image is built on Python 3.15 instead of 3.14. Python 3.15 joins the CI matrix and the supported-version classifiers. `requires-python` is unchanged at `>=3.13`.

## [0.8.0] - 2026-11-02

### Fixed

- A running server now picks up the tokens written by `bosch-flow-mcp auth` without a restart. It read the token file once and kept using that copy. The sync meanwhile routed by the client named in the file. After switching between the standard and the EU Data Act sign-in, requests carried the old client's token to the new client's host. They were refused. Once the old token expired, its refresh saved the old sign-in back over the new one.
- A token refresh no longer saves over a grant that `bosch-flow-mcp auth` saved before the refresh finished. The newer grant wins while its access token is still valid.
- A refused refresh no longer always asks for `bosch-flow-mcp auth`. If the token file holds a different refresh token by then, the server uses the new token if it is still valid. Otherwise it reports a network error, and the next request refreshes from the file. If the file cannot be read, it reports a network error, and the next request reads the file again.
- A refreshed token whose save fails is still kept by the running server, and the save is now retried on each request.
- The token file is replaced in one step rather than emptied and rewritten. Another server reading it while a token was being saved could find it empty or half-written. A save that failed part-way left it that way. On POSIX a reader now sees the old file or the new one, and a failed save leaves the old one in place. New files are still created owner-only on POSIX, and a symlinked file is saved where the link points. Saving now needs write access to the config directory. The new file belongs to whoever saved it, so run `bosch-flow-mcp auth` and the server as the same user. A token file mounted into a container on its own, rather than in its directory, cannot be renamed over, so mount the directory.
- A locked database no longer stops a sync part-way. A failure writing the sync log after a type finished stopped the remaining types. That type now reports its result as usual and the other types run. The sync log keeps showing that type's previous outcome until a later sync records one. The next read syncs the type again unless it already synced today.
- `bosch-flow-mcp auth` reports a failed exchange when the access token is blank or not valid token text. It printed `Auth complete` and saved that token over the existing file, and every later call was refused. The existing file is now left alone.
- `bosch-flow-mcp auth` treats an unusable refresh token in the response as missing. It saved that token, and the next refresh was refused. It now saves no refresh token and says to run it again when the access token expires.
- `bosch-flow-mcp auth` reports a token file it could not save, by the kind of error, and exits. It ended in a traceback.
- A date tool refuses month `00` as its end date. `2026-00` read as 31 December 2025 and answered with real data for the wrong range.
- A day count too large to compute, such as `99999999d`, got an unexpected error. So did `0000-12` as an end date. Both now get the accepted formats back. `9999-12` is now accepted as an end date.
- A token refresh answered 200 with no usable access token is now a network error. It told you to run `bosch-flow-mcp auth`, though such a response says nothing about your credentials. A non-text token was saved and sent. The next request asks again.
- A token refresh answered with a null, blank or otherwise unusable refresh token keeps the stored one. The unusable token replaced the live one, so the next refresh was refused.
- A token refresh answered with a refresh token but no usable access token keeps that refresh token. It was dropped, though the one sent may already be spent. The next refresh was then refused. The next request now refreshes from the new token.
- A token lifetime that is not a finite positive number counts as already expired. A lifetime of `"7200"` or null ended the refresh and lost a good token. In `bosch-flow-mcp auth` it ended in a traceback. An infinite lifetime never refreshed. The tokens are now saved, and every request refreshes while the answers stay like that. With no refresh token to use, `bosch-flow-mcp auth` applies the default lifetime instead.
- A stored access token that is blank or not valid token text is now refreshed rather than refused or sent. A stored refresh token of that kind is reported as missing. A stored expiry that is infinite, too large for a float or not a number counts as expired. An infinite or oversized one never refreshed, and text or null failed every request.
- A date range includes all of its end date. The end date was compared as text against stored timestamps, so every timestamp on that day was left out. A month's battery history lost its last day. `bosch_get_batteries` with `latest_only=False` and `start_date="1d"` returned none of today's snapshots. `bosch_battery_trends` and `bosch_get_software_updates` lost the same day.
- An end date with no start date now counts back from that end date. The missing start was counted back from today, so an older end date made an empty range. `bosch_battery_trends`, `bosch_get_service_records` and `bosch_get_software_updates` now return everything up to the end date. `bosch_get_activities`, and `bosch_get_batteries` with `latest_only=False`, now start 30 days before the end date.
- `bosch_battery_trends` says when a date range holds no battery data. It said to run `bosch_sync`, with data in the cache. An empty answer now also says why the last sync failed, as `bosch_get_batteries` does.
- `bosch-flow-mcp auth` reports a malformed HTTP response or a JSON body nested too deeply to parse. Each ended in a traceback after the authorisation code was spent.
- A `client_id` that is not text in the token file or the config no longer stops a sync. It raised an unexpected error before the sync started. A null one in the token file was sent to Bosch as `None` on a refresh. A blank one was sent as it stood. The configured client is now used instead, or the standard client if the config names none.
- `bosch-flow-mcp auth` stops on a config `client_id` that is neither text nor null, such as `0` or `[]`. It opened a login for the wrong client. A `client_id` of only spaces now runs the standard sign-in. It ran the EU Data Act sign-in with no client.
- A token file nested too deeply to parse counts as unreadable.
- The token file no longer stores `token_type`, because nothing reads it. It was copied from Bosch's answer unchecked.
- A cached row holding JSON too deep to parse no longer stops a read or a sync. Nor does a blob that is not valid UTF-8. The row comes back with that value as stored.

### Changed

- `mcp` 2.3.0, up from 2.1.1. `anyio` 4.15.1, up from 4.14.2. `tzdata` 2026.5, up from 2026.3.

## [0.7.0] - 2026-09-05

### Fixed

- `bosch_get_components` refuses a component type it cannot match instead of answering with an empty list. `component_type="DRIVEUNIT"` and `component_type="not-a-component"` both came back as no components, which reads as a bike without that part rather than as a filter the server could not use, and wrong case alone was enough to produce one. The filter is now matched against the types your bikes registered, ignoring case, and a value none of them match is refused with those types named. Bosch owns this vocabulary and a future bike can register a type this server has never seen, so the accepted values are read from your own data rather than declared in the tool's schema.
- A type stored under more than one spelling answers under all of them. A bike's profile keys and its registrations can describe the same part, so asking for `battery` no longer hides rows held as `Battery`.

### Changed

- `component_type=""` is refused rather than read as no filter at all. It previously returned every component on every bike, which answers a question the caller did not ask.

## [0.6.0] - 2026-09-02

### Fixed

- `bosch_battery_trends` reads the aggregation period it was asked for. Any value other than `weekly` or `quarterly` fell through to monthly, so a period the tool did not recognise returned correct monthly buckets labelled with whatever was sent. `"period": "not-a-period"` came back beside real figures. Nothing was empty and nothing failed, which made it worse than an error: the numbers were right and the label describing them was not.

### Changed

- `period` accepts only `weekly`, `monthly` or `quarterly`, and those values are declared in the tool's schema rather than chosen inside it. A caller sending anything else now gets a validation error naming the accepted values. The values are unpacked from the dispatch, so the schema offers exactly the periods there is a key function for.

## [0.5.1] - 2026-08-31

### Security

- `bosch-flow-mcp auth` no longer prints the token server's own words when the code exchange fails. A refused exchange printed up to 200 bytes of the error body, a transport failure printed the exception's own text, and a response with no access token printed the whole token dictionary. That last one matters most, because the shape that reaches it is a response carrying a `refresh_token` and no `access_token`, so explaining the failure put a live credential on the terminal and into whatever keeps its scrollback. The failures now report a status code, an exception type, and fixed text respectively.
- The same command now reports a read timeout, a reset connection, a body that is not JSON, and a token response that is not an object, instead of ending in a traceback. `urlopen` wraps only connect-phase failures, so each of those escaped both handlers, and the traceback named the paths of the machine it ran on.

### Changed

- `mcp` 2.1.1, up from 2.0.0. Nothing a caller sees changes: this server's auth gate turns every failure into a tool result rather than letting it escape, so the release's masking of unconverted exception messages has nothing to act on here.

## [0.5.0] - 2026-08-21

### Fixed

- `bosch-flow-mcp auth` reports a busy callback port instead of ending in a traceback, and binds it before opening the browser rather than after, so a run that cannot receive the callback does not send you to a Bosch login page first. Applies to the EUDA flow, the only one that listens on a port; the default `one-bike-app` flow is unaffected.

### Packaging

- The package declares `Operating System :: OS Independent`, and CI runs the suite on Linux, macOS and Windows. It had only ever been tested on Linux while claiming every platform. The suite passed on all three unchanged.

### Security

- On Windows, the EUDA `bosch-flow-mcp auth` flow no longer lets another process take over the port the authorisation code arrives on. The callback listener inherited `allow_reuse_address` from `http.server`, which on POSIX only waives the wait after a previous run. Windows reads it as consent to be displaced, so another process could bind over the live listener and receive the callback - and the code it carries is enough to obtain the tokens. The listener no longer asks to share the address, which is what closes it, and asks for exclusive use as well. The cost is Windows-only: after a run the port stays held until the previous connection has finished closing, normally a couple of minutes, and `auth` has to be retried until it has. Nothing changes on Linux or macOS, and the default `one-bike-app` flow never bound a port at all.

## [0.4.0] - 2026-08-09

### Fixed

- Every way of failing to obtain an access token is now classified, and the helpers that decide which client is in use fall back to the configured client instead of raising. Only a refusal is an authentication failure: HTTP 400 or 401, a response carrying no token, or credentials that are missing, unreadable or malformed. Everything else is a network error - an unreachable server, a read timeout, a reset connection, a body that will not decode, a 403, a rate limit, and a 5xx. Previously every `RuntimeError` from the refresh became an authentication error while a bare `OSError` or an unreadable response escaped uncaught.

  The classification is made where the token is obtained, with a catch-all at that boundary, so an unanticipated failure is a network error by construction rather than by listing exception types. The distinction matters because an authentication failure tells the user to re-authorise, which rewrites the token file and spends a refresh token that was still working - the wrong answer to a rate limit or a dropped connection.
- The message reported when a token cannot be obtained is fixed text. It previously carried the underlying exception; a credential-file failure also escaped uncaught, putting an absolute path in front of the user either way.
- No part of a Bosch response reaches a stored note or a tool result. A failed request put the first 200 bytes of the response body into its message, the sync log stored that message, and every later empty read handed it back to the model as the explanation for why there was no data - so an error body quoting an account or a frame serial was both kept on disk and repeated. Messages now carry the status code and the request path.
- An authentication failure during sync is recorded in `sync_log`. API errors were logged and this one was not, so the failure that will not clear itself was the one leaving no trace: the run reported it once, and afterwards queries carried on serving the cache with nothing anywhere saying why it had stopped growing.
- A rate limit, or any failure the sync loop does not name, now records a row and lets the rest of the sync continue. Both used to end the whole run with nothing written, because the rate-limit error is a sibling of the general API error rather than a kind of it, so the handler never saw it.
- A rate limit reaches the model as a result rather than as a transport error. It is the one failure nothing caught: the live-reading tools handle the other kinds themselves, so only this one escaped and left the caller with a protocol-level failure and no explanation.
- No part of a request path is stored or repeated. The messages recorded for a failed sync named the endpoint, and for battery capacity that includes a part number and a battery serial - which was written to the sync log and then returned to the model as the explanation for every later empty result, indefinitely. Failures are now recorded as fixed text plus the kind of failure.

### Packaging

- The container image is built on Python 3.14 instead of 3.13, and 3.14 joins the supported-version classifiers. `requires-python` is unchanged at `>=3.13`: the package still supports both, and only the published image moves. Installing from PyPI is unaffected - that uses whichever Python the user already has.
- Dependency updates are automated. Every dependency, the base image and the CI actions are pinned to exact versions, so nothing changes without a deliberate bump; Dependabot now proposes those bumps rather than leaving the pins to rot.

## [0.3.0] - 2026-08-03

### Changed

- Ported to the `mcp` 2.x server API. 2.0.0 renamed `mcp.server.fastmcp` to `mcp.server.mcpserver` and the `FastMCP` class to `MCPServer`, with no compatibility alias. The tool contract is unchanged: every tool keeps its name, description, and input and output schemas.
- Every dependency is pinned to an exact version instead of a lower bound: `mcp` 2.0.0, `anyio` 4.14.2, `tzdata` 2026.3, and for development `pytest` 9.1.1, `pytest-asyncio` 1.4.0 and `ruff` 0.16.1.

### Fixed

- A fresh install no longer breaks on import. The `mcp` spec was `>=1.6.0` with no upper bound, so once 2.0.0 was published the resolver picked it and the server failed to start.

### Packaging

- The build toolchain is pinned alongside the dependencies: `setuptools` to an exact version, the `python:3.13-slim` base image by digest, and every GitHub Action to a full commit SHA rather than a moving major tag. A floating tag can change what a build produces with nobody deciding, which is the same failure the dependency pins address.

## [0.2.2] - 2026-07-11

### Packaging

- Listed in the official MCP registry (`io.github.partymola/bosch-flow-mcp`); the release workflow now publishes to the registry alongside PyPI.

## [0.2.1] - 2026-07-11

### Added

- `bosch-flow-mcp --version` prints the installed package version.

### Packaging

- Published to PyPI (`pip install bosch-flow-mcp` / `uvx bosch-flow-mcp`) via GitHub Actions Trusted Publishing.

## [0.2.0] - 2026-07-11

### Changed

- Sync requests are now routed by the active token's client instead of trying both APIs: a `euda` client uses the EU Data Act API, the standard `one-bike-app` sign-in uses the mobile app API (each Keycloak client is only accepted by its own host).
- Components and current firmware are read from the mobile bike profile under a standard sign-in, so non-EU accounts get component data without an EU Data Act registration.
- Sync results report a per-type status (`ok` / `empty` / `unavailable` / `error`) with a machine code and human message instead of a silent `0 records`.

### Fixed

- A `euda` token on a non-EU account no longer fails silently (Data Act returned an empty 200, the mobile fallback 403'd, and everything cached as zero); it now reports `empty` / `euda_empty` with the remedy.
- Data-Act-only types (service book, software-update history, capacity) report `unavailable` under a standard sign-in instead of an empty result, and skip the doomed request.
- Components with no serial number no longer accumulate duplicate rows on every sync (SQLite treats NULLs as distinct in the UNIQUE index); each bike's components are reconciled as current state.
- `403 Forbidden` now raises `BoschForbiddenError` instead of being swallowed as an empty result; the live state-of-charge tool maps it to a client-aware hint.

### Added

- Ride activity tools: `bosch_get_activities` (per-ride summaries over a date range - distance, elevation gain/loss, avg/max speed, cadence, measured rider power, calories, rider-vs-motor energy share, assist-mode distance split (metres per mode), CO2, ABS/brake events) and `bosch_get_activity_detail` (per-point GPS/speed/elevation/cadence/power track). Live reads from the rider-activity API (`obc-rider-activity.prod.connected-biking.cloud`), reachable with the standard `one-bike-app` sign-in (scope `activity:user:read`) - no EU Data Act registration needed.
- An unofficial / not-affiliated / read-only disclaimer in the README and the `auth` flow.

## [0.1.0] - 2026-04-26

### Added

- Initial release.
- OAuth (PKCE) authentication against the Bosch `one-bike-app` public client.
- Optional EUDA (EU Data Act) credentials for capacity and service-book endpoints.
- Sync engine for `bikes`, `batteries`, `components`, `service`, `software_updates`, and `capacity` data types.
- Local SQLite cache (`bosch_flow.db`) with auto-sync on stale data.
- MCP tools: `bosch_sync`, `bosch_get_bikes`, `bosch_get_bike`, `bosch_get_batteries`, `bosch_get_soc`, `bosch_get_capacity`, `bosch_get_components`, `bosch_get_service_records`, `bosch_get_software_updates`, `bosch_battery_trends`.
- Live state-of-charge via the ConnectModule mobile API.
- Pre-commit hook (`scripts/check-no-data.sh`) blocking commit of databases, tokens, and other secrets.

[Unreleased]: https://github.com/partymola/bosch-flow-mcp/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/partymola/bosch-flow-mcp/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/partymola/bosch-flow-mcp/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/partymola/bosch-flow-mcp/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/partymola/bosch-flow-mcp/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/partymola/bosch-flow-mcp/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/partymola/bosch-flow-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/partymola/bosch-flow-mcp/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/partymola/bosch-flow-mcp/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/partymola/bosch-flow-mcp/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/partymola/bosch-flow-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/partymola/bosch-flow-mcp/releases/tag/v0.1.0
