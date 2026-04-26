# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
