"""Deterministic layer-authoring fixture for the native-scene viewer."""

from __future__ import annotations

import uuid

from PySide6 import QtCore

from openusdconnect import EventSender

PRIM_PATH = "/World/ProjectionDemo"


class ProjectionDemo(QtCore.QObject):
    """Author two layer strengths so masking is visible in the adapter log."""

    phase_changed = QtCore.Signal(str)

    def __init__(self, host: str, port: int, parent=None):
        super().__init__(parent)
        run_id = uuid.uuid4().hex[:8]
        self.layout = EventSender(
            host,
            port,
            client_id=f"qt-layout-{run_id}",
            origin=f"qt-layout-{run_id}",
            department="layout",
        )
        self.animation = EventSender(
            host,
            port,
            client_id=f"qt-animation-{run_id}",
            origin=f"qt-animation-{run_id}",
            department="animation",
        )
        if not self.layout.connect() or not self.animation.connect():
            self.close()
            raise ConnectionError("could not connect the projection demo authors")
        self._timers: list[QtCore.QTimer] = []

    @staticmethod
    def _create_events(x: float, color: list[float]) -> list[dict]:
        return [
            {"k": "ensure_prim", "prim": PRIM_PATH, "typeName": "Cube"},
            {"k": "ensure_xform_ops", "prim": PRIM_PATH},
            {
                "k": "set_xform_trs",
                "prim": PRIM_PATH,
                "fields": ["t"],
                "t": [x, 0.0, 0.0],
            },
            {
                "k": "set_gprim_attrs",
                "prim": PRIM_PATH,
                "attrs": {"displayColor": [color]},
            },
        ]

    def _later(self, delay_ms: int, callback) -> None:
        timer = QtCore.QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(callback)
        timer.start(delay_ms)
        self._timers.append(timer)

    def run(self) -> None:
        self.phase_changed.emit("Preparing the two collaboration layers")
        self.animation.send_events([{"k": "delete_prim", "prim": PRIM_PATH}])
        self.layout.send_events([{"k": "delete_prim", "prim": PRIM_PATH}])

        def layout_authors() -> None:
            self.phase_changed.emit("Layout authors x = -4 (visible)")
            self.layout.send_events(self._create_events(-4.0, [0.12, 0.48, 1.0]))

        def animation_overrides() -> None:
            self.phase_changed.emit("Animation authors x = +4 (stronger layer wins)")
            self.animation.send_events(self._create_events(4.0, [1.0, 0.32, 0.12]))

        def layout_is_masked() -> None:
            self.phase_changed.emit(
                "Layout changes x to -1; composition is still +4, so no adapter event"
            )
            self.layout.send_events(
                [
                    {
                        "k": "set_xform_trs",
                        "prim": PRIM_PATH,
                        "fields": ["t"],
                        "t": [-1.0, 0.0, 0.0],
                    }
                ]
            )

        def reveal_layout() -> None:
            self.phase_changed.emit("Animation removes its spec; projection reveals layout x = -1")
            self.animation.send_events([{"k": "delete_prim", "prim": PRIM_PATH}])

        self._later(300, layout_authors)
        self._later(1500, animation_overrides)
        self._later(2700, layout_is_masked)
        self._later(3900, reveal_layout)

    def close(self) -> None:
        self.layout.disconnect()
        self.animation.disconnect()


__all__ = ["PRIM_PATH", "ProjectionDemo"]
