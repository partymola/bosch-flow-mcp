"""Entry point for the Bosch Flow MCP server.

Usage:
    bosch-flow-mcp               Start MCP server (stdio transport)
    bosch-flow-mcp auth          Interactive OAuth setup (PKCE with one-bike-app)
    bosch-flow-mcp sync          Sync all data to local cache
    bosch-flow-mcp --version     Print the installed package version
"""

import argparse
import logging
import sqlite3
import sys
from importlib.metadata import version

# Configure logging to stderr (stdout is reserved for JSON-RPC)
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from .auth import TokenRefused, setup_auth  # noqa: E402
from .mcp_instance import mcp  # noqa: E402


def main() -> None:
    # No args: start MCP server
    if len(sys.argv) == 1:
        # Import tools to register @mcp.tool() decorators
        from .tools import (  # noqa: F401
            activity_tools,
            analysis_tools,
            battery_tools,
            bike_tools,
            component_tools,
            service_tools,
            sync_tools,
        )

        mcp.run(transport="stdio")
        return

    parser = argparse.ArgumentParser(
        prog="bosch-flow-mcp",
        description="Bosch eBike Flow MCP server",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"bosch-flow-mcp {version('bosch-flow-mcp')}",
    )
    subparsers = parser.add_subparsers(dest="cmd")

    # auth subcommand
    subparsers.add_parser("auth", help="Interactive OAuth setup (opens browser)")

    # sync subcommand
    sync_parser = subparsers.add_parser("sync", help="Sync data to local cache")
    sync_parser.add_argument(
        "--types",
        default="all",
        help="Comma-separated data types: all, bikes, batteries, components, service, "
        "software_updates, capacity. Default: all",
    )

    args = parser.parse_args()

    try:
        if args.cmd == "auth":
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

    except TokenRefused as e:
        # This package's own text, naming a file by its base name only.
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except sqlite3.Error as e:
        # Its text says what is wrong, such as "database is locked", and names no file.
        print(f"Error: bosch-flow-mcp {args.cmd} failed ({e}).", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        # The type alone: its text names a path on this machine.
        print(f"Error: bosch-flow-mcp {args.cmd} failed ({type(e).__name__}).", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
