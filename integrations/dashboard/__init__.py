"""Admin dashboard for the OpenUSDConnect server.

Optional web UI served alongside the TCP sync server. Requires the
``dashboard`` dependency group (nicegui).

Usage:
    python -m openusdconnect --dashboard 8080
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openusdconnect.server import UsdSyncServer


def run_dashboard(sync_server: UsdSyncServer, port: int = 8080):
    """Start the NiceGUI dashboard in a background thread."""
    from .pages import setup_pages

    try:
        from nicegui import ui
    except ImportError as e:
        raise ImportError(
            "Dashboard requires the 'dashboard' dependency group. "
            "Install with: uv sync --group dashboard"
        ) from e

    setup_pages(sync_server)

    import threading

    def _run():
        ui.run(
            host="0.0.0.0",
            port=port,
            title="OpenUSDConnect Dashboard",
            reload=False,
            show=False,
        )

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
