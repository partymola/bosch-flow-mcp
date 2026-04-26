"""Entry point for the Bosch Flow MCP server.

Usage:
    bosch-flow-mcp               Start MCP server (stdio transport)
    bosch-flow-mcp auth          Interactive OAuth setup (PKCE with one-bike-app)
    bosch-flow-mcp sync          Sync all data to local cache
"""

import argparse
import logging
import sys

# Configure logging to stderr (stdout is reserved for JSON-RPC)
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from .mcp_instance import mcp


def main() -> None:
    # No args: start MCP server
    if len(sys.argv) == 1:
        # Import tools to register @mcp.tool() decorators
        from .tools import (  # noqa: F401
            sync_tools,
            bike_tools,
            battery_tools,
            component_tools,
            service_tools,
            analysis_tools,
        )
        mcp.run(transport="stdio")
        return

    parser = argparse.ArgumentParser(
        prog="bosch-flow-mcp",
        description="Bosch eBike Flow MCP server",
    )
    subparsers = parser.add_subparsers(dest="cmd")

    # auth subcommand
    subparsers.add_parser("auth", help="Interactive OAuth setup (opens browser)")

    # sync subcommand
    sync_parser = subparsers.add_parser("sync", help="Sync data to local cache")
    sync_parser.add_argument(
        "--types", default="all",
        help="Comma-separated data types: all, bikes, batteries, components, service, "
             "software_updates, capacity. Default: all",
    )

    args = parser.parse_args()

    if args.cmd == "auth":
        from .auth import setup_auth
        setup_auth()

    elif args.cmd == "sync":
        from .tools.sync_tools import run_sync
        types = [t.strip() for t in args.types.split(",")]
        if "all" in types:
            from .tools.sync_tools import _ALL_TYPES
            types = _ALL_TYPES
        print(f"Syncing: {', '.join(types)}")
        results = run_sync(types)
        for dtype, result in results.items():
            status = result.get("status", "?")
            records = result.get("records", 0)
            msg = result.get("message", "")
            if status == "ok":
                print(f"  {dtype}: {records} records")
            else:
                print(f"  {dtype}: {status} - {msg}")

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
