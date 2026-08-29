"""usdview connection lifetime tests."""

import sys
from types import SimpleNamespace

from integrations.usdview import connection


class _Signal:
    def __init__(self):
        self.callback = None

    def connect(self, callback):
        assert self.callback is None
        self.callback = callback

    def disconnect(self, callback):
        assert self.callback is callback
        self.callback = None

    def emit(self):
        callback = self.callback
        assert callback is not None
        callback()


def test_qt_shutdown_stops_receiver_before_application_teardown(monkeypatch):
    signal = _Signal()
    app = SimpleNamespace(aboutToQuit=signal)
    qt_core = SimpleNamespace(
        QCoreApplication=SimpleNamespace(instance=lambda: app),
    )
    qt_module = SimpleNamespace(QtCore=qt_core)
    stopped = []

    monkeypatch.setitem(sys.modules, "pxr.Usdviewq.qt", qt_module)
    monkeypatch.setattr(connection, "_shutdown_hook_installed", False)
    monkeypatch.setattr(connection, "stop", lambda: stopped.append(True))

    connection._install_shutdown_hook()
    connection._install_shutdown_hook()

    assert signal.callback is connection._on_about_to_quit
    signal.emit()

    assert stopped == [True]
    assert signal.callback is None
    assert connection._shutdown_hook_installed is False
