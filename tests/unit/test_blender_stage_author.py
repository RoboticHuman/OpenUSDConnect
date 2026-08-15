"""Tests for BlenderStageAuthor + NoticeEmitter integration.

Mocks bpy to test auto-track, depsgraph-to-events flow, partial diff,
deletion detection, and feedback guard using the new architecture.
"""

import os
import sys
import tempfile
import types
from unittest.mock import MagicMock

import pytest
from pxr import Sdf, UsdGeom


# ---------------------------------------------------------------------------
# Mock bpy so capture.py can be imported outside Blender
# ---------------------------------------------------------------------------
class _BlenderObjectList(list):
    """list subclass that supports .get(name) lookup like bpy.data.objects."""

    def get(self, name):
        for obj in self:
            if getattr(obj, "name", None) == name:
                return obj
        return None


def _install_bpy_mock():
    """Install a minimal bpy mock into sys.modules."""
    if "bpy" in sys.modules:
        return sys.modules["bpy"]

    bpy = types.ModuleType("bpy")
    bpy.types = types.ModuleType("bpy.types")
    bpy.props = types.ModuleType("bpy.props")
    bpy.app = types.ModuleType("bpy.app")
    bpy.app.handlers = types.ModuleType("bpy.app.handlers")
    bpy.app.timers = types.ModuleType("bpy.app.timers")
    bpy.ops = types.ModuleType("bpy.ops")
    bpy.data = types.ModuleType("bpy.data")
    bpy.context = MagicMock()

    bpy.types.Object = type("Object", (), {})
    bpy.types.Scene = type("Scene", (), {})
    bpy.types.USDHook = type("USDHook", (), {"bl_idname": "", "bl_label": ""})
    bpy.types.Operator = type(
        "Operator",
        (),
        {"bl_idname": "", "bl_label": "", "bl_description": ""},
    )
    bpy.types.Panel = type(
        "Panel",
        (),
        {
            "bl_idname": "",
            "bl_label": "",
            "bl_space_type": "",
            "bl_region_type": "",
            "bl_category": "",
        },
    )

    bpy.props.StringProperty = lambda **kw: None
    bpy.props.IntProperty = lambda **kw: None
    bpy.props.FloatProperty = lambda **kw: None
    bpy.props.BoolProperty = lambda **kw: None

    bpy.app.handlers.depsgraph_update_post = []
    bpy.app.timers.register = lambda fn, **kw: fn()

    bpy.utils = types.ModuleType("bpy.utils")
    bpy.utils.register_class = lambda cls: None
    bpy.utils.unregister_class = lambda cls: None

    bpy.path = types.ModuleType("bpy.path")
    bpy.path.abspath = lambda p: p

    bpy.data.objects = _BlenderObjectList()

    bpy.ops.object = MagicMock()
    bpy.ops.mesh = MagicMock()
    bpy.ops.wm = MagicMock()

    sys.modules["bpy"] = bpy
    sys.modules["bpy.types"] = bpy.types
    sys.modules["bpy.props"] = bpy.props
    sys.modules["bpy.app"] = bpy.app
    sys.modules["bpy.app.handlers"] = bpy.app.handlers
    sys.modules["bpy.app.timers"] = bpy.app.timers
    sys.modules["bpy.utils"] = bpy.utils
    sys.modules["bpy.ops"] = bpy.ops
    sys.modules["bpy.data"] = bpy.data

    return bpy


_bpy_mock = _install_bpy_mock()

from openusdconnect import emitter as _core_emitter_before_blender_import


# ---------------------------------------------------------------------------
# Mock Blender objects
# ---------------------------------------------------------------------------
class _Vec3:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class _Quat:
    def __init__(self, w, x, y, z):
        self.w, self.x, self.y, self.z = w, x, y, z


class _Matrix:
    """Minimal mock of mathutils.Matrix with row iteration for change detection."""

    def __init__(self, loc=(0, 0, 0), rot=(1, 0, 0, 0), scl=(1, 1, 1)):
        self._loc = loc
        self._rot = rot
        self._scl = scl
        # 4x4 rows encoding loc/rot/scl for unique tuple fingerprint
        self._rows = [
            [scl[0], rot[1], rot[2], 0],
            [rot[1], scl[1], rot[3], 0],
            [rot[2], rot[3], scl[2], 0],
            [loc[0], loc[1], loc[2], rot[0]],
        ]

    def __iter__(self):
        return iter(self._rows)

    def copy(self):
        return _Matrix(self._loc, self._rot, self._scl)

    def decompose(self):
        return (
            _Vec3(*self._loc),
            _Quat(*self._rot),
            _Vec3(*self._scl),
        )

    def inverted_safe(self):
        return _Matrix()

    def __matmul__(self, other):
        return other.copy()


class MockBlenderObject(sys.modules["bpy"].types.Object):
    """Simulates a Blender object with custom properties and transform."""

    def __init__(
        self,
        name,
        loc=(0, 0, 0),
        rot=(1, 0, 0, 0),
        scl=(1, 1, 1),
        prim_path=None,
        type_name=None,
        obj_type="MESH",
    ):
        self._name = name
        self.type = obj_type
        self.parent = None
        self.matrix_world = _Matrix(loc, rot, scl)
        self.data = MagicMock()
        self.data.name = name + "_mesh"
        self._props = {}
        self._deleted = False
        if prim_path:
            self._props["usd_prim_path"] = prim_path
        if type_name:
            self._props["usd_type_name"] = type_name

    @property
    def name(self):
        if self._deleted:
            raise ReferenceError("StructRNA of type Object has been removed")
        return self._name

    @name.setter
    def name(self, value):
        self._name = value

    def get(self, key, default=None):
        return self._props.get(key, default)

    def __contains__(self, key):
        return key in self._props

    def __getitem__(self, key):
        return self._props[key]

    def __setitem__(self, key, value):
        self._props[key] = value


class MockDepsgraphUpdate:
    """Simulates a single depsgraph update entry."""

    def __init__(self, obj):
        self.id = obj


# ---------------------------------------------------------------------------
# Import the module under test (after bpy mock is installed)
# ---------------------------------------------------------------------------
from integrations.blender.blender_adapter import _PROP_USD_IMPORTED, BlenderAdapter
from integrations.blender.capture import BlenderStageAuthor
from openusdconnect.emitter import NoticeEmitter
from openusdconnect.protocol_constants import (
    K_DEACTIVATE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_SET_XFORM_TRS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_temp_usda(content='#usda 1.0\ndef Xform "World" {}\n'):
    """Create a temp .usda file and return its path."""
    f = tempfile.NamedTemporaryFile(suffix=".usda", delete=False, mode="w")
    f.write(content)
    f.close()
    return f.name


def _make_author_and_emitter(auto_track=False):
    """Create a BlenderStageAuthor + NoticeEmitter pair for testing."""
    tmp = _make_temp_usda()
    author = BlenderStageAuthor(base_usd_path=tmp)
    author.enabled = True
    author.auto_track = auto_track
    # Seed used paths from mock bpy.data.objects
    author._used_prim_paths = {
        obj.get("usd_prim_path")
        for obj in sys.modules["bpy"].data.objects
        if obj.get("usd_prim_path")
    }
    emitter = NoticeEmitter(author.stage)
    return author, emitter


def _get_events(author, emitter, updates):
    """Run depsgraph update and collect events."""
    author.on_depsgraph_update(updates)
    return emitter.build_events_for_dirty()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_source_import_keeps_core_module_identity():
    import importlib

    import integrations.blender as blender_package

    emitter_module = sys.modules["openusdconnect.emitter"]
    assert emitter_module is _core_emitter_before_blender_import

    importlib.reload(blender_package)
    assert sys.modules["openusdconnect.emitter"] is emitter_module


class TestAutoTrack:
    """Test auto-track assigns prim paths based on parent hierarchy."""

    def _make_world_parent(self):
        return MockBlenderObject("World", prim_path="/World", obj_type="EMPTY")

    def test_auto_track_assigns_path_from_parent(self):
        world = self._make_world_parent()
        obj = MockBlenderObject("Cube", loc=(1, 2, 3))
        obj.parent = world
        sys.modules["bpy"].data.objects = _BlenderObjectList([world, obj])
        author, emitter = _make_author_and_emitter(auto_track=True)

        events = _get_events(author, emitter, [MockDepsgraphUpdate(obj)])

        ensures = [e for e in events if e["k"] == K_ENSURE_PRIM]
        child_ensures = [e for e in ensures if e["prim"] == "/World/Cube"]
        assert len(child_ensures) == 1
        assert obj["usd_prim_path"] == "/World/Cube"

    def test_auto_track_skips_unparented_object(self):
        obj = MockBlenderObject("Cube", loc=(1, 2, 3))
        sys.modules["bpy"].data.objects = _BlenderObjectList([obj])
        author, emitter = _make_author_and_emitter(auto_track=True)

        events = _get_events(author, emitter, [MockDepsgraphUpdate(obj)])
        assert len(events) == 0

    def test_auto_track_skips_parent_without_prim_path(self):
        parent = MockBlenderObject("SomeEmpty", obj_type="EMPTY")
        obj = MockBlenderObject("Cube", loc=(1, 2, 3))
        obj.parent = parent
        sys.modules["bpy"].data.objects = _BlenderObjectList([parent, obj])
        author, emitter = _make_author_and_emitter(auto_track=True)

        events = _get_events(author, emitter, [MockDepsgraphUpdate(obj)])
        assert len(events) == 0

    def test_auto_track_infers_type(self):
        world = self._make_world_parent()
        obj = MockBlenderObject("Sphere", loc=(1, 2, 3))
        obj.data.name = "Sphere_mesh"
        obj.parent = world
        sys.modules["bpy"].data.objects = _BlenderObjectList([world, obj])
        author, emitter = _make_author_and_emitter(auto_track=True)

        events = _get_events(author, emitter, [MockDepsgraphUpdate(obj)])

        ensures = [e for e in events if e["k"] == K_ENSURE_PRIM]
        child_ensure = [e for e in ensures if e["prim"] == "/World/Sphere"]
        assert len(child_ensure) == 1
        assert child_ensure[0]["typeName"] == "Sphere"

    def test_auto_track_nested_hierarchy(self):
        world = MockBlenderObject("World", prim_path="/World", obj_type="EMPTY")
        group = MockBlenderObject("Group", prim_path="/World/Group", obj_type="EMPTY")
        group.parent = world
        obj = MockBlenderObject("NewCube", loc=(1, 2, 3))
        obj.parent = group
        sys.modules["bpy"].data.objects = _BlenderObjectList([world, group, obj])
        author, emitter = _make_author_and_emitter(auto_track=True)

        events = _get_events(author, emitter, [MockDepsgraphUpdate(obj)])

        ensures = [e for e in events if e["k"] == K_ENSURE_PRIM]
        ensure_paths = [e["prim"] for e in ensures]
        # /World is supplied by the shared base and remains untouched locally.
        assert "/World" not in ensure_paths
        world_spec = author.delta_layer.GetPrimAtPath("/World")
        assert world_spec.specifier == Sdf.SpecifierOver
        assert not world_spec.properties
        assert "/World/Group" in ensure_paths
        assert "/World/Group/NewCube" in ensure_paths
        # Parent before child
        assert ensure_paths.index("/World/Group") < ensure_paths.index("/World/Group/NewCube")
        assert obj["usd_prim_path"] == "/World/Group/NewCube"

    def test_remote_imported_descendant_authors_overrides(self):
        world = MockBlenderObject("World", prim_path="/World", obj_type="EMPTY")
        asset = MockBlenderObject(
            "Asset",
            prim_path="/World/Asset",
            type_name="Xform",
            obj_type="EMPTY",
        )
        geom = MockBlenderObject(
            "Geom",
            prim_path="/World/Asset/Geom",
            type_name="Xform",
            obj_type="EMPTY",
        )
        mesh = MockBlenderObject(
            "Mesh",
            loc=(1, 0, 0),
            prim_path="/World/Asset/Geom/Mesh",
            type_name="Mesh",
        )
        for obj in (asset, geom, mesh):
            obj[_PROP_USD_IMPORTED] = True
        asset.parent = world
        geom.parent = asset
        mesh.parent = geom
        sys.modules["bpy"].data.objects = _BlenderObjectList([world, asset, geom, mesh])
        author, emitter = _make_author_and_emitter()
        author._prim_refs.update(
            {
                "/World/Asset": asset,
                "/World/Asset/Geom": geom,
                "/World/Asset/Geom/Mesh": mesh,
            }
        )

        events = _get_events(author, emitter, [MockDepsgraphUpdate(mesh)])

        for path in ("/World/Asset", "/World/Asset/Geom", "/World/Asset/Geom/Mesh"):
            spec = author.delta_layer.GetPrimAtPath(path)
            assert spec is not None
            assert spec.specifier == Sdf.SpecifierOver
            assert not spec.typeName
        for path in ("/World/Asset", "/World/Asset/Geom"):
            assert author.delta_layer.GetAttributeAtPath(f"{path}.xformOpOrder") is None
        assert not [event for event in events if event["k"] == K_ENSURE_PRIM]
        trs = [
            event
            for event in events
            if event["k"] == K_SET_XFORM_TRS and event["prim"] == "/World/Asset/Geom/Mesh"
        ]
        assert len(trs) == 1
        assert trs[0]["t"] == [1.0, 0.0, 0.0]

    def test_seeded_remote_parent_is_authored_before_edited_child(self):
        """A receive-side cache entry does not imply a local USD spec exists."""
        world = MockBlenderObject("World", prim_path="/World", obj_type="EMPTY")
        parent = MockBlenderObject(
            "RemoteParent",
            prim_path="/World/RemoteParent",
            type_name="Xform",
            obj_type="EMPTY",
        )
        child = MockBlenderObject(
            "RemoteCube",
            loc=(1, 0, 0),
            prim_path="/World/RemoteParent/Cube",
            type_name="Cube",
        )
        parent.parent = world
        child.parent = parent
        sys.modules["bpy"].data.objects = _BlenderObjectList([world, parent, child])
        author, emitter = _make_author_and_emitter()
        author._prim_refs.update(
            {
                "/World/RemoteParent": parent,
                "/World/RemoteParent/Cube": child,
            }
        )

        events = _get_events(author, emitter, [MockDepsgraphUpdate(child)])

        ensures = [event for event in events if event["k"] == K_ENSURE_PRIM]
        assert [(event["prim"], event["typeName"]) for event in ensures] == [
            ("/World/RemoteParent", "Xform"),
            ("/World/RemoteParent/Cube", "Cube"),
        ]
        assert not [
            event
            for event in events
            if event["prim"] == "/World/RemoteParent"
            and event["k"] in {K_ENSURE_XFORM_OPS, K_SET_XFORM_TRS}
        ]

    def test_auto_track_disambiguates_collision(self):
        world = self._make_world_parent()
        existing = MockBlenderObject("Geom", prim_path="/World/Cube")
        existing.parent = world
        new_cube = MockBlenderObject("Cube", loc=(1, 2, 3))
        new_cube.parent = world
        sys.modules["bpy"].data.objects = _BlenderObjectList([world, existing, new_cube])
        author, emitter = _make_author_and_emitter(auto_track=True)

        events = _get_events(author, emitter, [MockDepsgraphUpdate(new_cube)])

        ensures = [e for e in events if e["k"] == K_ENSURE_PRIM]
        child_ensures = [e for e in ensures if e["prim"] != "/World"]
        assert len(child_ensures) == 1
        assert child_ensures[0]["prim"] == "/World/Cube_1"
        assert new_cube["usd_prim_path"] == "/World/Cube_1"

    def test_auto_track_no_collision_uses_exact_name(self):
        world = self._make_world_parent()
        new_sphere = MockBlenderObject("Sphere", loc=(1, 2, 3))
        new_sphere.parent = world
        sys.modules["bpy"].data.objects = _BlenderObjectList([world, new_sphere])
        author, emitter = _make_author_and_emitter(auto_track=True)

        events = _get_events(author, emitter, [MockDepsgraphUpdate(new_sphere)])

        ensures = [e for e in events if e["k"] == K_ENSURE_PRIM]
        child_ensures = [e for e in ensures if e["prim"] == "/World/Sphere"]
        assert len(child_ensures) == 1


def test_parametric_geometry_scale_is_not_authored_as_xform_scale():
    """A receive/emit round trip must not multiply Cube sizing repeatedly."""
    obj = MockBlenderObject(
        "ChairLeg",
        scl=(0.025, 0.025, 0.425),
        prim_path="/World/ChairLeg",
        type_name="Cube",
    )
    obj["usd_geom_scale"] = (0.5, 0.5, 0.5)
    obj["usd_xform_scale"] = (0.05, 0.05, 0.85)
    sys.modules["bpy"].data.objects = _BlenderObjectList([obj])
    author, _emitter = _make_author_and_emitter()
    UsdGeom.Cube.Define(author.stage, "/World/ChairLeg")
    author._ensure_xform_ops("/World/ChairLeg")

    author._author_xform("/World/ChairLeg", obj)

    scale_op = next(
        op
        for op in UsdGeom.Xformable(
            author.stage.GetPrimAtPath("/World/ChairLeg")
        ).GetOrderedXformOps()
        if op.GetOpType() == UsdGeom.XformOp.TypeScale
    )
    # Blender Z-up (0.05, 0.05, 0.85) converts back to USD Y-up.
    assert tuple(scale_op.Get()) == pytest.approx((0.05, 0.85, 0.05))


def test_parametric_scale_round_trip_noise_keeps_previous_usd_value():
    """Float32 display rounding must not make scale drift on every move."""
    from integrations.blender.capture import _usd_xform_scale

    obj = MockBlenderObject("ChairBackrest")
    obj["usd_geom_scale"] = (0.5, 0.5, 0.5)
    obj["usd_xform_scale"] = (0.42, 0.3, 0.04)

    authored = _usd_xform_scale(
        obj,
        (0.210000008, 0.150000006, 0.0199999996),
    )

    assert authored == [0.42, 0.3, 0.04]


class TestFirstEncounter:
    """Test event generation when an object is first seen."""

    def test_first_encounter_emits_ensure_and_trs(self):
        obj = MockBlenderObject("Cube", loc=(1, 2, 3), prim_path="/World/Cube", type_name="Cube")
        sys.modules["bpy"].data.objects = _BlenderObjectList([obj])
        author, emitter = _make_author_and_emitter()

        events = _get_events(author, emitter, [MockDepsgraphUpdate(obj)])

        kinds = [e["k"] for e in events]
        assert K_ENSURE_PRIM in kinds
        assert K_ENSURE_XFORM_OPS in kinds
        assert K_SET_XFORM_TRS in kinds


class TestPartialDiff:
    """Test TRS partial diffing via the new architecture."""

    def test_only_changed_fields_sent(self):
        obj = MockBlenderObject("Cube", loc=(1, 0, 0), prim_path="/World/Cube")
        sys.modules["bpy"].data.objects = _BlenderObjectList([obj])
        author, emitter = _make_author_and_emitter()

        # First encounter — all fields
        events1 = _get_events(author, emitter, [MockDepsgraphUpdate(obj)])
        trs1 = [e for e in events1 if e["k"] == K_SET_XFORM_TRS and e["prim"] == "/World/Cube"]
        assert len(trs1) == 1
        assert set(trs1[0]["fields"]) == {"t", "r", "s"}

        # Change only translation
        obj.matrix_world = _Matrix(loc=(5, 0, 0))
        events2 = _get_events(author, emitter, [MockDepsgraphUpdate(obj)])
        trs2 = [e for e in events2 if e["k"] == K_SET_XFORM_TRS and e["prim"] == "/World/Cube"]
        assert len(trs2) == 1
        assert trs2[0]["fields"] == ["t"]

    def test_no_trs_event_when_unchanged(self):
        obj = MockBlenderObject("Cube", loc=(1, 0, 0), prim_path="/World/Cube")
        sys.modules["bpy"].data.objects = _BlenderObjectList([obj])
        author, emitter = _make_author_and_emitter()

        _get_events(author, emitter, [MockDepsgraphUpdate(obj)])

        # Same transform — BlenderStageAuthor detects no matrix change, skips
        events = _get_events(author, emitter, [MockDepsgraphUpdate(obj)])
        trs = [e for e in events if e["k"] == K_SET_XFORM_TRS and e["prim"] == "/World/Cube"]
        assert len(trs) == 0


class TestDeletionDetection:
    """Test deletion detection via _prim_refs ReferenceError."""

    def test_deleted_object_emits_deactivate(self):
        obj = MockBlenderObject("Cube", loc=(1, 0, 0), prim_path="/World/Cube")
        sys.modules["bpy"].data.objects = _BlenderObjectList([obj])
        author, emitter = _make_author_and_emitter()

        _get_events(author, emitter, [MockDepsgraphUpdate(obj)])

        obj._deleted = True
        # Remove from scene so _detect_deletions sees it as truly gone
        sys.modules["bpy"].data.objects = _BlenderObjectList([])
        events = _get_events(author, emitter, [])

        deact = [e for e in events if e["k"] == K_DEACTIVATE_PRIM]
        assert len(deact) == 1
        assert deact[0]["prim"] == "/World/Cube"
        assert deact[0]["active"] is False

    def test_deleted_remote_object_authors_active_override(self):
        obj = MockBlenderObject("Remote", prim_path="/World/Remote")
        obj[_PROP_USD_IMPORTED] = True
        sys.modules["bpy"].data.objects = _BlenderObjectList([obj])
        author, emitter = _make_author_and_emitter()
        author._prim_refs["/World/Remote"] = obj

        obj._deleted = True
        sys.modules["bpy"].data.objects = _BlenderObjectList([])
        events = _get_events(author, emitter, [])

        spec = author.delta_layer.GetPrimAtPath("/World/Remote")
        assert spec.specifier == Sdf.SpecifierOver
        assert not spec.typeName
        assert [event for event in events if event["k"] == K_DEACTIVATE_PRIM] == [
            {"k": K_DEACTIVATE_PRIM, "prim": "/World/Remote", "active": False}
        ]


class TestFeedbackGuard:
    """Test that _applying_remote blocks event generation."""

    def test_applying_remote_blocks_events(self):
        obj = MockBlenderObject("Cube", loc=(1, 2, 3), prim_path="/World/Cube")
        sys.modules["bpy"].data.objects = _BlenderObjectList([obj])
        author, emitter = _make_author_and_emitter()
        author._applying_remote = True

        events = _get_events(author, emitter, [MockDepsgraphUpdate(obj)])
        assert events == []


class TestNonObjectUpdates:
    """Test that non-Object updates are ignored."""

    def test_non_object_updates_ignored(self):
        sys.modules["bpy"].data.objects = _BlenderObjectList()
        author, emitter = _make_author_and_emitter()

        fake_update = MockDepsgraphUpdate(MagicMock())
        events = _get_events(author, emitter, [fake_update])
        assert len(events) == 0


class TestReverseShaderAuthoring:
    """_author_shader_inputs: baseline diffing + type-preserving authoring."""

    def _make_author(self):
        sys.modules["bpy"].data.objects = _BlenderObjectList()
        author, _ = _make_author_and_emitter()
        return author

    def test_first_encounter_seeds_without_authoring(self):
        author = self._make_author()
        author._author_shader_inputs(
            "/World/Looks/M/S", {"roughness": 0.5}, shader_id="UsdPreviewSurface"
        )
        assert author._last_shader_values["/World/Looks/M/S"] == {"roughness": 0.5}
        assert not author.stage.GetPrimAtPath("/World/Looks/M/S")

    def test_existing_asset_input_type_wins(self):
        from pxr import Sdf, UsdShade

        author = self._make_author()
        prim = author.stage.DefinePrim("/World/Looks/M/Tex", "Shader")
        shader = UsdShade.Shader(prim)
        shader.CreateIdAttr("UsdUVTexture")
        shader.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath("D:/old.png"))
        author._last_shader_values["/World/Looks/M/Tex"] = {"file": "D:/old.png"}

        author._author_shader_inputs(
            "/World/Looks/M/Tex", {"file": "D:/new.png"}, shader_id="UsdUVTexture"
        )

        inp = UsdShade.Shader(author.stage.GetPrimAtPath("/World/Looks/M/Tex")).GetInput("file")
        assert str(inp.GetAttr().GetTypeName()) == "asset"
        assert inp.Get().path == "D:/new.png"

    def test_existing_vector_input_type_wins(self):
        from pxr import Gf, Sdf, UsdShade

        author = self._make_author()
        path = "/World/Looks/M/Surface"
        prim = author.stage.DefinePrim(path, "Shader")
        shader = UsdShade.Shader(prim)
        shader.CreateIdAttr("ND_standard_surface_surfaceshader")
        shader.CreateInput("subsurface_radius", Sdf.ValueTypeNames.Float3).Set(
            Gf.Vec3f(1.0, 1.0, 1.0)
        )
        author._last_shader_values[path] = {"subsurface_radius": [1.0, 1.0, 1.0]}

        author._author_shader_inputs(
            path,
            {"subsurface_radius": [1.0, 2.0, 3.0]},
            shader_id="ND_standard_surface_surfaceshader",
        )

        inp = UsdShade.Shader(author.stage.GetPrimAtPath(path)).GetInput("subsurface_radius")
        assert str(inp.GetAttr().GetTypeName()) == "float3"
        assert inp.Get() == Gf.Vec3f(1.0, 2.0, 3.0)

    def test_sdr_resolves_type_for_new_input(self):
        from pxr import UsdShade

        author = self._make_author()
        path = "/World/Looks/M/Tex"
        author._last_shader_values[path] = {"file": "D:/old.png"}

        author._author_shader_inputs(path, {"file": "D:/new.png"}, shader_id="UsdUVTexture")

        inp = UsdShade.Shader(author.stage.GetPrimAtPath(path)).GetInput("file")
        # A str would be "string" by heuristic; Sdr knows UsdUVTexture.file is asset.
        assert str(inp.GetAttr().GetTypeName()) == "asset"
        assert inp.Get().path == "D:/new.png"
        spec = author.delta_layer.GetPrimAtPath(path)
        assert spec.specifier == Sdf.SpecifierOver
        assert not spec.typeName

    def test_heuristic_types_for_unknown_shader(self):
        from pxr import UsdShade

        author = self._make_author()
        path = "/World/Looks/M/Custom"
        author._last_shader_values[path] = {"s": "a", "c": [0.0, 0.0, 0.0], "f": 1.0}

        author._author_shader_inputs(
            path, {"s": "b", "c": [1.0, 0.0, 0.0], "f": 2.0}, shader_id="MyCustomShader"
        )

        shader = UsdShade.Shader(author.stage.GetPrimAtPath(path))
        assert str(shader.GetInput("s").GetAttr().GetTypeName()) == "string"
        assert str(shader.GetInput("c").GetAttr().GetTypeName()) == "color3f"
        assert str(shader.GetInput("f").GetAttr().GetTypeName()) == "float"

    def test_unchanged_values_do_not_author(self):
        author = self._make_author()
        path = "/World/Looks/M/S"
        author._last_shader_values[path] = {"roughness": 0.5}

        author._author_shader_inputs(path, {"roughness": 0.5}, shader_id="UsdPreviewSurface")

        assert not author.stage.GetPrimAtPath(path)


class TestMapperReverseReads:
    """Reverse reads on the texture / primvar-reader mappers."""

    def _ensure_bpy_path(self):
        bpy = sys.modules["bpy"]
        if not hasattr(bpy, "path"):
            bpy.path = types.SimpleNamespace(abspath=lambda p: p)

    def test_texture_mapper_reads_image_filepath(self):
        self._ensure_bpy_path()
        from integrations.blender.shader_mapper import TextureShaderMapper

        mapper = TextureShaderMapper("UsdUVTexture", "ShaderNodeTexImage", {"file": "_image"})
        node = MagicMock()
        node.image.filepath = "D:/tex/albedo.png"
        assert mapper.read_all_inputs(node) == {"file": os.path.normpath("D:/tex/albedo.png")}

    def test_texture_mapper_skips_imageless_node(self):
        self._ensure_bpy_path()
        from integrations.blender.shader_mapper import TextureShaderMapper

        mapper = TextureShaderMapper("UsdUVTexture", "ShaderNodeTexImage", {"file": "_image"})
        node = MagicMock()
        node.image = None
        assert mapper.read_all_inputs(node) == {}
        node.image = MagicMock()
        node.image.filepath = ""
        assert mapper.read_all_inputs(node) == {}

    def test_uv_reader_mapper_reads_varname(self):
        from integrations.blender.shader_mapper import UVReaderMapper

        mapper = UVReaderMapper(
            "UsdPrimvarReader_float2", "ShaderNodeUVMap", {"varname": "_uv_map"}
        )
        node = MagicMock()
        node.uv_map = "st"
        assert mapper.read_all_inputs(node) == {"varname": "st"}
        node.uv_map = ""
        assert mapper.read_all_inputs(node) == {}

    def test_attribute_reader_mapper_reads_varname(self):
        from integrations.blender.shader_mapper import AttributeReaderMapper

        mapper = AttributeReaderMapper(
            "UsdPrimvarReader_float3", "ShaderNodeAttribute", {"varname": "_attribute_name"}
        )
        node = MagicMock()
        node.attribute_name = "displayColor"
        assert mapper.read_all_inputs(node) == {"varname": "displayColor"}


class TestBlenderAssetArcProjection:
    @staticmethod
    def _make_asset(tmp_path, name):
        from pxr import Usd

        path = tmp_path / f"{name}.usda"
        stage = Usd.Stage.CreateNew(str(path))
        stage.DefinePrim(f"/{name}", "Xform")
        stage.GetRootLayer().defaultPrim = name
        stage.GetRootLayer().Save()
        return str(path)

    def test_native_composition_roots_cover_import_and_payload_lifecycle(self):
        adapter = BlenderAdapter()

        roots = adapter.native_composition_subtree_roots(
            [
                {"k": "set_reference", "prim": "/World/Reference", "refs": []},
                {"k": "set_payload", "prim": "/World/Payload", "payloads": []},
                {"k": "load_payload", "prim": "/World/Payload"},
                {"k": "unload_payload", "prim": "/World/Unloaded"},
            ]
        )

        assert roots == {
            "/World/Reference",
            "/World/Payload",
            "/World/Unloaded",
        }

    def test_variant_owns_subtree_only_after_native_import(self, monkeypatch):
        adapter = BlenderAdapter()
        event = {
            "k": "set_variant_selections",
            "prim": "/World/Asset",
            "selections": {"model": "B"},
        }

        assert adapter.native_composition_subtree_roots([event]) == set()

        monkeypatch.setattr(adapter, "has_imported_children", lambda path: path == "/World/Asset")
        assert adapter.native_composition_subtree_roots([event]) == {"/World/Asset"}

    def test_reference_projection_uses_composed_list_op(self, tmp_path):
        from pxr import Sdf, Usd

        asset_a = self._make_asset(tmp_path, "A")
        asset_b = self._make_asset(tmp_path, "B")
        nested_asset = self._make_asset(tmp_path, "Nested")
        asset_b_stage = Usd.Stage.Open(asset_b)
        asset_b_stage.GetPrimAtPath("/B").GetReferences().AddReference(nested_asset)
        asset_b_stage.GetRootLayer().Save()
        root = Sdf.Layer.CreateAnonymous("root")
        weak = Sdf.Layer.CreateAnonymous("weak")
        root.subLayerPaths.append(weak.identifier)
        stage = Usd.Stage.Open(root)

        with Usd.EditContext(stage, weak):
            prim = stage.DefinePrim("/World", "Xform")
            prim.GetReferences().AddReference(asset_a)
            prim.GetReferences().AddReference(asset_b)
        strong_spec = Sdf.CreatePrimInLayer(root, "/World")
        strong_op = Sdf.ReferenceListOp()
        strong_op.deletedItems = [Sdf.Reference(asset_a)]
        strong_op.orderedItems = [Sdf.Reference(asset_b)]
        strong_spec.SetInfo("references", strong_op)

        adapter = BlenderAdapter(mirror_stage=stage)
        result = adapter._composed_asset_arcs(
            "/World",
            [
                {"asset_path": asset_a, "list_position": "deleted"},
                {"asset_path": asset_b, "list_position": "ordered"},
            ],
            explicit=False,
            payload=False,
        )

        assert result == [{"asset_path": asset_b, "prim_path": "/B"}]
        assert isinstance(result[0]["asset_path"], str)

    def test_unloaded_payload_is_reprojected_when_loaded(self, tmp_path):
        from pxr import Sdf, Usd

        asset = self._make_asset(tmp_path, "Payload")
        nested_asset = self._make_asset(tmp_path, "NestedPayload")
        asset_stage = Usd.Stage.Open(asset)
        asset_stage.GetPrimAtPath("/Payload").GetPayloads().AddPayload(nested_asset)
        asset_stage.GetRootLayer().Save()
        root = Sdf.Layer.CreateAnonymous("root")
        spec = Sdf.CreatePrimInLayer(root, "/World")
        spec.specifier = Sdf.SpecifierDef
        payload_op = Sdf.PayloadListOp()
        payload_op.prependedItems = [Sdf.Payload(asset)]
        spec.SetInfo("payload", payload_op)
        stage = Usd.Stage.Open(root, load=Usd.Stage.LoadNone)
        adapter = BlenderAdapter(mirror_stage=stage)
        event_entries = [{"asset_path": asset}]

        assert (
            adapter._composed_asset_arcs(
                "/World",
                event_entries,
                explicit=False,
                payload=True,
            )
            == []
        )

        assert adapter.set_payload("/World", event_entries)
        assert adapter._pending_payloads["/World"]["entries"] == event_entries

        stage.Load("/World")
        assert adapter._composed_asset_arcs(
            "/World",
            adapter._pending_payloads["/World"]["entries"],
            explicit=False,
            payload=True,
        ) == [{"asset_path": asset, "prim_path": "/Payload"}]

    def test_variant_export_does_not_modify_cached_source_layer(self, tmp_path):
        from pxr import Sdf, Usd

        asset_path = tmp_path / "variants.usda"
        source = Usd.Stage.CreateNew(str(asset_path))
        asset = source.DefinePrim("/Asset", "Xform")
        source.SetDefaultPrim(asset)
        variants = asset.GetVariantSets().AddVariantSet("model")
        for name in ("A", "B"):
            variants.AddVariant(name)
            variants.SetVariantSelection(name)
            with variants.GetVariantEditContext():
                asset.CreateAttribute("selection", Sdf.ValueTypeNames.String).Set(name)
        variants.SetVariantSelection("A")
        source.GetRootLayer().Save()
        source_text = source.GetRootLayer().ExportToString()

        adapter = BlenderAdapter()
        export_path = adapter._create_variant_stage(
            str(asset_path),
            "/Asset",
            {"model": "B"},
        )

        assert export_path is not None
        assert source.GetRootLayer().ExportToString() == source_text
        exported = Usd.Stage.Open(export_path)
        exported_asset = exported.GetPrimAtPath("/Asset")
        assert exported_asset.GetAttribute("selection").Get() == "B"


def test_receiver_mirror_has_an_independent_session_layer(monkeypatch):
    from pxr import Usd

    from integrations.blender import capture, receiver_addon

    base_path = _make_temp_usda()
    author = BlenderStageAuthor(base_usd_path=base_path)
    monkeypatch.setattr(capture._state, "author", author)
    monkeypatch.setattr(receiver_addon, "_MIRROR_STAGE", None)
    monkeypatch.setattr(receiver_addon, "_MIRROR_SOURCE", "")

    mirror = receiver_addon._ensure_mirror_stage(reset=True)

    assert mirror is not author.stage
    assert mirror.GetRootLayer() is author.stage.GetRootLayer()
    assert mirror.GetSessionLayer() is not author.stage.GetSessionLayer()
    assert mirror.GetEditTarget().GetLayer() is mirror.GetSessionLayer()
    with Usd.EditContext(author.stage, author.stage.GetSessionLayer()):
        author.stage.DefinePrim("/LocalOnly", "Xform")
    assert not mirror.GetPrimAtPath("/LocalOnly")
    mirror.DefinePrim("/ReceiverOnly", "Xform")
    assert not author.stage.GetPrimAtPath("/ReceiverOnly")


def test_blender_emitter_releases_batch_only_after_send_succeeds(monkeypatch):
    from integrations.blender import capture

    events = [{"k": "delete_prim", "prim": "/World/Deleted"}]
    emitter = MagicMock()
    emitter.prepare_events_for_send.return_value = events
    sender = MagicMock()
    sender.sock = object()
    sender.send_events.side_effect = [False, True]
    monkeypatch.setattr(capture._state, "notice_emitter", emitter)
    monkeypatch.setattr(capture._state, "sender", sender)

    capture._try_send_dirty_events()
    emitter.mark_prepared_events_sent.assert_not_called()

    sender.sock = object()
    capture._try_send_dirty_events()

    assert sender.send_events.call_count == 2
    emitter.mark_prepared_events_sent.assert_called_once_with(events)


def test_blender_timer_retries_a_prepared_batch_after_reconnect(monkeypatch):
    from integrations.blender import capture

    author = MagicMock()
    author.enabled = True
    emitter = MagicMock()
    emitter.dirty = set()
    emitter.prepared_event_count = 1
    send_pending = MagicMock()
    monkeypatch.setattr(capture._state, "author", author)
    monkeypatch.setattr(capture._state, "notice_emitter", emitter)
    monkeypatch.setattr(capture, "_try_send_dirty_events", send_pending)
    monkeypatch.setattr(capture, "_schedule_emitter_reconnect", MagicMock())

    assert capture._timer_tick() == 0.25
    send_pending.assert_called_once_with()


def test_blender_timer_schedules_emitter_reconnect(monkeypatch):
    from integrations.blender import capture

    author = MagicMock()
    author.enabled = True
    reconnect = MagicMock()
    monkeypatch.setattr(capture._state, "author", author)
    monkeypatch.setattr(capture._state, "notice_emitter", None)
    monkeypatch.setattr(capture, "_schedule_emitter_reconnect", reconnect)

    assert capture._timer_tick() == 0.25
    reconnect.assert_called_once_with()


def test_blender_reconnect_worker_preserves_current_sender(monkeypatch):
    from integrations.blender import capture

    sender = MagicMock()
    sender.connect.return_value = True
    sender.host = "127.0.0.1"
    sender.port = 7200
    monkeypatch.setattr(capture._state, "sender", sender)
    monkeypatch.setattr(capture._state, "_reconnect_generation", 7)
    monkeypatch.setattr(capture._state, "_reconnect_interval", 4.0)

    capture._emitter_reconnect_worker(sender, 7)

    sender.connect.assert_called_once_with(
        timeout=capture.EMITTER_RECONNECT_TIMEOUT_SECONDS,
    )
    sender.disconnect.assert_not_called()
    assert capture._state._reconnect_interval == capture.EMITTER_RECONNECT_INTERVAL_SECONDS


def test_blender_reconnect_worker_backs_off_after_failure(monkeypatch):
    from integrations.blender import capture

    sender = MagicMock()
    sender.connect.return_value = False
    monkeypatch.setattr(capture._state, "sender", sender)
    monkeypatch.setattr(capture._state, "_reconnect_generation", 7)
    monkeypatch.setattr(capture._state, "_reconnect_interval", 4.0)

    capture._emitter_reconnect_worker(sender, 7)

    assert capture._state._reconnect_interval == 8.0


def test_blender_reconnect_worker_closes_stale_connection(monkeypatch):
    from integrations.blender import capture

    sender = MagicMock()
    sender.connect.return_value = True
    monkeypatch.setattr(capture._state, "sender", sender)
    monkeypatch.setattr(capture._state, "_reconnect_generation", 8)

    capture._emitter_reconnect_worker(sender, 7)

    sender.disconnect.assert_called_once_with()


def test_cancel_blender_reconnect_waits_for_worker(monkeypatch):
    from integrations.blender import capture

    reconnect_thread = MagicMock()
    reconnect_thread.is_alive.return_value = True
    monkeypatch.setattr(capture._state, "_reconnect_thread", reconnect_thread)
    generation = capture._state._reconnect_generation
    monkeypatch.setattr(capture._state, "_reconnect_generation", generation)

    capture._cancel_emitter_reconnect()

    reconnect_thread.join.assert_called_once_with()
    assert capture._state._reconnect_thread is None
    assert capture._state._reconnect_generation == generation + 1


def test_remote_apply_refreshes_only_matching_transform_baselines(monkeypatch):
    from integrations.blender import capture

    remote = MockBlenderObject(
        "Remote",
        loc=(4, 5, 6),
        prim_path="/World/Remote",
    )
    unrelated = MockBlenderObject(
        "Unrelated",
        loc=(7, 8, 9),
        prim_path="/World/Unrelated",
    )
    untagged = MockBlenderObject("Untagged", loc=(1, 2, 3))
    monkeypatch.setattr(
        capture.bpy.data,
        "objects",
        _BlenderObjectList([remote, unrelated, untagged]),
    )
    author = MagicMock()
    author._last_matrix = {"Unrelated": ("unchanged",)}
    author._prim_refs = {}
    author._used_prim_paths = set()
    monkeypatch.setattr(capture._state, "author", author)

    refreshed = capture.refresh_transform_baselines_after_remote_apply(
        {"/World/Remote": remote},
    )

    assert refreshed == 1
    assert author._last_matrix["Remote"] == tuple(
        value for row in remote.matrix_world for value in row
    )
    assert author._last_matrix["Unrelated"] == ("unchanged",)
    assert author._prim_refs == {"/World/Remote": remote}
    assert author._used_prim_paths == {"/World/Remote"}


def test_user_edit_after_remote_baseline_refresh_still_emits(monkeypatch):
    from integrations.blender import capture

    remote = MockBlenderObject(
        "Remote",
        loc=(4, 5, 6),
        prim_path="/World/Remote",
    )
    objects = _BlenderObjectList([remote])
    monkeypatch.setattr(capture.bpy.data, "objects", objects)
    author, emitter = _make_author_and_emitter(auto_track=False)
    monkeypatch.setattr(capture._state, "author", author)

    assert (
        capture.refresh_transform_baselines_after_remote_apply(
            {"/World/Remote": remote},
        )
        == 1
    )
    remote.matrix_world = _Matrix(loc=(5, 5, 6))

    events = _get_events(author, emitter, [MockDepsgraphUpdate(remote)])

    assert any(event["k"] == K_SET_XFORM_TRS for event in events)


def test_receiver_tracks_only_object_events_for_delayed_transform_refresh(monkeypatch):
    from integrations.blender import receiver_addon

    pending = set()
    monkeypatch.setattr(receiver_addon, "_pending_object_baseline_paths", pending)

    receiver_addon._on_applied_events(
        [
            {"k": "set_xform_trs", "prim": "/World/A"},
            {"k": "ensure_prim", "prim": "/World/B", "typeName": "Xform"},
            {
                "k": "ensure_prim",
                "prim": "/World/Looks/Material",
                "typeName": "Material",
            },
            {"k": "set_connectable_input", "prim": "/World/Looks/Material/Shader"},
        ],
    )

    assert pending == {"/World/A", "/World/B"}


def test_blender_connect_reuses_sender_with_unacknowledged_outbox(monkeypatch):
    from integrations.blender import capture

    sender = MagicMock()
    sender.sock = None
    sender.host = "127.0.0.1"
    sender.port = 7200
    sender.department = "animation"
    sender.pending_transaction_count = 1
    sender.connect.return_value = True
    author = MagicMock()
    emitter = MagicMock()
    scene = MagicMock()
    scene.usd_connect_emit_host = "127.0.0.1"
    scene.usd_connect_emit_port = 7200
    scene.usd_connect_department = "animation"
    scene.usd_connect_auto_track = True
    scene.usd_connect_net_emitter_running = False
    context = MagicMock(scene=scene)
    monkeypatch.setattr(capture._state, "sender", sender)
    monkeypatch.setattr(capture._state, "author", author)
    monkeypatch.setattr(capture._state, "notice_emitter", emitter)
    constructor = MagicMock()
    monkeypatch.setattr(capture, "EventSender", constructor)
    monkeypatch.setattr(capture, "_remove_handler", MagicMock())
    monkeypatch.setattr(capture.bpy.app.timers, "register", MagicMock())
    operator = capture.USD_CONNECT_OT_connect_emitter()
    operator.report = MagicMock()

    assert operator.execute(context) == {"FINISHED"}
    sender.connect.assert_called_once_with()
    constructor.assert_not_called()
    assert capture._state.sender is sender


def test_blender_disconnect_retains_outbox_when_flush_times_out(monkeypatch):
    from integrations.blender import capture

    sender = MagicMock()
    sender.flush.return_value = False
    sender.pending_transaction_count = 2
    emitter = MagicMock()
    scene = MagicMock()
    context = MagicMock(scene=scene)
    monkeypatch.setattr(capture._state, "sender", sender)
    monkeypatch.setattr(capture._state, "notice_emitter", emitter)
    monkeypatch.setattr(capture._state, "author", MagicMock())
    monkeypatch.setattr(capture, "_remove_handler", MagicMock())
    operator = capture.USD_CONNECT_OT_disconnect_emitter()
    operator.report = MagicMock()

    assert operator.execute(context) == {"FINISHED"}
    sender.flush.assert_called_once_with(timeout=2.0)
    sender.disconnect.assert_called_once_with()
    assert capture._state.sender is sender
    operator.report.assert_called_once()
    assert "retained" in operator.report.call_args.args[1]


def test_receiver_rebuilds_logical_layers_from_full_replay():
    from integrations.blender import receiver_addon

    plan = receiver_addon._replay_plan(
        last_seq=42,
        mirror_reusable=False,
        live_metadata=None,
    )

    assert plan.sync_from == 1
    assert plan.baseline_seq == 0
    assert plan.layered_replay is True


def test_receiver_resumes_after_flattened_live_snapshot():
    from integrations.blender import receiver_addon

    plan = receiver_addon._replay_plan(
        last_seq=91,
        mirror_reusable=False,
        live_metadata={"snapshot_seq": 17},
    )

    assert plan.sync_from == 18
    assert plan.baseline_seq == 17
    assert plan.layered_replay is False


def test_receiver_reuses_retained_mirror_state():
    from integrations.blender import receiver_addon

    plan = receiver_addon._replay_plan(
        last_seq=42,
        mirror_reusable=True,
        live_metadata=None,
    )

    assert plan.sync_from == 43
    assert plan.baseline_seq == 42
    assert plan.layered_replay is True


def test_receiver_discards_retained_replay_state(monkeypatch):
    from integrations.blender import receiver_addon

    dispatcher = MagicMock()
    monkeypatch.setattr(receiver_addon, "_DISPATCHER", dispatcher)
    monkeypatch.setattr(receiver_addon, "_ADAPTER", object())
    monkeypatch.setattr(receiver_addon, "_MIRROR_STAGE", object())
    monkeypatch.setattr(receiver_addon, "_MIRROR_SOURCE", "/old/base.usda")
    monkeypatch.setattr(receiver_addon, "_MIRROR_ENDPOINT", ("127.0.0.1", 7200))
    monkeypatch.setattr(receiver_addon, "_MIRROR_LAYERED_REPLAY", True)
    monkeypatch.setattr(receiver_addon, "_pending_import_seed_paths", {"/World"})
    monkeypatch.setattr(
        receiver_addon,
        "_pending_object_baseline_paths",
        {"/World/Remote"},
    )
    monkeypatch.setattr(
        receiver_addon,
        "_pending_shader_baseline_paths",
        {"/Material"},
    )

    receiver_addon._discard_replay_state()

    dispatcher.close.assert_called_once_with()
    assert receiver_addon._DISPATCHER is None
    assert receiver_addon._ADAPTER is None
    assert receiver_addon._MIRROR_STAGE is None
    assert receiver_addon._MIRROR_SOURCE == ""
    assert receiver_addon._MIRROR_ENDPOINT is None
    assert receiver_addon._MIRROR_LAYERED_REPLAY is None
    assert receiver_addon._pending_import_seed_paths == set()
    assert receiver_addon._pending_object_baseline_paths == set()
    assert receiver_addon._pending_shader_baseline_paths == set()


def test_receiver_thread_cleanup_tolerates_unstarted_thread():
    from integrations.blender import receiver_addon

    receiver = MagicMock()
    receiver.join.side_effect = RuntimeError("cannot join thread before it is started")

    receiver_addon._stop_receiver_thread(receiver)

    receiver.stop.assert_called_once_with()
    receiver.join.assert_called_once_with(timeout=2.0)


def test_receiver_sequence_persistence_tolerates_released_scene():
    from integrations.blender import receiver_addon

    class ReleasedScene:
        def __setattr__(self, name, value):
            raise ReferenceError("Blender scene has been removed")

    receiver_addon._store_last_sequence(ReleasedScene(), 42)


def test_receiver_stage_metadata_updates_adapter_axis_conversion(monkeypatch):
    from integrations.blender import blender_adapter

    monkeypatch.setattr(blender_adapter, "BPY_AVAILABLE", False)
    adapter = BlenderAdapter(scene_up_axis="Y")
    assert adapter._needs_axis_conv is True

    adapter.set_stage_metadata(upAxis="Z")

    assert adapter._needs_axis_conv is False
