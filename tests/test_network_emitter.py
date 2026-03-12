"""Tests for _NetworkEmitter.build_events_from_updates() — the Blender capture logic.

Mocks bpy to test event building without a running Blender instance.
Verifies:
- First-encounter emits ensure_prim + ensure_xform_ops + set_xform_trs
- No visibility events emitted from depsgraph handler (removed — unreliable)
- Partial TRS diffs (only changed fields sent)
- Deletion detection via tracked prims
- Auto-track assigns prim paths
- Feedback guard blocks events when _applying_remote is True
- Multiple objects in a single update batch
"""

import sys
import types
import pytest
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
        return  # already available (running inside Blender)

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
    bpy.types.Operator = type("Operator", (), {"bl_idname": "", "bl_label": "", "bl_description": ""})
    bpy.types.Panel = type("Panel", (), {"bl_idname": "", "bl_label": "", "bl_space_type": "", "bl_region_type": "", "bl_category": ""})

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
    """Minimal mock of mathutils.Matrix."""
    def __init__(self, loc=(0, 0, 0), rot=(1, 0, 0, 0), scl=(1, 1, 1)):
        self._loc = loc
        self._rot = rot
        self._scl = scl

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
    """Simulates a Blender object with custom properties and transform.

    Set ``_deleted = True`` to simulate a removed object — accessing ``.name``
    will raise ``ReferenceError``, matching real Blender behaviour.
    """

    def __init__(self, name, loc=(0, 0, 0), rot=(1, 0, 0, 0), scl=(1, 1, 1),
                 prim_path=None, type_name=None, obj_type="MESH"):
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
from integrations.blender.capture import _NetworkEmitter


def _make_emitter(**kwargs):
    """Create a _NetworkEmitter without connecting to a server."""
    defaults = dict(host="127.0.0.1", port=7200, client_id="test",
                    auto_track=False)
    defaults.update(kwargs)
    em = _NetworkEmitter(**defaults)
    em.sock = MagicMock()  # pretend connected
    # Seed used-paths from mock bpy.data.objects (mirrors connect() behavior)
    em._used_prim_paths = {
        obj.get("usd_prim_path")
        for obj in sys.modules["bpy"].data.objects
        if obj.get("usd_prim_path")
    }
    return em


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFirstEncounter:
    """Test event generation when an object is first seen."""

    def test_first_encounter_emits_ensure_and_trs(self):
        obj = MockBlenderObject("Cube", loc=(1, 2, 3), prim_path="/World/Cube", type_name="Cube")
        sys.modules["bpy"].data.objects = _BlenderObjectList([obj])
        em = _make_emitter()

        events = em.build_events_from_updates([MockDepsgraphUpdate(obj)])

        kinds = [e["k"] for e in events]
        assert "ensure_prim" in kinds
        assert "ensure_xform_ops" in kinds
        assert "set_xform_trs" in kinds

    def test_first_encounter_sends_type_name(self):
        obj = MockBlenderObject("Sphere", loc=(1, 2, 3), prim_path="/World/Sphere", type_name="Sphere")
        sys.modules["bpy"].data.objects = _BlenderObjectList([obj])
        em = _make_emitter()

        events = em.build_events_from_updates([MockDepsgraphUpdate(obj)])

        ensure = [e for e in events if e["k"] == "ensure_prim"][0]
        assert ensure["typeName"] == "Sphere"

    def test_first_encounter_no_visibility_event(self):
        """Visibility is not tracked from depsgraph — no visibility events ever."""
        obj = MockBlenderObject("Cube", loc=(1, 2, 3), prim_path="/World/Cube")
        sys.modules["bpy"].data.objects = _BlenderObjectList([obj])
        em = _make_emitter()

        events = em.build_events_from_updates([MockDepsgraphUpdate(obj)])

        vis_events = [e for e in events if e["k"] == "set_visibility"]
        assert len(vis_events) == 0


class TestNoVisibilityFromDepsgraph:
    """Visibility tracking is removed from depsgraph handler entirely."""

    def test_never_emits_visibility(self):
        """No visibility events under any circumstances from build_events_from_updates."""
        obj = MockBlenderObject("Cube", loc=(1, 2, 3), prim_path="/World/Cube")
        sys.modules["bpy"].data.objects = _BlenderObjectList([obj])
        em = _make_emitter()

        all_events = []

        # First encounter
        events = em.build_events_from_updates([MockDepsgraphUpdate(obj)])
        all_events.extend(events)

        # Move
        obj.matrix_world = _Matrix(loc=(5, 6, 7))
        events = em.build_events_from_updates([MockDepsgraphUpdate(obj)])
        all_events.extend(events)

        # Move again
        obj.matrix_world = _Matrix(loc=(10, 20, 30))
        events = em.build_events_from_updates([MockDepsgraphUpdate(obj)])
        all_events.extend(events)

        vis = [e for e in all_events if e["k"] == "set_visibility"]
        assert len(vis) == 0


class TestMultipleObjects:
    """Test behavior when multiple objects are updated simultaneously."""

    def test_two_objects_first_encounter(self):
        cube = MockBlenderObject("Cube", loc=(1, 0, 0), prim_path="/World/Cube")
        sphere = MockBlenderObject("Sphere", loc=(0, 1, 0), prim_path="/World/Sphere")
        sys.modules["bpy"].data.objects = _BlenderObjectList([cube, sphere])
        em = _make_emitter()

        events = em.build_events_from_updates([
            MockDepsgraphUpdate(cube),
            MockDepsgraphUpdate(sphere),
        ])

        ensures = [e for e in events if e["k"] == "ensure_prim"]
        assert len(ensures) == 2
        trs = [e for e in events if e["k"] == "set_xform_trs"]
        assert len(trs) == 2
        vis = [e for e in events if e["k"] == "set_visibility"]
        assert len(vis) == 0

    def test_move_second_object_after_first(self):
        cube = MockBlenderObject("Cube", loc=(1, 0, 0), prim_path="/World/Cube")
        sphere = MockBlenderObject("Sphere", loc=(0, 1, 0), prim_path="/World/Sphere")
        sys.modules["bpy"].data.objects = _BlenderObjectList([cube, sphere])
        em = _make_emitter()

        # First encounter for both
        em.build_events_from_updates([
            MockDepsgraphUpdate(cube),
            MockDepsgraphUpdate(sphere),
        ])

        # Move only sphere
        sphere.matrix_world = _Matrix(loc=(10, 20, 30))
        events = em.build_events_from_updates([MockDepsgraphUpdate(sphere)])

        trs = [e for e in events if e["k"] == "set_xform_trs"]
        assert len(trs) == 1
        assert trs[0]["prim"] == "/World/Sphere"
        # No ensure_prim (already known)
        ensures = [e for e in events if e["k"] == "ensure_prim"]
        assert len(ensures) == 0

    def test_sequential_first_moves(self):
        """Move objects one at a time — the exact user scenario."""
        cube = MockBlenderObject("Cube", loc=(1, 0, 0), prim_path="/World/Cube")
        sphere = MockBlenderObject("Sphere", loc=(0, 1, 0), prim_path="/World/Sphere")
        cone = MockBlenderObject("Cone", loc=(0, 0, 1), prim_path="/World/Cone")
        sys.modules["bpy"].data.objects = _BlenderObjectList([cube, sphere, cone])
        em = _make_emitter()

        all_events = []
        for obj in [cube, sphere, cone]:
            events = em.build_events_from_updates([MockDepsgraphUpdate(obj)])
            all_events.extend(events)

        ensures = [e for e in all_events if e["k"] == "ensure_prim"]
        assert len(ensures) == 3
        trs = [e for e in all_events if e["k"] == "set_xform_trs"]
        assert len(trs) == 3
        vis = [e for e in all_events if e["k"] == "set_visibility"]
        assert len(vis) == 0


class TestPartialDiff:
    """Test TRS partial diffing."""

    def test_only_changed_fields_sent(self):
        obj = MockBlenderObject("Cube", loc=(1, 0, 0), prim_path="/World/Cube")
        sys.modules["bpy"].data.objects = _BlenderObjectList([obj])
        em = _make_emitter()

        # First encounter — all fields
        events1 = em.build_events_from_updates([MockDepsgraphUpdate(obj)])
        trs1 = [e for e in events1 if e["k"] == "set_xform_trs"]
        assert len(trs1) == 1
        assert set(trs1[0]["fields"]) == {"t", "r", "s"}

        # Change only translation
        obj.matrix_world = _Matrix(loc=(5, 0, 0))
        events2 = em.build_events_from_updates([MockDepsgraphUpdate(obj)])
        trs2 = [e for e in events2 if e["k"] == "set_xform_trs"]
        assert len(trs2) == 1
        assert trs2[0]["fields"] == ["t"]

    def test_no_trs_event_when_unchanged(self):
        obj = MockBlenderObject("Cube", loc=(1, 0, 0), prim_path="/World/Cube")
        sys.modules["bpy"].data.objects = _BlenderObjectList([obj])
        em = _make_emitter()

        em.build_events_from_updates([MockDepsgraphUpdate(obj)])

        # Same transform — no TRS event
        events = em.build_events_from_updates([MockDepsgraphUpdate(obj)])
        trs = [e for e in events if e["k"] == "set_xform_trs"]
        assert len(trs) == 0

    def test_no_duplicate_ensure_prim(self):
        obj = MockBlenderObject("Cube", loc=(1, 0, 0), prim_path="/World/Cube")
        sys.modules["bpy"].data.objects = _BlenderObjectList([obj])
        em = _make_emitter()

        em.build_events_from_updates([MockDepsgraphUpdate(obj)])
        obj.matrix_world = _Matrix(loc=(2, 0, 0))
        events = em.build_events_from_updates([MockDepsgraphUpdate(obj)])

        ensures = [e for e in events if e["k"] == "ensure_prim"]
        assert len(ensures) == 0


class TestDeletionDetection:
    """Test deletion detection via _prim_refs ReferenceError."""

    def test_deleted_object_emits_deactivate(self):
        obj = MockBlenderObject("Cube", loc=(1, 0, 0), prim_path="/World/Cube")
        sys.modules["bpy"].data.objects = _BlenderObjectList([obj])
        em = _make_emitter()

        em.build_events_from_updates([MockDepsgraphUpdate(obj)])
        assert "/World/Cube" in em._prim_refs

        # Mark object as deleted (Blender removes it — .name raises ReferenceError)
        obj._deleted = True
        events = em.build_events_from_updates([])

        deact = [e for e in events if e["k"] == "deactivate_prim"]
        assert len(deact) == 1
        assert deact[0]["prim"] == "/World/Cube"
        assert deact[0]["active"] is False

    def test_deletion_cleans_up_caches(self):
        obj = MockBlenderObject("Cube", loc=(1, 0, 0), prim_path="/World/Cube")
        sys.modules["bpy"].data.objects = _BlenderObjectList([obj])
        em = _make_emitter()

        em.build_events_from_updates([MockDepsgraphUpdate(obj)])
        assert "/World/Cube" in em._known_prims
        assert "/World/Cube" in em._prim_refs

        obj._deleted = True
        em.build_events_from_updates([])

        assert "/World/Cube" not in em._known_prims
        assert "/World/Cube" not in em._last_sent
        assert "/World/Cube" not in em._prim_refs
        assert "/World/Cube" not in em._used_prim_paths


class TestAutoTrack:
    """Test auto-track assigns prim paths based on parent hierarchy."""

    def _make_world_parent(self):
        """Create a mock 'World' empty that acts as the USD root prim."""
        world = MockBlenderObject("World", prim_path="/World", obj_type="EMPTY")
        return world

    def test_auto_track_assigns_path_from_parent(self):
        world = self._make_world_parent()
        obj = MockBlenderObject("Cube", loc=(1, 2, 3))
        obj.parent = world
        sys.modules["bpy"].data.objects = _BlenderObjectList([world, obj])
        em = _make_emitter(auto_track=True)

        events = em.build_events_from_updates([MockDepsgraphUpdate(obj)])

        ensures = [e for e in events if e["k"] == "ensure_prim"]
        assert len(ensures) == 1
        assert ensures[0]["prim"] == "/World/Cube"
        assert obj["usd_prim_path"] == "/World/Cube"

    def test_auto_track_skips_unparented_object(self):
        """Objects not parented under a tracked prim should be ignored."""
        obj = MockBlenderObject("Cube", loc=(1, 2, 3))
        # No parent — floating at scene root
        sys.modules["bpy"].data.objects = _BlenderObjectList([obj])
        em = _make_emitter(auto_track=True)

        events = em.build_events_from_updates([MockDepsgraphUpdate(obj)])
        assert len(events) == 0

    def test_auto_track_skips_parent_without_prim_path(self):
        """Parent exists but has no usd_prim_path — should be ignored."""
        parent = MockBlenderObject("SomeEmpty", obj_type="EMPTY")
        obj = MockBlenderObject("Cube", loc=(1, 2, 3))
        obj.parent = parent
        sys.modules["bpy"].data.objects = _BlenderObjectList([parent, obj])
        em = _make_emitter(auto_track=True)

        events = em.build_events_from_updates([MockDepsgraphUpdate(obj)])
        assert len(events) == 0

    def test_auto_track_infers_type(self):
        world = self._make_world_parent()
        obj = MockBlenderObject("Sphere", loc=(1, 2, 3))
        obj.data.name = "Sphere_mesh"  # _infer_usd_type checks mesh data name
        obj.parent = world
        sys.modules["bpy"].data.objects = _BlenderObjectList([world, obj])
        em = _make_emitter(auto_track=True)

        events = em.build_events_from_updates([MockDepsgraphUpdate(obj)])

        ensures = [e for e in events if e["k"] == "ensure_prim"]
        assert ensures[0]["typeName"] == "Sphere"

    def test_auto_track_nested_hierarchy(self):
        """Auto-track under a deeper parent (/World/Group/NewCube)."""
        world = MockBlenderObject("World", prim_path="/World", obj_type="EMPTY")
        group = MockBlenderObject("Group", prim_path="/World/Group", obj_type="EMPTY")
        group.parent = world
        obj = MockBlenderObject("NewCube", loc=(1, 2, 3))
        obj.parent = group
        sys.modules["bpy"].data.objects = _BlenderObjectList([world, group, obj])
        em = _make_emitter(auto_track=True)

        events = em.build_events_from_updates([MockDepsgraphUpdate(obj)])

        ensures = [e for e in events if e["k"] == "ensure_prim"]
        assert len(ensures) == 1
        assert ensures[0]["prim"] == "/World/Group/NewCube"
        assert obj["usd_prim_path"] == "/World/Group/NewCube"

    def test_auto_track_disambiguates_collision(self):
        """If /World/Cube already exists, new object gets /World/Cube_1."""
        world = self._make_world_parent()
        existing = MockBlenderObject("Geom", prim_path="/World/Cube")
        existing.parent = world
        new_cube = MockBlenderObject("Cube", loc=(1, 2, 3))
        new_cube.parent = world
        sys.modules["bpy"].data.objects = _BlenderObjectList([world, existing, new_cube])
        em = _make_emitter(auto_track=True)

        events = em.build_events_from_updates([MockDepsgraphUpdate(new_cube)])

        ensures = [e for e in events if e["k"] == "ensure_prim"]
        assert len(ensures) == 1
        assert ensures[0]["prim"] == "/World/Cube_1"
        assert new_cube["usd_prim_path"] == "/World/Cube_1"

    def test_auto_track_no_collision_uses_exact_name(self):
        """When no collision, the prim path uses the exact sanitized name."""
        world = self._make_world_parent()
        new_sphere = MockBlenderObject("Sphere", loc=(1, 2, 3))
        new_sphere.parent = world
        sys.modules["bpy"].data.objects = _BlenderObjectList([world, new_sphere])
        em = _make_emitter(auto_track=True)

        events = em.build_events_from_updates([MockDepsgraphUpdate(new_sphere)])

        ensures = [e for e in events if e["k"] == "ensure_prim"]
        assert ensures[0]["prim"] == "/World/Sphere"

    def test_auto_track_no_visibility(self):
        world = self._make_world_parent()
        obj = MockBlenderObject("Cube", loc=(1, 2, 3))
        obj.parent = world
        sys.modules["bpy"].data.objects = _BlenderObjectList([world, obj])
        em = _make_emitter(auto_track=True)

        events = em.build_events_from_updates([MockDepsgraphUpdate(obj)])
        vis = [e for e in events if e["k"] == "set_visibility"]
        assert len(vis) == 0


class TestFeedbackGuard:
    """Test that _applying_remote blocks event generation."""

    def test_applying_remote_blocks_events(self):
        obj = MockBlenderObject("Cube", loc=(1, 2, 3), prim_path="/World/Cube")
        sys.modules["bpy"].data.objects = _BlenderObjectList([obj])
        em = _make_emitter()
        em._applying_remote = True

        events = em.build_events_from_updates([MockDepsgraphUpdate(obj)])
        assert events == []


class TestEndToEndScenario:
    """Simulate the exact user scenario: create objects, move them one by one."""

    def test_four_objects_moved_sequentially(self):
        """Create 4 objects, move each one — only ensure_prim + TRS, no visibility."""
        cube = MockBlenderObject("Cube", loc=(0, 0, 0), prim_path="/World/Cube", type_name="Cube")
        sphere = MockBlenderObject("Sphere", loc=(0, 0, 0), prim_path="/World/Sphere", type_name="Sphere")
        cone = MockBlenderObject("Cone", loc=(0, 0, 0), prim_path="/World/Cone", type_name="Cone")
        cylinder = MockBlenderObject("Cylinder", loc=(0, 0, 0), prim_path="/World/Cylinder", type_name="Cylinder")
        all_objs = _BlenderObjectList([cube, sphere, cone, cylinder])
        sys.modules["bpy"].data.objects = all_objs
        em = _make_emitter()

        all_events = []
        for i, obj in enumerate(all_objs):
            obj.matrix_world = _Matrix(loc=(float(i), 0, 0))
            events = em.build_events_from_updates([MockDepsgraphUpdate(obj)])
            all_events.extend(events)

        ensures = [e for e in all_events if e["k"] == "ensure_prim"]
        assert len(ensures) == 4

        trs = [e for e in all_events if e["k"] == "set_xform_trs"]
        assert len(trs) == 4

        vis = [e for e in all_events if e["k"] == "set_visibility"]
        assert len(vis) == 0

        deact = [e for e in all_events if e["k"] == "deactivate_prim"]
        assert len(deact) == 0

    def test_move_then_delete(self):
        """Move an object, then delete it — should get deactivate_prim."""
        cube = MockBlenderObject("Cube", loc=(1, 0, 0), prim_path="/World/Cube")
        sphere = MockBlenderObject("Sphere", loc=(0, 1, 0), prim_path="/World/Sphere")
        sys.modules["bpy"].data.objects = _BlenderObjectList([cube, sphere])
        em = _make_emitter()

        # Move both
        em.build_events_from_updates([MockDepsgraphUpdate(cube), MockDepsgraphUpdate(sphere)])

        # Delete cube (mark as deleted — ReferenceError on .name access)
        cube._deleted = True
        events = em.build_events_from_updates([])

        deact = [e for e in events if e["k"] == "deactivate_prim"]
        assert len(deact) == 1
        assert deact[0]["prim"] == "/World/Cube"

    def test_non_object_updates_ignored(self):
        """Non-Object updates (meshes, materials, etc.) should be ignored."""
        obj = MockBlenderObject("Cube", loc=(1, 0, 0), prim_path="/World/Cube")
        sys.modules["bpy"].data.objects = _BlenderObjectList([obj])
        em = _make_emitter()

        # Pass a non-Object update
        fake_update = MockDepsgraphUpdate(MagicMock())  # not a bpy.types.Object
        events = em.build_events_from_updates([fake_update])

        assert len(events) == 0
