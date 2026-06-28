# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/partymola/bosch-flow-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/partymola/bosch-flow-mcp/releases/tag/v0.1.0
