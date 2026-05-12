"""Tests for BlenderStageAuthor + NoticeEmitter integration.

Mocks bpy to test auto-track, depsgraph-to-events flow, partial diff,
deletion detection, and feedback guard using the new architecture.
"""

import sys
import tempfile
import types
from unittest.mock import MagicMock


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
    return emitter.build_events_for_dirty(include_matrices=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


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
        assert "/World" in ensure_paths
        assert "/World/Group" in ensure_paths
        assert "/World/Group/NewCube" in ensure_paths
        # Parent before child
        assert ensure_paths.index("/World") < ensure_paths.index("/World/Group")
        assert ensure_paths.index("/World/Group") < ensure_paths.index("/World/Group/NewCube")
        assert obj["usd_prim_path"] == "/World/Group/NewCube"

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
