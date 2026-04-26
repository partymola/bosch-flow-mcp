# Contributing to bosch-flow-mcp

Thanks for your interest in contributing. This is a community MCP server for the Bosch eBike Flow APIs - it talks to the EU Data Act API and the same mobile-app API the Bosch Flow iOS/Android app uses.

## Getting started

### Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- A Bosch eBike Flow account with at least one BES3 Smart System eBike registered in the app

### Set up the dev environment

```bash
git clone https://github.com/partymola/bosch-flow-mcp
cd bosch-flow-mcp
uv venv --python 3.13 .venv
uv pip install -e ".[dev]"
```

### Install the pre-commit hook

The repo ships with `scripts/check-no-data.sh`, which blocks commits that contain databases, tokens, or other secrets:

```bash
ln -sf ../../scripts/check-no-data.sh .git/hooks/pre-commit
```

Please install it before your first commit.

### Run the test suite

```bash
.venv/bin/python -m pytest tests/ -v
```

Tests are fully offline - no real API calls, no real tokens. Fixtures in `tests/conftest.py` use fictional UUIDs and round numbers only; never paste real bike data into tests.

## Making changes

- **Open an issue first** for non-trivial changes (new tools, schema migrations, API additions, breaking changes). Small fixes (typos, bug fixes, docs) can go straight to a PR.
- Keep PRs small and focused.
- Add or update tests for any behaviour change.
- Run `pytest tests/ -v` before opening a PR.

## Pull requests

- Branch off `main`.
- Reference any related issue.
- Maintainer aims to reply within ~7 days. Feel free to bump if you don't hear back.

## Reporting issues

Helpful details to include:

- Python version (`python --version`)
- MCP client (Claude Desktop, Claude Code, other)
- The Bosch eBike model / generation if relevant (Smart System / BES3 only is supported)
- Steps to reproduce
- Relevant log output, with any tokens, bike serial numbers, or frame numbers redacted

## Security

Please do not open a public issue for credential, OAuth-flow, or token-leak issues. Use [GitHub's private vulnerability reporting](https://github.com/partymola/bosch-flow-mcp/security/advisories/new) instead.

## License

By contributing, you agree that your contributions are licensed under GPL-3.0-or-later, the project's license.
