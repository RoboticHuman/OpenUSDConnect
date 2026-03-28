"""Blender headless test script for BlenderAdapter.

Run via: blender --background --python tests/blender_adapter_test_script.py
Exit code 0 = all tests pass, 1 = failure.

This script runs inside Blender's Python interpreter with real bpy/bmesh.
"""

# Add project root and tests dir to path
import os
import sys
import traceback

import bpy

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(os.path.dirname(_scripts_dir)))
tests_dir = os.path.dirname(os.path.dirname(_scripts_dir))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
if tests_dir not in sys.path:
    sys.path.insert(0, tests_dir)
for _k in [k for k in sys.modules if k.startswith("openusdconnect")]:
    del sys.modules[_k]

from integrations.blender.blender_adapter import BlenderAdapter


def _clear_scene():
    """Remove all objects from the scene."""
    bpy.ops.object.select_all(action="SELECT")
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
    if obj.type != "EMPTY":
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
    if obj.type != "MESH":
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
    if obj.type != "MESH":
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
    if obj.type != "MESH":
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
    if obj.type != "MESH":
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
    if obj.type != "MESH":
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
    adapter.set_xform_trs(
        "/World/Box",
        {
            "fields": ["t", "s"],
            "t": [3.0, 4.0, 5.0],
            "s": [2.0, 2.0, 2.0],
        },
    )
    obj = _find_by_prim(adapter, "/World/Box")
    if obj is None:
        r.fail(name, "object not found")
        return
    loc = obj.location
    # Adapter converts Y-up (USD) to Z-up (Blender): (x, y, z) → (x, -z, y)
    if abs(loc.x - 3.0) > 1e-4 or abs(loc.y + 5.0) > 1e-4 or abs(loc.z - 4.0) > 1e-4:
        r.fail(name, f"location wrong: {tuple(loc)} (expected ~(3, -5, 4) after Y→Z)")

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


def test_ensure_xform_ops_resets_mpi(r):
    """ensure_xform_ops resets non-identity matrix_parent_inverse to Identity."""
    name = "test_ensure_xform_ops_resets_mpi"
    _clear_scene()
    adapter = BlenderAdapter()
    import mathutils

    # Create parent and child
    adapter.ensure_prim("/World", "Xform")
    adapter.ensure_prim("/World/Child", "Cube")
    child = _find_by_prim(adapter, "/World/Child")
    if child is None:
        r.fail(name, "child object not created")
        return
    if child.parent is None:
        r.fail(name, "child has no parent")
        return

    # Simulate what Blender's USD importer does: set MPI to a non-identity matrix
    # (e.g. a 90° X rotation that converts Y-up USD to Z-up Blender)
    non_identity = mathutils.Matrix.Rotation(1.5708, 4, "X")
    child.matrix_parent_inverse = non_identity

    # Verify MPI is non-identity
    identity = mathutils.Matrix.Identity(4)
    if child.matrix_parent_inverse == identity:
        r.fail(name, "MPI should be non-identity before ensure_xform_ops")
        return

    # ensure_xform_ops should reset it
    adapter.ensure_xform_ops("/World/Child")

    if child.matrix_parent_inverse != identity:
        r.fail(name, f"MPI not reset to Identity: {child.matrix_parent_inverse}")
        return
    r.ok(name)


def test_ensure_xform_ops_preserves_world(r):
    """ensure_xform_ops preserves matrix_world when resetting MPI."""
    name = "test_ensure_xform_ops_preserves_world"
    _clear_scene()
    adapter = BlenderAdapter()
    import mathutils

    # Create parent and child
    adapter.ensure_prim("/World", "Xform")
    _find_by_prim(adapter, "/World")
    adapter.ensure_prim("/World/Child", "Cube")
    child = _find_by_prim(adapter, "/World/Child")
    if child is None or child.parent is None:
        r.fail(name, "child not created or not parented")
        return

    # Place the child at a known world position
    child.location = (3.0, 4.0, 5.0)
    bpy.context.view_layer.update()

    # Set a non-identity MPI (simulating USD Y-up → Blender Z-up import)
    rot_x_90 = mathutils.Matrix.Rotation(1.5708, 4, "X")
    child.matrix_parent_inverse = rot_x_90
    bpy.context.view_layer.update()

    # Record world position before
    world_before = child.matrix_world.copy()

    # ensure_xform_ops should reset MPI but preserve matrix_world
    adapter.ensure_xform_ops("/World/Child")
    bpy.context.view_layer.update()

    world_after = child.matrix_world.copy()

    # Compare element-by-element with tolerance
    eps = 1e-3
    for i in range(4):
        for j in range(4):
            if abs(world_before[i][j] - world_after[i][j]) > eps:
                r.fail(
                    name,
                    f"matrix_world changed at [{i}][{j}]: "
                    f"{world_before[i][j]:.4f} -> {world_after[i][j]:.4f}",
                )
                return
    r.ok(name)


def test_ensure_xform_ops_identity_noop(r):
    """ensure_xform_ops is a no-op when MPI is already identity."""
    name = "test_ensure_xform_ops_identity_noop"
    _clear_scene()
    adapter = BlenderAdapter()

    adapter.ensure_prim("/World", "Xform")
    adapter.ensure_prim("/World/Child", "Cube")
    child = _find_by_prim(adapter, "/World/Child")
    if child is None or child.parent is None:
        r.fail(name, "child not created or not parented")
        return

    child.location = (1.0, 2.0, 3.0)
    bpy.context.view_layer.update()

    # MPI is already identity (set by ensure_prim)
    basis_before = child.matrix_basis.copy()
    adapter.ensure_xform_ops("/World/Child")
    basis_after = child.matrix_basis.copy()

    if basis_before != basis_after:
        r.fail(name, "matrix_basis changed when MPI was already identity")
        return
    r.ok(name)


def test_ensure_xform_ops_no_parent(r):
    """ensure_xform_ops on unparented object is a no-op (no crash)."""
    name = "test_ensure_xform_ops_no_parent"
    _clear_scene()
    adapter = BlenderAdapter()
    adapter.ensure_prim("/Standalone", "Cube")
    result = adapter.ensure_xform_ops("/Standalone")
    if not result:
        r.fail(name, "ensure_xform_ops returned False")
        return
    r.ok(name)


def test_set_reference_imports_usd(r):
    """set_reference imports a USD file and creates mesh objects under container."""
    import tempfile

    from unit.test_asset_builder import (
        EXPECTED_MESH_COUNT,
        EXPECTED_VERTEX_COUNT,
        create_chair_asset,
    )

    name = "test_set_reference_imports_usd"
    _clear_scene()
    adapter = BlenderAdapter()

    # Create parent prim so parenting works
    adapter.ensure_prim("/World", "Xform")
    # Create the container (as would happen in normal event flow)
    adapter.ensure_prim("/World/Asset", "Xform")

    # Create the shared chair test asset
    tmp_dir = tempfile.mkdtemp()
    tmp_usd = os.path.join(tmp_dir, "test_chair.usda")
    try:
        create_chair_asset(tmp_usd)
    except Exception as e:
        r.fail(name, f"Failed to create test USD: {e}")
        return

    refs = [{"asset_path": tmp_usd}]
    result = adapter.set_reference("/World/Asset", refs)
    if not result:
        r.fail(name, "set_reference returned False")
        return

    # Verify container has Reference type tag
    container = adapter._find_object_by_prim("/World/Asset")
    if container is None:
        r.fail(name, "container not found by prim path /World/Asset")
        return
    if container.get("usd_type_name") != "Reference":
        r.fail(name, f"usd_type_name={container.get('usd_type_name')}, expected Reference")
        return

    # Verify child objects match the chair asset (6 meshes, 48 vertices)
    children = [
        o for o in bpy.data.objects if o.get("usd_prim_path", "").startswith("/World/Asset/")
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

    # Verify idempotent — calling again doesn't duplicate
    count_before = len(bpy.data.objects)
    adapter.set_reference("/World/Asset", refs)
    count_after = len(bpy.data.objects)
    if count_after != count_before:
        r.fail(name, f"idempotent check failed: {count_before} -> {count_after} objects")
        return

    r.ok(name)


def test_set_reference_reimport_after_delete(r):
    """set_reference re-imports when cached children have been deleted."""
    import tempfile

    from unit.test_asset_builder import EXPECTED_MESH_COUNT, create_chair_asset

    name = "test_set_reference_reimport_after_delete"
    _clear_scene()
    adapter = BlenderAdapter()

    adapter.ensure_prim("/World", "Xform")
    adapter.ensure_prim("/World/Asset", "Xform")

    tmp_dir = tempfile.mkdtemp()
    tmp_usd = os.path.join(tmp_dir, "test_chair.usda")
    try:
        create_chair_asset(tmp_usd)
    except Exception as e:
        r.fail(name, f"Failed to create test USD: {e}")
        return

    # First import
    refs = [{"asset_path": tmp_usd}]
    result = adapter.set_reference("/World/Asset", refs)
    if not result:
        r.fail(name, "first set_reference returned False")
        return

    children_before = [
        o for o in bpy.data.objects if o.get("usd_prim_path", "").startswith("/World/Asset/")
    ]
    if len(children_before) == 0:
        r.fail(name, "no children after first import")
        return

    # Delete all child objects using the adapter's own cleanup method,
    # which clears registry entries, aliases, and reference children.
    adapter._remove_imported_ref_children("/World/Asset")

    # Verify children are gone
    children_mid = [
        o for o in bpy.data.objects if o.get("usd_prim_path", "").startswith("/World/Asset/")
    ]
    if len(children_mid) != 0:
        r.fail(name, f"children still exist after delete: {len(children_mid)}")
        return

    # Diagnostic: check adapter state before second import
    has_children = adapter._registry.children_exist("/World/Asset")
    has_ref = adapter._registry.get_imported_ref("/World/Asset")
    container = adapter._registry.find("/World/Asset")
    remaining = [p for p in adapter._registry._objects
                 if p.startswith("/World/Asset/")]
    print(f"  info: before reimport: children_exist={has_children} "
          f"imported_ref={has_ref} container={container} "
          f"remaining_children={remaining}")

    # Second import — same asset path, but stale cache should be detected
    all_before = set(bpy.data.objects)
    result2 = adapter.set_reference("/World/Asset", refs)
    if not result2:
        r.fail(name, "second set_reference returned False")
        return
    all_after = set(bpy.data.objects)
    new_from_reimport = all_after - all_before
    print(f"  info: reimport created {len(new_from_reimport)} new objects: "
          f"{[o.name for o in new_from_reimport]}")
    print(f"  info: all objects after: {[o.name for o in bpy.data.objects]}")

    # Verify children were re-imported
    children_after = [
        o for o in bpy.data.objects if o.get("usd_prim_path", "").startswith("/World/Asset/")
    ]
    mesh_children = [o for o in children_after if o.type == "MESH" and o.data is not None]
    if len(mesh_children) != EXPECTED_MESH_COUNT:
        r.fail(
            name, f"expected {EXPECTED_MESH_COUNT} meshes after re-import, got {len(mesh_children)}"
        )
        return
    else:
        print(f"  info: re-imported {len(mesh_children)} meshes after stale cache detection")
    r.ok(name)


def test_set_reference_missing_file(r):
    """set_reference with non-existent file returns False gracefully."""
    name = "test_set_reference_missing_file"
    _clear_scene()
    adapter = BlenderAdapter()
    result = adapter.set_reference(
        "/World/Missing", [{"asset_path": "/nonexistent/path/fake.usda"}]
    )
    if result:
        r.fail(name, "set_reference should return False for missing file")
        return
    # Verify no objects were created
    count = sum(
        1 for o in bpy.data.objects if o.get("usd_prim_path", "").startswith("/World/Missing")
    )
    if count != 0:
        r.fail(name, f"expected 0 objects, got {count}")
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
        test_ensure_xform_ops_resets_mpi,
        test_ensure_xform_ops_preserves_world,
        test_ensure_xform_ops_identity_noop,
        test_ensure_xform_ops_no_parent,
        test_set_reference_imports_usd,
        test_set_reference_reimport_after_delete,
        test_set_reference_missing_file,
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
