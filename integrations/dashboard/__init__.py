"""Optional dashboard with explicit, process-local lifecycle ownership."""

from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from weakref import WeakSet

if TYPE_CHECKING:
    from openusdconnect.server import UsdSyncServer

_dashboard_lock = threading.Lock()
_dashboard_app = None


class DashboardHandle:
    """Own a dashboard thread. NiceGUI allows one active dashboard per process."""

    def __init__(self, sync_server: UsdSyncServer, port: int = 8080):
        self.sync_server = sync_server
        self.port = port
        self.server = None
        self.thread = None
        self.error = None
        self._owns_app = False
        self._routes_before = None
        self._page_routes = set()
        self._clients = WeakSet()
        self._lifespan_before = None

    def start(self) -> DashboardHandle:
        global _dashboard_app
        from fastapi import FastAPI
        from nicegui import app, ui
        from nicegui.client import Client
        from uvicorn import Config, Server

        from .pages import setup_pages

        if not _dashboard_lock.acquire(blocking=False):
            raise RuntimeError("Only one dashboard may run in a process")
        self._owns_app = True
        try:
            if _dashboard_app is None:
                if app.config.has_run_config:
                    raise RuntimeError("The dashboard cannot own an existing NiceGUI application")
                _dashboard_app = FastAPI()
                ui.run_with(
                    _dashboard_app, title="OpenUSDConnect Dashboard", show_welcome_message=False
                )
            self._routes_before = list(app.routes)
            page_routes_before = set(Client.page_routes)
            try:
                setup_pages(self.sync_server, on_client=self._clients.add)
            finally:
                # NiceGUI also retains the original page functions outside the router.
                # Capture partial registration even when setup fails.
                self._page_routes = Client.page_routes.keys() - page_routes_before
            self._lifespan_before = _dashboard_app.router.lifespan_context

            @asynccontextmanager
            async def lifespan(application):
                async with self._lifespan_before(application) as state:
                    try:
                        yield state
                    finally:
                        # Delete our pages while NiceGUI and its event loop are still active.
                        for client in list(self._clients):
                            if Client.instances.get(client.id) is client:
                                client.delete()
                        self._clients.clear()

            _dashboard_app.router.lifespan_context = lifespan
            self.server = Server(
                Config(
                    _dashboard_app,
                    host="0.0.0.0",
                    port=self.port,
                    log_level="warning",
                    timeout_graceful_shutdown=5,
                )
            )
            self.thread = threading.Thread(
                target=self._run, name="OpenUSDConnect_Dashboard", daemon=True
            )
            self.thread.start()
            deadline = time.monotonic() + 10
            while not self.server.started:
                if not self.thread.is_alive():
                    raise RuntimeError("Dashboard startup failed") from self.error
                if time.monotonic() >= deadline:
                    raise TimeoutError("Dashboard startup timed out")
                self.thread.join(timeout=0.01)
        except BaseException:
            self.stop()
            raise
        return self

    def _run(self):
        try:
            self.server.run()
        except BaseException as exc:
            self.error = exc

    def stop(self) -> None:
        if not self._owns_app:
            return
        try:
            if self.server is not None:
                self.server.should_exit = True
            if self.thread is not None and self.thread.ident is not None:
                self.thread.join()
            if self._lifespan_before is not None:
                _dashboard_app.router.lifespan_context = self._lifespan_before
                self._lifespan_before = None
            if self._routes_before is not None:
                from nicegui import app
                from nicegui.client import Client

                # Remove closures over the old sync state before a subsequent start.
                app.router.routes[:] = self._routes_before
                app.openapi_schema = None
                for page in self._page_routes:
                    Client.page_routes.pop(page, None)
                self._page_routes.clear()
        finally:
            self._owns_app = False
            _dashboard_lock.release()


def run_dashboard(sync_server: UsdSyncServer, port: int = 8080) -> DashboardHandle:
    """Start the dashboard and return an explicit stop handle."""
    return DashboardHandle(sync_server, port).start()
