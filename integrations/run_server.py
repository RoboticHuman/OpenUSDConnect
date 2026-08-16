"""Compatibility launcher that derives RenderMan DLL paths from ``RMANTREE``.

The standard server already handles ``OPENUSDCONNECT_DLL_DIRS`` and Sdr
preflight. This source-tree wrapper only provides RenderMan-specific path
discovery before handing off to that command.

    uv run python -m integrations.run_server --port 7200 --base scene.usda --event-log events.db
"""

from __future__ import annotations

from .renderman import apply_dll_dirs


def main() -> int:
    apply_dll_dirs()
    from openusdconnect.server import main as server_main

    return server_main()


if __name__ == "__main__":
    raise SystemExit(main())
