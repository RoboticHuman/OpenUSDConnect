"""PluginContainer entry point for usdview's libplug loader.

Pulls in PySide6 transitively via ``pxr.Usdviewq`` — only safe to import
inside usdview's interpreter. The package ``__init__.py`` is empty so
non-Qt callers (launcher, tests) can import siblings without Qt.
"""

from __future__ import annotations

import logging
import os

from pxr import Tf
from pxr.Usdviewq.plugin import PluginContainer

from . import connection

LOG = logging.getLogger("openusdconnect.usdview")


def _on_connect(usdviewApi) -> None:
    from pxr.Usdviewq.qt import QtWidgets

    default_host = os.environ.get("OPENUSDCONNECT_HOST", "127.0.0.1")
    default_port = os.environ.get("OPENUSDCONNECT_PORT", "7200")

    dialog = QtWidgets.QDialog(usdviewApi.qMainWindow)
    dialog.setWindowTitle("Connect to OpenUSDConnect")

    layout = QtWidgets.QFormLayout(dialog)
    host_field = QtWidgets.QLineEdit(default_host)
    port_field = QtWidgets.QLineEdit(default_port)
    layout.addRow("Host:", host_field)
    layout.addRow("Port:", port_field)

    buttons = QtWidgets.QDialogButtonBox(
        QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel,
        parent=dialog,
    )
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addRow(buttons)

    if dialog.exec_() != QtWidgets.QDialog.Accepted:
        return

    host = host_field.text().strip() or "127.0.0.1"
    try:
        port = int(port_field.text().strip())
    except ValueError:
        QtWidgets.QMessageBox.warning(
            usdviewApi.qMainWindow, "OpenUSDConnect", "Port must be an integer."
        )
        return

    token = os.environ.get("OPENUSDCONNECT_TOKEN") or None
    if not connection.start(usdviewApi, host, port, token=token):
        QtWidgets.QMessageBox.warning(
            usdviewApi.qMainWindow,
            "OpenUSDConnect",
            "Failed to start receiver — see console for details.",
        )


def _on_disconnect(usdviewApi) -> None:
    connection.stop()


def _on_status(usdviewApi) -> None:
    from pxr.Usdviewq.qt import QtWidgets

    info = connection.status()
    lines = [f"{key}: {value}" for key, value in info.items()]
    QtWidgets.QMessageBox.information(
        usdviewApi.qMainWindow, "OpenUSDConnect status", "\n".join(lines)
    )


def _autoconnect(usdviewApi) -> None:
    host = os.environ.get("OPENUSDCONNECT_HOST")
    if not host:
        return
    try:
        port = int(os.environ.get("OPENUSDCONNECT_PORT", "7200"))
    except ValueError:
        LOG.error("OPENUSDCONNECT_PORT is not an integer; skipping auto-connect")
        return
    token = os.environ.get("OPENUSDCONNECT_TOKEN") or None
    connection.start(usdviewApi, host, port, token=token)


class UsdConnectContainer(PluginContainer):
    def registerPlugins(self, plugRegistry, usdviewApi):
        self._usdviewApi = usdviewApi
        self._connectCmd = plugRegistry.registerCommandPlugin(
            "UsdConnectContainer.connect",
            "Connect to OpenUSDConnect…",
            _on_connect,
            description="Start receiving stage updates from an OpenUSDConnect server",
        )
        self._disconnectCmd = plugRegistry.registerCommandPlugin(
            "UsdConnectContainer.disconnect",
            "Disconnect",
            _on_disconnect,
            description="Stop the OpenUSDConnect receiver",
        )
        self._statusCmd = plugRegistry.registerCommandPlugin(
            "UsdConnectContainer.status",
            "Connection status…",
            _on_status,
            description="Show the current OpenUSDConnect connection state",
        )

    def configureView(self, plugRegistry, plugUIBuilder):
        menu = plugUIBuilder.findOrCreateMenu("OpenUSDConnect")
        menu.addItem(self._connectCmd)
        menu.addItem(self._disconnectCmd)
        menu.addSeparator()
        menu.addItem(self._statusCmd)

        if os.environ.get("OPENUSDCONNECT_HOST"):
            from pxr.Usdviewq.qt import QtCore

            QtCore.QTimer.singleShot(100, lambda: _autoconnect(self._usdviewApi))


Tf.Type.Define(UsdConnectContainer)
