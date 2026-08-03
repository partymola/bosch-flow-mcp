# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/partymola/bosch-flow-mcp/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/partymola/bosch-flow-mcp/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/partymola/bosch-flow-mcp/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/partymola/bosch-flow-mcp/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/partymola/bosch-flow-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/partymola/bosch-flow-mcp/releases/tag/v0.1.0
