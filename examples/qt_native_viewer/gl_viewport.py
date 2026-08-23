"""Interactive OpenGL viewport for the adapter-owned example scene."""

from __future__ import annotations

import math
from array import array
from dataclasses import dataclass

from PySide6 import QtCore, QtGui
from PySide6.QtOpenGL import (
    QOpenGLBuffer,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLVertexArrayObject,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from examples.qt_native_viewer.native_scene import NativeObject, NativeScene

_GL_COLOR_BUFFER_BIT = 0x00004000
_GL_DEPTH_BUFFER_BIT = 0x00000100
_GL_DEPTH_TEST = 0x0B71
_GL_CULL_FACE = 0x0B44
_GL_MULTISAMPLE = 0x809D
_GL_FLOAT = 0x1406
_GL_LINES = 0x0001
_GL_TRIANGLES = 0x0004

_VERTEX_SHADER = """
#version 330 core
layout(location = 0) in vec3 position;
layout(location = 1) in vec3 normal;

uniform mat4 model;
uniform mat4 mvp;

out vec3 world_normal;

void main()
{
    gl_Position = mvp * vec4(position, 1.0);
    world_normal = mat3(model) * normal;
}
"""

_FRAGMENT_SHADER = """
#version 330 core
in vec3 world_normal;

uniform vec3 object_color;
uniform int use_lighting;

out vec4 fragment_color;

void main()
{
    float intensity = 1.0;
    if (use_lighting != 0) {
        vec3 light_direction = normalize(vec3(-0.35, 0.8, 0.55));
        intensity = 0.24 + 0.76 * max(dot(normalize(world_normal), light_direction), 0.0);
    }
    fragment_color = vec4(object_color * intensity, 1.0);
}
"""


@dataclass(slots=True)
class _Mesh:
    buffer: QOpenGLBuffer
    vertex_array: QOpenGLVertexArrayObject
    vertex_count: int
    primitive: int


def _append_vertex(values: array, position, normal) -> None:
    values.extend((*position, *normal))


def _cube_vertices() -> array:
    values = array("f")
    faces = (
        ((1, 0, 0), ((0.5, -0.5, -0.5), (0.5, -0.5, 0.5), (0.5, 0.5, 0.5), (0.5, 0.5, -0.5))),
        ((-1, 0, 0), ((-0.5, -0.5, 0.5), (-0.5, -0.5, -0.5), (-0.5, 0.5, -0.5), (-0.5, 0.5, 0.5))),
        ((0, 1, 0), ((-0.5, 0.5, -0.5), (0.5, 0.5, -0.5), (0.5, 0.5, 0.5), (-0.5, 0.5, 0.5))),
        ((0, -1, 0), ((-0.5, -0.5, 0.5), (0.5, -0.5, 0.5), (0.5, -0.5, -0.5), (-0.5, -0.5, -0.5))),
        ((0, 0, 1), ((0.5, -0.5, 0.5), (-0.5, -0.5, 0.5), (-0.5, 0.5, 0.5), (0.5, 0.5, 0.5))),
        ((0, 0, -1), ((-0.5, -0.5, -0.5), (0.5, -0.5, -0.5), (0.5, 0.5, -0.5), (-0.5, 0.5, -0.5))),
    )
    for normal, corners in faces:
        for index in (0, 1, 2, 0, 2, 3):
            _append_vertex(values, corners[index], normal)
    return values


def _sphere_vertices(latitude_segments: int = 18, longitude_segments: int = 28) -> array:
    values = array("f")

    def point(latitude: int, longitude: int) -> tuple[float, float, float]:
        theta = math.pi * latitude / latitude_segments
        phi = 2.0 * math.pi * longitude / longitude_segments
        sin_theta = math.sin(theta)
        return (
            sin_theta * math.cos(phi),
            math.cos(theta),
            sin_theta * math.sin(phi),
        )

    for latitude in range(latitude_segments):
        for longitude in range(longitude_segments):
            corners = (
                point(latitude, longitude),
                point(latitude + 1, longitude),
                point(latitude + 1, longitude + 1),
                point(latitude, longitude + 1),
            )
            for index in (0, 1, 2, 0, 2, 3):
                vertex = corners[index]
                _append_vertex(values, vertex, vertex)
    return values


def _grid_vertices(extent: int = 12) -> array:
    values = array("f")
    normal = (0.0, 1.0, 0.0)
    for step in range(-extent, extent + 1):
        _append_vertex(values, (-extent, 0.0, step), normal)
        _append_vertex(values, (extent, 0.0, step), normal)
        _append_vertex(values, (step, 0.0, -extent), normal)
        _append_vertex(values, (step, 0.0, extent), normal)
    return values


def _axis_vertices() -> array:
    values = array("f")
    normal = (0.0, 1.0, 0.0)
    for endpoint in ((3.0, 0.0, 0.0), (0.0, 3.0, 0.0), (0.0, 0.0, 3.0)):
        _append_vertex(values, (0.0, 0.0, 0.0), normal)
        _append_vertex(values, endpoint, normal)
    return values


class NativeViewport(QOpenGLWidget):
    """Perspective renderer driven only by :class:`NativeScene`."""

    def __init__(self, scene: NativeScene, parent=None):
        super().__init__(parent)
        self.scene = scene
        self.setMinimumSize(560, 440)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self._yaw = 42.0
        self._pitch = 24.0
        self._distance = 18.0
        self._target = QtGui.QVector3D(0.0, 0.8, 0.0)
        self._last_pointer: QtCore.QPointF | None = None
        self._shader: QOpenGLShaderProgram | None = None
        self._meshes: dict[str, _Mesh] = {}

    def initializeGL(self) -> None:
        functions = self.context().functions()
        functions.glEnable(_GL_DEPTH_TEST)
        functions.glEnable(_GL_CULL_FACE)
        functions.glEnable(_GL_MULTISAMPLE)

        shader = QOpenGLShaderProgram(self)
        if not shader.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, _VERTEX_SHADER):
            raise RuntimeError(f"vertex shader compilation failed: {shader.log()}")
        if not shader.addShaderFromSourceCode(
            QOpenGLShader.ShaderTypeBit.Fragment, _FRAGMENT_SHADER
        ):
            raise RuntimeError(f"fragment shader compilation failed: {shader.log()}")
        if not shader.link():
            raise RuntimeError(f"shader link failed: {shader.log()}")
        self._shader = shader
        self._meshes = {
            "cube": self._create_mesh(_cube_vertices(), _GL_TRIANGLES),
            "sphere": self._create_mesh(_sphere_vertices(), _GL_TRIANGLES),
            "grid": self._create_mesh(_grid_vertices(), _GL_LINES),
            "axes": self._create_mesh(_axis_vertices(), _GL_LINES),
        }

    def _create_mesh(self, values: array, primitive: int) -> _Mesh:
        shader = self._shader
        if shader is None:
            raise RuntimeError("OpenGL shader is not initialized")
        vertex_array = QOpenGLVertexArrayObject(self)
        if not vertex_array.create():
            raise RuntimeError("could not create an OpenGL vertex array")
        buffer = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        if not buffer.create():
            raise RuntimeError("could not create an OpenGL vertex buffer")

        vertex_array.bind()
        buffer.bind()
        buffer.allocate(values.tobytes(), len(values) * values.itemsize)
        shader.bind()
        shader.enableAttributeArray(0)
        shader.setAttributeBuffer(0, _GL_FLOAT, 0, 3, 6 * values.itemsize)
        shader.enableAttributeArray(1)
        shader.setAttributeBuffer(1, _GL_FLOAT, 3 * values.itemsize, 3, 6 * values.itemsize)
        shader.release()
        buffer.release()
        vertex_array.release()
        return _Mesh(buffer, vertex_array, len(values) // 6, primitive)

    def resizeGL(self, width: int, height: int) -> None:
        self.context().functions().glViewport(0, 0, width, max(height, 1))

    def paintGL(self) -> None:
        functions = self.context().functions()
        background = self.palette().color(QtGui.QPalette.ColorRole.Base)
        red, green, blue, alpha = background.getRgbF()
        functions.glClearColor(red, green, blue, alpha)
        functions.glClear(_GL_COLOR_BUFFER_BIT | _GL_DEPTH_BUFFER_BIT)
        shader = self._shader
        if shader is None:
            return

        projection = QtGui.QMatrix4x4()
        projection.perspective(45.0, self.width() / max(self.height(), 1), 0.1, 500.0)
        view = QtGui.QMatrix4x4()
        view.lookAt(self._eye_position(), self._target, QtGui.QVector3D(0.0, 1.0, 0.0))

        shader.bind()
        identity = QtGui.QMatrix4x4()
        grid_color = self.palette().color(QtGui.QPalette.ColorRole.Mid)
        self._draw_mesh(
            self._meshes["grid"],
            identity,
            projection,
            view,
            grid_color,
            use_lighting=False,
        )
        axis_colors = (
            QtGui.QColor(214, 72, 72),
            QtGui.QColor(75, 190, 105),
            QtGui.QColor(72, 125, 224),
        )
        axis_mesh = self._meshes["axes"]
        axis_mesh.vertex_array.bind()
        shader.setUniformValue("model", identity)
        shader.setUniformValue("mvp", projection * view * identity)
        shader.setUniformValue("use_lighting", 0)
        for index, color in enumerate(axis_colors):
            shader.setUniformValue("object_color", self._vector_color(color))
            functions.glDrawArrays(_GL_LINES, index * 2, 2)
        axis_mesh.vertex_array.release()

        for obj in sorted(self.scene.objects.values(), key=lambda item: item.path):
            if obj.path == "/World" or not obj.visible or not obj.active:
                continue
            model = self._model_matrix(obj)
            mesh = self._meshes["sphere" if obj.type_name in {"Sphere", "Capsule"} else "cube"]
            self._draw_mesh(
                mesh,
                model,
                projection,
                view,
                self._object_color(obj),
                use_lighting=True,
            )
        shader.release()

    def _draw_mesh(
        self,
        mesh: _Mesh,
        model: QtGui.QMatrix4x4,
        projection: QtGui.QMatrix4x4,
        view: QtGui.QMatrix4x4,
        color: QtGui.QColor,
        *,
        use_lighting: bool,
    ) -> None:
        shader = self._shader
        if shader is None:
            return
        shader.setUniformValue("model", model)
        shader.setUniformValue("mvp", projection * view * model)
        shader.setUniformValue("object_color", self._vector_color(color))
        shader.setUniformValue("use_lighting", int(use_lighting))
        mesh.vertex_array.bind()
        self.context().functions().glDrawArrays(mesh.primitive, 0, mesh.vertex_count)
        mesh.vertex_array.release()

    @staticmethod
    def _vector_color(color: QtGui.QColor) -> QtGui.QVector3D:
        red, green, blue, _alpha = color.getRgbF()
        return QtGui.QVector3D(red, green, blue)

    @staticmethod
    def _object_color(obj: NativeObject) -> QtGui.QColor:
        colors = obj.attributes.get("displayColor") or []
        if colors and len(colors[0]) >= 3:
            rgb = [max(0.0, min(float(component), 1.0)) for component in colors[0][:3]]
            return QtGui.QColor.fromRgbF(*rgb)
        return QtGui.QColor(65, 145, 235)

    @staticmethod
    def _model_matrix(obj: NativeObject) -> QtGui.QMatrix4x4:
        model = QtGui.QMatrix4x4()
        model.translate(*(float(value) for value in obj.translation[:3]))
        if len(obj.rotation) == 4:
            w, x, y, z = (float(value) for value in obj.rotation)
            model.rotate(QtGui.QQuaternion(w, x, y, z))
        scale = [float(value) for value in obj.scale[:3]]
        if obj.type_name in {"Sphere", "Capsule"}:
            radius = float(obj.attributes.get("radius", 0.75))
            scale = [component * radius for component in scale]
        else:
            size = float(obj.attributes.get("size", 1.5))
            scale = [component * size for component in scale]
        model.scale(*scale)
        return model

    def _eye_position(self) -> QtGui.QVector3D:
        yaw = math.radians(self._yaw)
        pitch = math.radians(self._pitch)
        direction = QtGui.QVector3D(
            math.cos(pitch) * math.sin(yaw),
            math.sin(pitch),
            math.cos(pitch) * math.cos(yaw),
        )
        return self._target + direction * self._distance

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        self._last_pointer = event.position()
        self.setFocus()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._last_pointer is None:
            return
        delta = event.position() - self._last_pointer
        self._last_pointer = event.position()
        if event.buttons() & QtCore.Qt.MouseButton.LeftButton:
            self._yaw -= delta.x() * 0.45
            self._pitch = max(-85.0, min(85.0, self._pitch + delta.y() * 0.35))
        elif event.buttons() & (
            QtCore.Qt.MouseButton.MiddleButton | QtCore.Qt.MouseButton.RightButton
        ):
            yaw = math.radians(self._yaw)
            right = QtGui.QVector3D(math.cos(yaw), 0.0, -math.sin(yaw))
            up = QtGui.QVector3D(0.0, 1.0, 0.0)
            scale = self._distance * 0.0018
            self._target += right * (-delta.x() * scale) + up * (delta.y() * scale)
        self.update()

    def mouseReleaseEvent(self, _event: QtGui.QMouseEvent) -> None:
        self._last_pointer = None

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        steps = event.angleDelta().y() / 120.0
        self._distance = max(2.0, min(120.0, self._distance * math.pow(0.86, steps)))
        self.update()

    def mouseDoubleClickEvent(self, _event: QtGui.QMouseEvent) -> None:
        self._yaw = 42.0
        self._pitch = 24.0
        self._distance = 18.0
        self._target = QtGui.QVector3D(0.0, 0.8, 0.0)
        self.update()


__all__ = ["NativeViewport"]
