"""PluginContainer entry point for usdview's libplug loader.

Pulls in PySide6 transitively via ``pxr.Usdviewq``, only safe to import
inside usdview's interpreter. The package ``__init__.py`` carries no Qt
imports so non-Qt callers (launcher, tests) can import siblings without Qt.
"""

from __future__ import annotations

import logging
import os

from pxr import Tf
from pxr.Usdviewq.plugin import PluginContainer

from openusdconnect.cli_common import parse_bool, validate_nonnegative_int, validate_port
from openusdconnect.defaults import DEFAULT_HOST, DEFAULT_SYNC_PORT

from . import connection

LOG = logging.getLogger("openusdconnect.usdview")


def _on_connect(usdviewApi) -> None:
    from pxr.Usdviewq.qt import QtWidgets

    default_host = os.environ.get("OPENUSDCONNECT_HOST", DEFAULT_HOST)
    default_port = os.environ.get("OPENUSDCONNECT_PORT", str(DEFAULT_SYNC_PORT))

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

    host = host_field.text().strip() or DEFAULT_HOST
    try:
        port = validate_port(port_field.text().strip())
    except ValueError:
        QtWidgets.QMessageBox.warning(
            usdviewApi.qMainWindow,
            "OpenUSDConnect",
            "Port must be an integer between 1 and 65535.",
        )
        return

    token = os.environ.get("OPENUSDCONNECT_TOKEN") or None
    try:
        if not connection.start(usdviewApi, host, port, token=token):
            QtWidgets.QMessageBox.warning(
                usdviewApi.qMainWindow,
                "OpenUSDConnect",
                "Failed to start receiver. See console for details.",
            )
    except Exception as exc:
        QtWidgets.QMessageBox.warning(
            usdviewApi.qMainWindow,
            "OpenUSDConnect",
            f"Failed to start receiver: {exc}",
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
        port = validate_port(os.environ.get("OPENUSDCONNECT_PORT", DEFAULT_SYNC_PORT))
    except ValueError:
        LOG.error("OPENUSDCONNECT_PORT is not a valid TCP port; skipping auto-connect")
        return
    token = os.environ.get("OPENUSDCONNECT_TOKEN") or None
    try:
        connection.start(usdviewApi, host, port, token=token)
    except Exception:
        LOG.exception("Auto-connect failed")


def _configure_presented_view(usdviewApi) -> None:
    """Apply optional camera and scene-light settings after replay settles."""
    camera_path = os.environ.get("OPENUSDCONNECT_CAMERA_PATH", "")
    try:
        use_scene_lights = parse_bool(os.environ.get("OPENUSDCONNECT_SCENE_LIGHTS", "0"))
    except ValueError:
        LOG.error("OPENUSDCONNECT_SCENE_LIGHTS is not a valid boolean; ignoring it")
        use_scene_lights = False
    if not camera_path and not use_scene_lights:
        return

    from pxr import UsdGeom
    from pxr.Usdviewq.qt import QtCore

    try:
        expected_seq = validate_nonnegative_int(
            os.environ.get("OPENUSDCONNECT_EXPECTED_SEQ", "0")
        )
    except ValueError:
        LOG.error("OPENUSDCONNECT_EXPECTED_SEQ is not a non-negative integer; using 0")
        expected_seq = 0
    timer = QtCore.QTimer(usdviewApi.qMainWindow)
    usdviewApi.qMainWindow._openusdconnect_presentation_timer = timer
    attempts = 0

    def poll() -> None:
        nonlocal attempts
        attempts += 1
        stage = usdviewApi.dataModel.stage
        status = connection.status()
        if stage is None or int(status.get("last_seq", 0)) < expected_seq:
            if attempts >= 1200:
                timer.stop()
                LOG.error("Timed out waiting for replay sequence %d", expected_seq)
            return

        camera_prim = stage.GetPrimAtPath(camera_path) if camera_path else None
        if camera_path and not (camera_prim and camera_prim.IsA(UsdGeom.Camera)):
            return

        settings = usdviewApi.dataModel.viewSettings
        if use_scene_lights:
            settings.enableSceneMaterials = True
            settings.enableSceneLights = True
            settings.ambientLightOnly = False
            settings.domeLightEnabled = False
            settings.domeLightTexturesVisible = True
        if camera_prim:
            settings.cameraPrim = camera_prim
        timer.stop()
        LOG.info(
            "Presentation ready at seq=%d camera=%s scene_lights=%s",
            status.get("last_seq", 0),
            camera_path or "<unchanged>",
            use_scene_lights,
        )

    timer.timeout.connect(poll)
    timer.start(100)


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
        _configure_presented_view(self._usdviewApi)


Tf.Type.Define(UsdConnectContainer)
