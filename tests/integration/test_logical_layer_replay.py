"""End-to-end logical layer replay driven by department policy."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from pxr import Sdf, Usd, UsdShade

from openusdconnect.adapters import MockAdapter, UsdStageAdapter
from openusdconnect.dispatcher import EventDispatcher
from openusdconnect.emitter import NoticeEmitter
from openusdconnect.receiver import ReceiverThread
from openusdconnect.sender import EventSender
from openusdconnect.server import UsdSyncServer
from openusdconnect.server.connection import ConnectionHandler, ThreadedTCPServer

_PROPERTY_PATH = "/World/Thing.userProperties:value"
_DEFAULT_LAYER_KEY = "default"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MATERIAL_ZOO = _REPO_ROOT / "tests" / "fixtures" / "layered_material_zoo"
_MATERIAL_ZOO_TEXTURE = (
    _REPO_ROOT / "assets" / "test_assets" / "MaterialXTest" / "textures" / "brass_color.jpg"
)


def _department_layer_key(department):
    return f"department:{department}"


class _RunningServer:
    def __init__(self, log_path, departments):
        self.sync = UsdSyncServer(
            log_path=str(log_path),
            department_priority=departments,
        )
        self.tcp = ThreadedTCPServer(
            ("127.0.0.1", 0),
            ConnectionHandler,
            self.sync,
            max_workers=8,
        )
        self.thread = threading.Thread(target=self.tcp.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.tcp.server_address[1]

    def close(self):
        self.tcp.shutdown()
        self.tcp.server_close()
        self.thread.join(timeout=5)
        self.sync.shutdown()
        self.sync.store.close()


class _LayeredClient:
    def __init__(self, port, client_id):
        self.stage = Usd.Stage.CreateInMemory()
        self.receiver = ReceiverThread(
            port=port,
            reconnect=False,
            client_id=client_id,
            origin=f"{client_id}-origin",
            layered_replay=True,
        )
        self.dispatcher = EventDispatcher(
            receiver=self.receiver,
            adapter=UsdStageAdapter(self.stage),
        )
        self.receiver.start()

    def close(self):
        self.receiver.stop()
        self.receiver.join(timeout=2)
        self.dispatcher.close()

    def pump_until(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.dispatcher.drain_and_apply()
            if predicate():
                return True
            time.sleep(0.01)
        self.dispatcher.drain_and_apply()
        return bool(predicate())

    def local_value(self, department):
        router = self.dispatcher.layer_router
        if router is None:
            return None
        layer_key = _department_layer_key(department) if department else _DEFAULT_LAYER_KEY
        try:
            layer = router.layer_for(layer_key)
        except (RuntimeError, ValueError):
            return None
        attr = layer.GetAttributeAtPath(_PROPERTY_PATH)
        return attr.default if attr else None

    def layer_keys(self):
        router = self.dispatcher.layer_router
        return router.layer_keys if router is not None else ()

    def layer(self, department):
        router = self.dispatcher.layer_router
        if router is None:
            raise RuntimeError("logical layer state has not been received")
        layer_key = _department_layer_key(department) if department else _DEFAULT_LAYER_KEY
        return router.layer_for(layer_key)

    def composed_value(self):
        attr = self.stage.GetAttributeAtPath(_PROPERTY_PATH)
        return attr.Get() if attr and attr.IsValid() else None


class _NativeLayeredClient:
    def __init__(self, port, client_id, department):
        self.stage = Usd.Stage.CreateInMemory()
        self.adapter = MockAdapter()
        self.receiver = ReceiverThread(
            port=port,
            reconnect=False,
            client_id=client_id,
            origin=f"{client_id}-origin",
            department=department,
            layered_replay=True,
        )
        self.dispatcher = EventDispatcher(
            receiver=self.receiver,
            adapter=self.adapter,
            mirror_stage=self.stage,
        )
        self.receiver.start()

    def close(self):
        self.receiver.stop()
        self.receiver.join(timeout=2)
        self.dispatcher.close()

    def pump_until(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.dispatcher.drain_and_apply()
            if predicate():
                return True
            time.sleep(0.01)
        self.dispatcher.drain_and_apply()
        return bool(predicate())


def _property_events(value):
    stage = Usd.Stage.CreateInMemory()
    prim = stage.DefinePrim("/World/Thing", "Xform")
    prim.CreateAttribute(
        "userProperties:value",
        Sdf.ValueTypeNames.Int,
        custom=True,
    ).Set(value)
    emitter = NoticeEmitter(stage)
    try:
        return emitter.snapshot_events()
    finally:
        emitter.cleanup()


def _property_clear_events():
    return [
        {
            "k": "set_sdf_spec_fields",
            "prim": "/World/Thing",
            "spec_path": _PROPERTY_PATH,
            "spec_kind": "attribute",
            "fields": ["default"],
            "fragment": "",
            "removed": True,
        }
    ]


def _xform_events(value):
    return [
        {"k": "ensure_prim", "prim": "/World/Thing", "typeName": "Xform"},
        {"k": "ensure_xform_ops", "prim": "/World/Thing"},
        {
            "k": "set_xform_trs",
            "prim": "/World/Thing",
            "fields": ["t"],
            "t": [value, 0.0, 0.0],
        },
    ]


def _sender(port, client_id, department):
    sender = EventSender(
        "127.0.0.1",
        port,
        client_id=client_id,
        origin=f"{client_id}-origin",
        department=department,
    )
    assert sender.connect()
    return sender


def _copy_layer(path, label):
    source = Sdf.Layer.FindOrOpen(str(path))
    assert source is not None
    layer = Sdf.Layer.CreateAnonymous(label)
    layer.TransferContent(source)
    return layer


def _snapshot_events(stage):
    emitter = NoticeEmitter(stage)
    try:
        return emitter.snapshot_events()
    finally:
        emitter.cleanup()


def _material_zoo_events():
    lookdev = _copy_layer(_MATERIAL_ZOO / "lookdev.usda", "lookdev")
    lookdev_stage = Usd.Stage.Open(lookdev)
    texture = lookdev_stage.GetAttributeAtPath("/World/Looks/Textured/Texture.inputs:file")
    texture.Set(Sdf.AssetPath(str(_MATERIAL_ZOO_TEXTURE)))
    lookdev_events = _snapshot_events(lookdev_stage)

    shot = _copy_layer(_MATERIAL_ZOO / "shot.usda", "shot")
    root = Sdf.Layer.CreateAnonymous("material-zoo-root")
    root.subLayerPaths = [shot.identifier, lookdev.identifier]
    composed = Usd.Stage.Open(root)
    composed.SetEditTarget(shot)
    shot_events = _snapshot_events(composed)
    return lookdev_events, shot_events


def test_live_late_join_compaction_reorder_and_muting(tmp_path):
    server = _RunningServer(
        tmp_path / "layered-replay.db",
        ["animation", "layout"],
    )
    client = _LayeredClient(server.port, "layered-observer")
    late = None
    animation = None
    layout = None
    fallback = None
    try:
        assert client.pump_until(lambda: client.receiver.connected)
        animation = _sender(server.port, "animator", "animation")
        layout = _sender(server.port, "layout-artist", "layout")
        fallback = _sender(server.port, "unassigned-artist", None)

        assert animation.send_events(_property_events(2))
        assert layout.send_events(_property_events(1))
        assert fallback.send_events(_property_events(0))
        assert client.pump_until(
            lambda: (
                client.local_value("animation") == 2
                and client.local_value("layout") == 1
                and client.local_value(None) == 0
                and client.composed_value() == 2
            )
        )

        assert animation.send_events(_property_clear_events())
        assert client.pump_until(
            lambda: client.local_value("animation") is None and client.composed_value() == 1
        )
        assert animation.send_events(_property_events(2))
        assert client.pump_until(
            lambda: client.local_value("animation") == 2 and client.composed_value() == 2
        )

        server.sync.set_department_priority(["layout", "animation"])
        assert client.pump_until(lambda: client.composed_value() == 1)

        assert server.sync.mute_layer("layout")
        assert client.pump_until(lambda: client.composed_value() == 2)

        assert layout.send_events(_property_events(3))
        assert client.pump_until(
            lambda: client.local_value("layout") == 3 and client.composed_value() == 2
        )

        assert server.sync.unmute_layer("layout")
        assert client.pump_until(lambda: client.composed_value() == 3)

        late = _LayeredClient(server.port, "late-layered-observer")
        max_seq = server.sync.store.get_max_seq()
        assert late.pump_until(
            lambda: (
                late.dispatcher.last_seq == max_seq
                and late.local_value("animation") == 2
                and late.local_value("layout") == 3
                and late.local_value(None) == 0
                and late.composed_value() == 3
            )
        )

        server.sync.compact_log()
        compacted_max = server.sync.store.get_max_seq()
        assert client.pump_until(
            lambda: (
                client.dispatcher.last_seq == compacted_max
                and client.local_value("animation") == 2
                and client.local_value("layout") == 3
                and client.local_value(None) == 0
                and client.composed_value() == 3
            )
        )
        assert late.pump_until(
            lambda: (
                late.dispatcher.last_seq == compacted_max
                and late.local_value("animation") == 2
                and late.local_value("layout") == 3
                and late.local_value(None) == 0
                and late.composed_value() == 3
            )
        )
    finally:
        if animation is not None:
            animation.disconnect()
        if layout is not None:
            layout.disconnect()
        if fallback is not None:
            fallback.disconnect()
        client.close()
        if late is not None:
            late.close()
        server.close()


def test_native_receiver_projects_composed_department_state(tmp_path):
    server = _RunningServer(
        tmp_path / "native-layered-replay.db",
        ["animation", "layout"],
    )
    client = _NativeLayeredClient(server.port, "layout-artist", "layout")
    animation = None
    layout = None
    try:
        assert client.pump_until(lambda: client.receiver.connected)
        animation = _sender(server.port, "animator", "animation")
        layout = _sender(server.port, "layout-artist", "layout")

        assert animation.send_events(_xform_events(2.0))
        assert layout.send_events(_xform_events(1.0))
        assert client.pump_until(
            lambda: (
                client.adapter.get_trs("/World/Thing").get("t") == pytest.approx([2.0, 0.0, 0.0])
            )
        )

        client.adapter.set_xform_trs("/World/Thing", t=[3.0, 0.0, 0.0])
        assert layout.send_events(_xform_events(3.0))
        expected_seq = len(_xform_events(2.0)) + 2 * len(_xform_events(1.0))
        assert client.pump_until(lambda: client.dispatcher.last_seq == expected_seq)
        assert client.adapter.get_trs("/World/Thing")["t"] == pytest.approx([2.0, 0.0, 0.0])

        server.sync.set_department_priority(["layout", "animation"])
        assert client.pump_until(
            lambda: (
                client.adapter.get_trs("/World/Thing").get("t") == pytest.approx([3.0, 0.0, 0.0])
            )
        )

        assert server.sync.mute_layer("layout")
        assert client.pump_until(
            lambda: (
                client.adapter.get_trs("/World/Thing").get("t") == pytest.approx([2.0, 0.0, 0.0])
            )
        )
    finally:
        if animation is not None:
            animation.disconnect()
        if layout is not None:
            layout.disconnect()
        client.close()
        server.close()


def test_fresh_layered_receiver_after_server_restart(tmp_path):
    log_path = tmp_path / "layered-restart.db"
    first = _RunningServer(log_path, ["animation", "layout"])
    animation = _sender(first.port, "animator", "animation")
    layout = _sender(first.port, "layout-artist", "layout")
    fallback = _sender(first.port, "unassigned-artist", None)
    try:
        animation_events = _property_events(8)
        layout_events = _property_events(5)
        fallback_events = _property_events(3)
        assert animation.send_events(animation_events)
        assert layout.send_events(layout_events)
        assert fallback.send_events(fallback_events)
        expected_records = len(animation_events) + len(layout_events) + len(fallback_events)
        deadline = time.monotonic() + 5
        while first.sync.store.get_max_seq() < expected_records and time.monotonic() < deadline:
            time.sleep(0.01)
        assert first.sync.store.get_max_seq() == expected_records
    finally:
        animation.disconnect()
        layout.disconnect()
        fallback.disconnect()
        first.close()

    second = _RunningServer(log_path, ["animation", "layout"])
    receiver = _LayeredClient(second.port, "post-restart-observer")
    try:
        max_seq = second.sync.store.get_max_seq()
        assert receiver.pump_until(
            lambda: (
                receiver.dispatcher.last_seq == max_seq
                and receiver.local_value("animation") == 8
                and receiver.local_value("layout") == 5
                and receiver.local_value(None) == 3
                and receiver.composed_value() == 8
            )
        )
    finally:
        receiver.close()
        second.close()


def test_department_policy_advertises_logical_layer_before_its_events(tmp_path):
    server = _RunningServer(
        tmp_path / "layered-unlisted.db",
        ["animation"],
    )
    receiver = _LayeredClient(server.port, "layered-observer")
    sender = None
    try:
        assert receiver.pump_until(lambda: receiver.receiver.connected)
        sender = _sender(server.port, "fx-artist", "fx")
        assert sender.send_events(_property_events(4))
        assert receiver.pump_until(
            lambda: (
                receiver.layer_keys()
                == (
                    _department_layer_key("fx"),
                    _DEFAULT_LAYER_KEY,
                )
                and receiver.local_value("fx") == 4
                and receiver.composed_value() == 4
            )
        )
    finally:
        if sender is not None:
            sender.disconnect()
        receiver.close()
        server.close()


def test_layered_material_zoo_preserves_graphs_and_overrides(tmp_path):
    lookdev_events, shot_events = _material_zoo_events()
    assert lookdev_events
    assert shot_events
    assert not [
        event for event in shot_events if event.get("prim", "").startswith("/World/Looks/Textured")
    ]

    server = _RunningServer(
        tmp_path / "layered-material-zoo.db",
        ["shot", "lookdev"],
    )
    receiver = _LayeredClient(server.port, "material-zoo-observer")
    lookdev = None
    shot = None
    try:
        assert receiver.pump_until(lambda: receiver.receiver.connected)
        lookdev = _sender(server.port, "lookdev-artist", "lookdev")
        shot = _sender(server.port, "shot-artist", "shot")
        assert lookdev.send_events(lookdev_events)
        assert shot.send_events(shot_events)

        expected_seq = len(lookdev_events) + len(shot_events)
        roughness_path = "/World/Looks/Metal/Surface.inputs:roughness"
        assert receiver.pump_until(
            lambda: (
                receiver.dispatcher.last_seq == expected_seq
                and receiver.stage.GetAttributeAtPath(roughness_path).Get() == pytest.approx(0.08)
            )
        )

        lookdev_layer = receiver.layer("lookdev")
        shot_layer = receiver.layer("shot")
        assert lookdev_layer.GetAttributeAtPath(roughness_path).default == pytest.approx(0.28)
        assert shot_layer.GetAttributeAtPath(roughness_path).default == pytest.approx(0.08)

        matte_roughness = lookdev_layer.GetAttributeAtPath(
            "/World/Looks/Matte/Surface.inputs:roughness"
        )
        assert matte_roughness.documentation == "Baseline matte roughness"
        assert dict(matte_roughness.customData) == {"owner": "lookdev"}

        stage = receiver.stage
        matte_color = stage.GetAttributeAtPath(
            "/World/Looks/Matte/Surface.inputs:diffuseColor"
        ).Get()
        mtlx_color = stage.GetAttributeAtPath(
            "/World/Looks/MaterialX/Surface.inputs:base_color"
        ).Get()
        assert tuple(matte_color) == pytest.approx((0.06, 0.48, 0.16))
        assert tuple(mtlx_color) == pytest.approx((0.55, 0.08, 0.72))

        texture_shader = UsdShade.Shader(stage.GetPrimAtPath("/World/Looks/Textured/Texture"))
        assert texture_shader.GetInput("file").Get().path == str(_MATERIAL_ZOO_TEXTURE)
        texture_sources, _ = texture_shader.GetInput("st").GetConnectedSources()
        assert len(texture_sources) == 1
        assert str(texture_sources[0].source.GetPath()) == ("/World/Looks/Textured/Primvar")
        assert texture_sources[0].sourceName == "result"

        surface = UsdShade.Shader(stage.GetPrimAtPath("/World/Looks/Textured/Surface"))
        color_sources, _ = surface.GetInput("diffuseColor").GetConnectedSources()
        assert len(color_sources) == 1
        assert str(color_sources[0].source.GetPath()) == ("/World/Looks/Textured/Texture")
        assert color_sources[0].sourceName == "rgb"

        material = UsdShade.Material(stage.GetPrimAtPath("/World/Looks/MaterialX"))
        terminal_sources, _ = material.GetSurfaceOutput("mtlx").GetConnectedSources()
        assert len(terminal_sources) == 1
        assert str(terminal_sources[0].source.GetPath()) == ("/World/Looks/MaterialX/Surface")

        bound, _ = UsdShade.MaterialBindingAPI(
            stage.GetPrimAtPath("/World/TexturedBall")
        ).ComputeBoundMaterial()
        assert str(bound.GetPath()) == "/World/Looks/Textured"

        labels = stage.GetAttributeAtPath("/World/Looks/TransportProbe.inputs:labels").Get()
        assert list(labels) == ["base", "coat"]
        targets = stage.GetRelationshipAtPath(
            "/World/Looks/TransportProbe.userProperties:source"
        ).GetTargets()
        assert targets == [Sdf.Path("/World/Looks/Textured/Texture")]

        assert server.sync.mute_layer("shot")
        assert receiver.pump_until(
            lambda: receiver.stage.GetAttributeAtPath(roughness_path).Get() == pytest.approx(0.28)
        )
        assert server.sync.unmute_layer("shot")
        assert receiver.pump_until(
            lambda: receiver.stage.GetAttributeAtPath(roughness_path).Get() == pytest.approx(0.08)
        )
    finally:
        if lookdev is not None:
            lookdev.disconnect()
        if shot is not None:
            shot.disconnect()
        receiver.close()
        server.close()
