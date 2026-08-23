"""Qt viewer backed by a non-USD scene graph populated through DCCAdapter."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pxr import Usd  # noqa: E402
from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

from examples.qt_native_viewer.demo_driver import PRIM_PATH, ProjectionDemo  # noqa: E402
from examples.qt_native_viewer.gl_viewport import NativeViewport  # noqa: E402
from examples.qt_native_viewer.native_scene import NativeScene, NativeSceneAdapter  # noqa: E402
from openusdconnect import UsdReceiver  # noqa: E402


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, host: str, port: int, *, run_demo: bool):
        super().__init__()
        self.setWindowTitle("OpenUSDConnect native-scene projection")
        self.resize(1040, 680)

        self.native_scene = NativeScene()
        self.adapter = NativeSceneAdapter(self.native_scene, self._on_native_events)
        self.mirror_stage = Usd.Stage.CreateInMemory()
        self.client = UsdReceiver(
            self.mirror_stage,
            app_name="qt-native-viewer",
            host=host,
            port=port,
            adapter=self.adapter,
            on_resync=self.adapter.reset,
            persist_token=False,
        )

        self.viewport = NativeViewport(self.native_scene)
        self.outliner = QtWidgets.QTreeWidget()
        self.outliner.setHeaderLabels(["Native object", "Type", "Position"])
        self.outliner.setAlternatingRowColors(True)
        self.event_log = QtWidgets.QPlainTextEdit()
        self.event_log.setReadOnly(True)
        self.event_log.setPlaceholderText("Projected adapter events appear here")
        self.phase_label = QtWidgets.QLabel("Waiting for composed state")
        self.phase_label.setWordWrap(True)
        self.run_button = QtWidgets.QPushButton("Run masking demo")

        side = QtWidgets.QWidget()
        side_layout = QtWidgets.QVBoxLayout(side)
        side_layout.addWidget(QtWidgets.QLabel("Left drag: orbit · right drag: pan · wheel: zoom"))
        side_layout.addWidget(QtWidgets.QLabel("Adapter-owned scene"))
        side_layout.addWidget(self.outliner, 2)
        side_layout.addWidget(QtWidgets.QLabel("Projected events"))
        side_layout.addWidget(self.event_log, 3)
        side_layout.addWidget(self.phase_label)
        side_layout.addWidget(self.run_button)

        splitter = QtWidgets.QSplitter()
        splitter.addWidget(self.viewport)
        splitter.addWidget(side)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.setCentralWidget(splitter)

        self.client.start()
        self.demo_endpoint = (host, port)
        self.demo: ProjectionDemo | None = None
        self.run_button.clicked.connect(self._run_demo)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._drain)
        self.timer.start(16)
        if run_demo:
            QtCore.QTimer.singleShot(500, self._run_demo)

    def _run_demo(self) -> None:
        if self.demo is None:
            try:
                self.demo = ProjectionDemo(*self.demo_endpoint, self)
            except (ConnectionError, OSError) as exc:
                self.phase_label.setText(f"Could not start demo authors: {exc}")
                return
            self.demo.phase_changed.connect(self.phase_label.setText)
        self.run_button.setEnabled(False)
        self.demo.run()
        QtCore.QTimer.singleShot(4500, lambda: self.run_button.setEnabled(True))

    def _drain(self) -> None:
        try:
            self.client.update()
        except Exception as exc:
            self.timer.stop()
            self.phase_label.setText(f"Receiver stopped: {exc}")

    def _on_native_events(self, events: list[dict]) -> None:
        self.outliner.clear()
        for obj in sorted(self.native_scene.objects.values(), key=lambda item: item.path):
            position = ", ".join(f"{value:.2f}" for value in obj.translation)
            self.outliner.addTopLevelItem(
                QtWidgets.QTreeWidgetItem([obj.path, obj.type_name, position])
            )
        if events:
            self.event_log.appendPlainText(json.dumps(events, indent=2, default=str))
        self.viewport.update()

    def closeEvent(self, event) -> None:
        self.timer.stop()
        if self.demo is not None:
            self.demo.close()
        self.client.close()
        super().closeEvent(event)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7340)
    parser.add_argument("--demo", action="store_true", help="run the masking demo after startup")
    parser.add_argument(
        "--exit-after",
        type=float,
        default=0.0,
        help="close after this many seconds; zero waits for the window to close",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    surface_format = QtGui.QSurfaceFormat()
    surface_format.setRenderableType(QtGui.QSurfaceFormat.RenderableType.OpenGL)
    surface_format.setProfile(QtGui.QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    surface_format.setVersion(3, 3)
    surface_format.setSamples(4)
    QtGui.QSurfaceFormat.setDefaultFormat(surface_format)
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    try:
        window = MainWindow(args.host, args.port, run_demo=args.demo)
    except (ConnectionError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    window.show()
    if args.exit_after > 0:
        QtCore.QTimer.singleShot(round(args.exit_after * 1000), window.close)
    result = application.exec()
    if args.demo and args.exit_after > 0:
        obj = window.native_scene.objects.get(PRIM_PATH)
        translation = obj.translation if obj is not None else None
        print(f"final native translation={translation}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
