"""Blender headless e2e test for set_reference receive path.

Run via: blender --background --python tests/test_e2e_blender_spawn.py
Exit code 0 = all tests pass, 1 = failure.

Tests the full Blender receive path: simulates receiving events through
_process_event and verifies that objects are created in Blender.
"""

import os
import sys
import tempfile
import traceback

import bpy

# Add project root and tests dir to path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tests_dir = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
if tests_dir not in sys.path:
    sys.path.insert(0, tests_dir)

from test_asset_builder import EXPECTED_MESH_COUNT, EXPECTED_VERTEX_COUNT

from integrations.blender.blender_adapter import BlenderAdapter
from integrations.blender.receiver_addon import _dispatch_event


class TestResult:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors = []

    def ok(self, name):
        self.passed += 1
        print(f"  PASS: {name}")

    def fail(self, name, msg):
        self.failed += 1
        self.errors.append((name, msg))
        print(f"  FAIL: {name} -- {msg}")


def _clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()


def _create_test_asset():
    """Create a USD chair asset with real mesh geometry and return its path."""
    from test_asset_builder import create_chair_asset

    tmp_dir = tempfile.mkdtemp()
    asset_path = os.path.join(tmp_dir, "test_chair.usda")
    create_chair_asset(asset_path)
    return asset_path


def test_receive_ensure_prim_and_set_reference(r):
    """Simulate receiving ensure_prim + set_reference events through dispatch."""
    name = "test_receive_ensure_prim_and_set_reference"
    _clear_scene()

    asset_path = _create_test_asset()
    adapter = BlenderAdapter()

    # Simulate the event sequence a receiver would get
    events = [
        {"k": "ensure_prim", "prim": "/World", "typeName": "Xform"},
        {"k": "ensure_prim", "prim": "/World/Chair", "typeName": "Xform"},
        {
            "k": "set_reference",
            "prim": "/World/Chair",
            "refs": [{"asset_path": asset_path, "prim_path": "/Model"}],
        },
    ]

    for ev in events:
        _dispatch_event(adapter, ev["k"], ev["prim"], ev)

    # Verify /World was created
    world = adapter._find_object_by_prim("/World")
    if world is None:
        r.fail(name, "/World object not created")
        return

    # Verify /World/Chair was created
    chair = adapter._find_object_by_prim("/World/Chair")
    if chair is None:
        r.fail(name, "/World/Chair object not created")
        return

    # Verify container is tagged as Reference
    if chair.get("usd_type_name") != "Reference":
        r.fail(name, f"usd_type_name={chair.get('usd_type_name')}, expected Reference")
        return

    # Verify child mesh objects were imported under the container
    children = [
        o for o in bpy.data.objects if o.get("usd_prim_path", "").startswith("/World/Chair/")
    ]
    if len(children) == 0:
        r.fail(name, "no child objects imported under /World/Chair/")
        return

    # Verify imported meshes match expected chair geometry
    mesh_children = [o for o in children if o.type == "MESH" and o.data is not None]
    if len(mesh_children) != EXPECTED_MESH_COUNT:
        r.fail(
            name,
            f"expected {EXPECTED_MESH_COUNT} meshes, "
            f"got {len(mesh_children)} (types: {[o.type for o in children]})",
        )
        return

    total_verts = sum(len(o.data.vertices) for o in mesh_children)
    if total_verts != EXPECTED_VERTEX_COUNT:
        r.fail(name, f"expected {EXPECTED_VERTEX_COUNT} vertices, got {total_verts}")
        return

    print(f"  info: {len(mesh_children)} meshes, {total_verts} vertices — matches asset")
    r.ok(name)


def test_receive_set_reference_missing_file(r):
    """set_reference with missing file returns False, no crash."""
    name = "test_receive_set_reference_missing_file"
    _clear_scene()

    adapter = BlenderAdapter()
    _dispatch_event(
        adapter,
        "ensure_prim",
        "/World",
        {"k": "ensure_prim", "prim": "/World", "typeName": "Xform"},
    )

    ev = {
        "k": "set_reference",
        "prim": "/World/Bad",
        "refs": [{"asset_path": "/nonexistent/fake.usda"}],
    }
    _dispatch_event(adapter, "set_reference", "/World/Bad", ev)

    # Should not crash, and no objects for /World/Bad
    bad = adapter._find_object_by_prim("/World/Bad")
    if bad is not None:
        r.fail(name, "object created for missing reference file")
        return
    r.ok(name)


def test_full_receive_pipeline(r):
    """Full pipeline: ensure hierarchy, set reference, then set transform on parent."""
    name = "test_full_receive_pipeline"
    _clear_scene()

    asset_path = _create_test_asset()
    adapter = BlenderAdapter()

    events = [
        {"k": "ensure_prim", "prim": "/World", "typeName": "Xform"},
        {"k": "ensure_xform_ops", "prim": "/World"},
        {"k": "ensure_prim", "prim": "/World/Chair", "typeName": "Xform"},
        {"k": "ensure_xform_ops", "prim": "/World/Chair"},
        {"k": "set_xform_trs", "prim": "/World/Chair", "fields": ["t"], "t": [5.0, 0.0, 3.0]},
        {
            "k": "set_reference",
            "prim": "/World/Chair",
            "refs": [{"asset_path": asset_path, "prim_path": "/Model"}],
        },
    ]

    for ev in events:
        _dispatch_event(adapter, ev["k"], ev["prim"], ev)

    # Verify chair exists and has the reference tag
    chair = adapter._find_object_by_prim("/World/Chair")
    if chair is None:
        r.fail(name, "/World/Chair not found")
        return

    # Verify container is tagged with reference asset
    ref_asset = chair.get("usd_ref_asset", "")
    if not ref_asset:
        r.fail(name, "container missing usd_ref_asset tag")
        return

    # Verify imported children have mesh data matching the chair asset
    children = [
        o for o in bpy.data.objects if o.get("usd_prim_path", "").startswith("/World/Chair/")
    ]
    mesh_children = [o for o in children if o.type == "MESH" and o.data is not None]
    if len(mesh_children) != EXPECTED_MESH_COUNT:
        r.fail(
            name,
            f"expected {EXPECTED_MESH_COUNT} meshes, "
            f"got {len(mesh_children)} (types: {[o.type for o in children]})",
        )
        return

    total_verts = sum(len(o.data.vertices) for o in mesh_children)
    if total_verts != EXPECTED_VERTEX_COUNT:
        r.fail(name, f"expected {EXPECTED_VERTEX_COUNT} vertices, got {total_verts}")
        return

    # Verify transform was applied to container before import
    loc = chair.location
    if abs(loc.x - 5.0) > 0.01 or abs(loc.z - 3.0) > 0.01:
        r.fail(name, f"chair location wrong: {tuple(loc)}, expected ~(5,0,3)")
        return

    print(f"  info: {len(mesh_children)} meshes, {total_verts} verts, loc={tuple(loc)}")
    r.ok(name)


def main():
    print("\n=== Blender E2E Reference Spawn Tests ===\n")
    r = TestResult()

    tests = [
        test_receive_ensure_prim_and_set_reference,
        test_receive_set_reference_missing_file,
        test_full_receive_pipeline,
    ]

    for t in tests:
        try:
            t(r)
        except Exception:
            r.fail(t.__name__, traceback.format_exc())

    print(f"\n=== Results: {r.passed} passed, {r.failed} failed ===\n")
    if r.errors:
        for test_name, msg in r.errors:
            print(f"  FAILED: {test_name}: {msg}")

    sys.exit(0 if r.failed == 0 else 1)


if __name__ == "__main__":
    main()
