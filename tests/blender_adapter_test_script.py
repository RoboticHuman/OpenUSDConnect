"""Blender headless test script for BlenderAdapter.

Run via: blender --background --python tests/blender_adapter_test_script.py
Exit code 0 = all tests pass, 1 = failure.

This script runs inside Blender's Python interpreter with real bpy/bmesh.
"""

import sys
import traceback

import bpy

# Add project root to path so we can import openusdconnect
import os
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from integrations.blender.blender_adapter import BlenderAdapter


def _clear_scene():
    """Remove all objects from the scene."""
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete()


def _find_by_prim(adapter, prim_path):
    return adapter._find_object_by_prim(prim_path)


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
        print(f"  FAIL: {name} — {msg}")


def test_ensure_prim_xform(r):
    """ensure_prim with Xform creates an Empty."""
    name = "test_ensure_prim_xform"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/Empty", "Xform")
    obj = _find_by_prim(adapter, "/World/Empty")
    if obj is None:
        r.fail(name, "object not created")
        return
    if obj.type != 'EMPTY':
        r.fail(name, f"expected EMPTY, got {obj.type}")
        return
    if obj.get("usd_type_name") != "Xform":
        r.fail(name, f"usd_type_name={obj.get('usd_type_name')}")
        return
    r.ok(name)


def test_ensure_prim_cube(r):
    """ensure_prim with Cube creates a mesh object with vertices."""
    name = "test_ensure_prim_cube"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/MyCube", "Cube")
    obj = _find_by_prim(adapter, "/World/MyCube")
    if obj is None:
        r.fail(name, "object not created")
        return
    if obj.type != 'MESH':
        r.fail(name, f"expected MESH, got {obj.type}")
        return
    if len(obj.data.vertices) == 0:
        r.fail(name, "mesh has no vertices")
        return
    if obj.get("usd_type_name") != "Cube":
        r.fail(name, f"usd_type_name={obj.get('usd_type_name')}")
        return
    r.ok(name)


def test_ensure_prim_sphere(r):
    """ensure_prim with Sphere creates a mesh with vertices."""
    name = "test_ensure_prim_sphere"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/MySphere", "Sphere")
    obj = _find_by_prim(adapter, "/World/MySphere")
    if obj is None:
        r.fail(name, "object not created")
        return
    if obj.type != 'MESH':
        r.fail(name, f"expected MESH, got {obj.type}")
        return
    if len(obj.data.vertices) < 10:
        r.fail(name, f"expected many vertices for sphere, got {len(obj.data.vertices)}")
        return
    r.ok(name)


def test_ensure_prim_cylinder(r):
    """ensure_prim with Cylinder creates a mesh."""
    name = "test_ensure_prim_cylinder"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/MyCylinder", "Cylinder")
    obj = _find_by_prim(adapter, "/World/MyCylinder")
    if obj is None:
        r.fail(name, "object not created")
        return
    if obj.type != 'MESH':
        r.fail(name, f"expected MESH, got {obj.type}")
        return
    if len(obj.data.vertices) == 0:
        r.fail(name, "mesh has no vertices")
        return
    r.ok(name)


def test_ensure_prim_cone(r):
    """ensure_prim with Cone creates a mesh."""
    name = "test_ensure_prim_cone"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/MyCone", "Cone")
    obj = _find_by_prim(adapter, "/World/MyCone")
    if obj is None:
        r.fail(name, "object not created")
        return
    if obj.type != 'MESH':
        r.fail(name, f"expected MESH, got {obj.type}")
        return
    if len(obj.data.vertices) == 0:
        r.fail(name, "mesh has no vertices")
        return
    r.ok(name)


def test_ensure_prim_mesh(r):
    """ensure_prim with Mesh creates an empty mesh object."""
    name = "test_ensure_prim_mesh"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/MyMesh", "Mesh")
    obj = _find_by_prim(adapter, "/World/MyMesh")
    if obj is None:
        r.fail(name, "object not created")
        return
    if obj.type != 'MESH':
        r.fail(name, f"expected MESH, got {obj.type}")
        return
    r.ok(name)


def test_ensure_prim_idempotent(r):
    """Calling ensure_prim twice doesn't create duplicates."""
    name = "test_ensure_prim_idempotent"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/Thing", "Cube")
    adapter.ensure_prim("/World/Thing", "Cube")
    count = sum(1 for o in bpy.data.objects if o.get("usd_prim_path") == "/World/Thing")
    if count != 1:
        r.fail(name, f"expected 1 object, got {count}")
        return
    r.ok(name)


def test_set_xform_trs(r):
    """set_xform_trs updates location and scale."""
    name = "test_set_xform_trs"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/Box", "Cube")
    adapter.set_xform_trs("/World/Box", {
        "fields": ["t", "s"],
        "t": [3.0, 4.0, 5.0],
        "s": [2.0, 2.0, 2.0],
    })
    obj = _find_by_prim(adapter, "/World/Box")
    if obj is None:
        r.fail(name, "object not found")
        return
    loc = obj.location
    if abs(loc.x - 3.0) > 1e-4 or abs(loc.y - 4.0) > 1e-4 or abs(loc.z - 5.0) > 1e-4:
        r.fail(name, f"location wrong: {tuple(loc)}")
        return
    scl = obj.scale
    if abs(scl.x - 2.0) > 1e-4:
        r.fail(name, f"scale wrong: {tuple(scl)}")
        return
    r.ok(name)


def test_set_visibility(r):
    """set_visibility hides/unhides objects."""
    name = "test_set_visibility"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/Vis", "Cube")

    adapter.set_visibility("/World/Vis", False)
    obj = _find_by_prim(adapter, "/World/Vis")
    if not obj.hide_viewport:
        r.fail(name, "hide_viewport should be True after visible=False")
        return

    adapter.set_visibility("/World/Vis", True)
    if obj.hide_viewport:
        r.fail(name, "hide_viewport should be False after visible=True")
        return
    r.ok(name)


def test_set_gprim_attrs_sphere(r):
    """set_gprim_attrs maps sphere radius to uniform scale."""
    name = "test_set_gprim_attrs_sphere"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/S", "Sphere")
    adapter.set_gprim_attrs("/World/S", {"radius": 3.0})
    obj = _find_by_prim(adapter, "/World/S")
    if obj is None:
        r.fail(name, "object not found")
        return
    if abs(obj.scale.x - 3.0) > 1e-4 or abs(obj.scale.y - 3.0) > 1e-4:
        r.fail(name, f"scale wrong: {tuple(obj.scale)}")
        return
    r.ok(name)


def test_set_gprim_attrs_cube(r):
    """set_gprim_attrs maps cube size to half-scale."""
    name = "test_set_gprim_attrs_cube"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/C", "Cube")
    adapter.set_gprim_attrs("/World/C", {"size": 4.0})
    obj = _find_by_prim(adapter, "/World/C")
    if obj is None:
        r.fail(name, "object not found")
        return
    expected = 4.0 / 2.0  # Blender default cube is 2 units
    if abs(obj.scale.x - expected) > 1e-4:
        r.fail(name, f"scale.x={obj.scale.x}, expected {expected}")
        return
    r.ok(name)


def test_set_gprim_attrs_cylinder(r):
    """set_gprim_attrs maps cylinder height/radius to scale axes."""
    name = "test_set_gprim_attrs_cylinder"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/Cyl", "Cylinder")
    adapter.set_gprim_attrs("/World/Cyl", {"radius": 2.0, "height": 6.0})
    obj = _find_by_prim(adapter, "/World/Cyl")
    if obj is None:
        r.fail(name, "object not found")
        return
    if abs(obj.scale.x - 2.0) > 1e-4 or abs(obj.scale.y - 2.0) > 1e-4:
        r.fail(name, f"scale xy wrong: {obj.scale.x}, {obj.scale.y}")
        return
    if abs(obj.scale.z - 3.0) > 1e-4:  # height/2
        r.fail(name, f"scale.z={obj.scale.z}, expected 3.0")
        return
    r.ok(name)


def test_delete_prim(r):
    """delete_prim removes the object."""
    name = "test_delete_prim"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/Del", "Cube")
    assert _find_by_prim(adapter, "/World/Del") is not None
    adapter.delete_prim("/World/Del")
    if _find_by_prim(adapter, "/World/Del") is not None:
        r.fail(name, "object still exists after delete")
        return
    r.ok(name)


def test_rename_prim(r):
    """rename_prim updates the custom property."""
    name = "test_rename_prim"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/World/OldName", "Xform")
    adapter.rename_prim("/World/OldName", "NewName")
    if _find_by_prim(adapter, "/World/OldName") is not None:
        r.fail(name, "old path still resolves")
        return
    obj = _find_by_prim(adapter, "/World/NewName")
    if obj is None:
        r.fail(name, "new path not found")
        return
    r.ok(name)


def test_set_reference_stub(r):
    """set_reference returns True (stub)."""
    name = "test_set_reference_stub"
    adapter = BlenderAdapter()
    result = adapter.set_reference("/World/Ref", "./asset.usd", "/Model")
    if not result:
        r.fail(name, "set_reference returned False")
        return
    r.ok(name)


def main():
    print("\n=== BlenderAdapter Headless Tests ===\n")
    r = TestResult()

    tests = [
        test_ensure_prim_xform,
        test_ensure_prim_cube,
        test_ensure_prim_sphere,
        test_ensure_prim_cylinder,
        test_ensure_prim_cone,
        test_ensure_prim_mesh,
        test_ensure_prim_idempotent,
        test_set_xform_trs,
        test_set_visibility,
        test_set_gprim_attrs_sphere,
        test_set_gprim_attrs_cube,
        test_set_gprim_attrs_cylinder,
        test_delete_prim,
        test_rename_prim,
        test_set_reference_stub,
    ]

    for t in tests:
        try:
            t(r)
        except Exception:
            r.fail(t.__name__, traceback.format_exc())

    print(f"\n=== Results: {r.passed} passed, {r.failed} failed ===\n")
    if r.errors:
        for name, msg in r.errors:
            print(f"  FAILED: {name}: {msg}")

    sys.exit(0 if r.failed == 0 else 1)


if __name__ == "__main__":
    main()
