"""CLI entry point for the OpenUSDConnect MCP server (stdio transport).

Run with ``uv run python -m integrations.mcp``. Configure via flags or the
OPENUSDCONNECT_* environment variables (see config.py).
"""

from __future__ import annotations

import argparse
import logging
import sys

from openusdconnect.cli_common import add_sync_endpoint_args, positive_seconds

from .config import McpConfig
from .tools import build_server


def _bootstrap_plugin_dll_dirs() -> None:
    """Make renderer plugin DLLs loadable before Sdr is first touched.

    RenderMan's plugins live in the shared USD install and need their runtime
    DLLs on PATH or the Sdr registry fails to load. ``integrations.renderman``
    owns that RenderMan-specific knowledge (discovered from ``RMANTREE``);
    ``OPENUSDCONNECT_DLL_DIRS`` covers any other plugin dependency dirs.
    """
    from openusdconnect.dll_paths import apply_env_dll_dirs

    from ..renderman import apply_dll_dirs

    apply_dll_dirs()
    apply_env_dll_dirs()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m integrations.mcp",
        description="MCP server exposing the OpenUSDConnect event protocol as tools.",
    )
    endpoint = parser.add_argument_group("sync endpoint")
    add_sync_endpoint_args(endpoint, host_default=None, port_default=None)
    parser.add_argument(
        "--client-id",
        dest="client_id",
        help="Stable client identity for authentication and producer replay",
    )
    parser.add_argument(
        "--department",
        help="Optional department identity; ordering requires server department policy",
    )
    behavior = parser.add_argument_group("behavior")
    behavior.add_argument(
        "--mirror",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Maintain the introspection mirror",
    )
    behavior.add_argument(
        "--auto-connect",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Connect automatically when the first authoring tool is called",
    )
    behavior.add_argument(
        "--auto-create-ancestors",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Create missing parent prims as Xforms",
    )
    behavior.add_argument(
        "--read-after-write-timeout",
        type=positive_seconds,
        default=None,
        metavar="SECONDS",
        help="Time to wait for the mirror after a write",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
        help="stderr log level",
    )
    args = parser.parse_args(argv)

    _bootstrap_plugin_dll_dirs()

    # stdout is the MCP transport, all logging must go to stderr.
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = McpConfig.from_env().merge_args(args)
    try:
        server = build_server(config)
    except ImportError as exc:
        sys.exit(str(exc))
    server.run()


if __name__ == "__main__":
    main()
