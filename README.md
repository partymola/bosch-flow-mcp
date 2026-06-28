# bosch-flow-mcp

[![CI](https://github.com/partymola/bosch-flow-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/partymola/bosch-flow-mcp/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)

MCP server for Bosch eBike Flow (Smart System / BES3). Tracks battery health, charge cycles,
component versions, service history, and live state-of-charge.

## Disclaimer

This is an **unofficial**, community-built project. It is **not affiliated with, authorised
by, or endorsed by Robert Bosch GmbH, Bosch eBike Systems, or SingleKey ID**. "Bosch",
"eBike Flow", and related marks belong to their owners and are used here only to describe
interoperability.

- It signs in with **your own Bosch eBike Flow account** (via the standard SingleKey ID
  login) and reads **only your own data**. It is **read-only** - it never modifies your
  bike, battery, or account.
- It uses the same **public** OAuth client (`one-bike-app`, PKCE) that the official Bosch
  eBike Flow app uses. **No credentials, secrets, or protection measures are extracted,
  bypassed, or circumvented** - every identifier here is already publicly documented.
- When you supply your own EU Data Act API credentials, the official Data Act API is used.
  Otherwise the same app API your phone already uses is queried with your own login.
- This is an **undocumented, unofficial** interface that **may change or stop working at any
  time** if Bosch alters their systems.
- **You are responsible** for ensuring your use complies with Bosch's and SingleKey ID's
  terms of service in your jurisdiction.
- Provided **with no warranty** under GPLv3+ (see [LICENSE](LICENSE)). Use at your own risk.

## Features

- Battery state snapshots over time (charge cycles, energy delivered, degradation trends)
- Component registrations (drive unit, display, ConnectModule, battery)
- Service book history and software update log
- Live state-of-charge from ConnectModule via mobile API
- Battery capacity tester results
- Auto-sync on demand - tools fetch fresh data without a cron job

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- A Bosch eBike Flow account (free, register at the Bosch eBike Flow app)
- A BES3 Smart System eBike registered in the app

## Install

```bash
git clone https://github.com/partymola/bosch-flow-mcp
cd bosch-flow-mcp
uv venv --python 3.13 .venv
uv pip install -e .
```

## Auth

```bash
.venv/bin/bosch-flow-mcp auth
```

Opens your browser to Bosch login. The auth flow uses the `one-bike-app` public client
with PKCE - no API keys or registration needed. Just your Bosch Flow account.

**Important:** Open browser DevTools (F12) and switch to the Network tab **before** logging in.
After login, the browser redirects to an iOS URI (`onebikeapp-ios://`) that desktop browsers
can't open. Copy the full redirect URL from DevTools (right-click > Copy URL on the
`oauth2redirect` entry) and paste it at the prompt.

Tokens are saved to `config/bosch_tokens.json` and auto-refresh via `offline_access`.

## Sync

```bash
.venv/bin/bosch-flow-mcp sync
```

Fetches your data and stores it locally. The source depends on your sign-in:

- A standard Bosch eBike Flow account (the default) reads bikes, batteries, components,
  current firmware, and live state-of-charge from the **mobile app API** - works for any
  account, including non-EU.
- Service-book history, software-update history, and capacity-tester results come only
  from the **EU Data Act API**, which requires registering your own `euda` client at the
  [Bosch Data Act portal](https://portal.bosch-ebike.com/data-act). With a standard sign-in
  those types report `unavailable` (with a note) rather than a silent empty result, and the
  Data Act API returns nothing for accounts registered outside the EU.

You can also use the `bosch_sync` MCP tool, or rely on automatic sync (each `get_*` tool
triggers a sync if data is stale).

## Register with Claude Code

```bash
claude mcp add -s user bosch-flow -- /full/path/to/bosch-flow-mcp/.venv/bin/bosch-flow-mcp
```

Then ask Claude questions like:
- "What's my bike's battery health this year?"
- "Show me charge cycle trends by month"
- "What firmware version is my drive unit on?"
- "Have there been any service records for my bike?"

## Available tools

| Tool | Description |
|------|-------------|
| `bosch_sync` | Sync one or more data types (default: all) |
| `bosch_get_bikes` | List registered bikes |
| `bosch_get_bike` | Single bike with full details |
| `bosch_get_batteries` | Battery snapshots - latest or historical range |
| `bosch_get_soc` | Live state-of-charge from ConnectModule |
| `bosch_get_capacity` | Battery capacity tester results |
| `bosch_get_components` | Component registrations and software versions |
| `bosch_get_service_records` | Service book entries |
| `bosch_get_software_updates` | Software update history |
| `bosch_battery_trends` | Charge cycle and energy trends by period |

## API credits

This server uses the **Bosch Mobile API** (`obc-rider-profile.prod.connected-biking.cloud`)
as the primary data source, with optional **Data Act API** (`api.bosch-ebike.com`) support
for additional endpoints.

Authentication uses the `one-bike-app` public client (the same OAuth client as the Bosch
eBike Flow mobile app). The auth approach was documented by the
[marq24/ha-bosch-ebike-flow](https://github.com/marq24/ha-bosch-ebike-flow) Home Assistant
integration and the [open-ebike/open-ebike-backend](https://github.com/open-ebike/open-ebike-backend)
project.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, the test workflow, and the pre-commit hook. Changes are tracked in [CHANGELOG.md](CHANGELOG.md).

## License

GPLv3+. See [LICENSE](LICENSE).
