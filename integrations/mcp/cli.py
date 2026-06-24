"""CLI entry point for the OpenUSDConnect MCP server (stdio transport).

Run with ``uv run python -m integrations.mcp``. Configure via flags or the
OPENUSDCONNECT_* environment variables (see config.py).
"""

from __future__ import annotations

import argparse
import logging
import sys

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
    _bootstrap_plugin_dll_dirs()
    parser = argparse.ArgumentParser(
        prog="openusdconnect-mcp",
        description="MCP server exposing the OpenUSDConnect event protocol as tools.",
    )
    parser.add_argument(
        "--host", help="Sync server host (default 127.0.0.1 or $OPENUSDCONNECT_HOST)"
    )
    parser.add_argument(
        "--port", type=int, help="Sync server port (default 7200 or $OPENUSDCONNECT_PORT)"
    )
    parser.add_argument("--client-id", dest="client_id", help="Stable per-client id for the layer")
    parser.add_argument("--department", help="Optional department for layer ordering")
    parser.add_argument("--no-mirror", action="store_true", help="Disable the introspection mirror")
    parser.add_argument("--log-level", default="INFO", help="stderr log level (default INFO)")
    args = parser.parse_args(argv)

    # stdout is the MCP transport, all logging must go to stderr.
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
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
