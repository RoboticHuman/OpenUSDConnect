"""Shared test asset builder — creates a USD chair with real mesh geometry.

Used by both headless Blender test scripts and scripts/create_test_asset.py.
The chair has 6 meshes (Seat + 4 Legs + Back), 48 vertices total.
"""

from pxr import Usd, UsdGeom, Gf, Vt


# Expected counts for test assertions
EXPECTED_MESH_COUNT = 6
EXPECTED_VERTEX_COUNT = 48  # 6 meshes * 8 vertices each


def _box_verts(cx, cy, cz, hw, hh, hd):
    """8 vertices of an axis-aligned box centered at (cx, cy+hh, cz)."""
    return [
        Gf.Vec3f(cx - hw, cy,        cz - hd), Gf.Vec3f(cx + hw, cy,        cz - hd),
        Gf.Vec3f(cx + hw, cy,        cz + hd), Gf.Vec3f(cx - hw, cy,        cz + hd),
        Gf.Vec3f(cx - hw, cy + 2*hh, cz - hd), Gf.Vec3f(cx + hw, cy + 2*hh, cz - hd),
        Gf.Vec3f(cx + hw, cy + 2*hh, cz + hd), Gf.Vec3f(cx - hw, cy + 2*hh, cz + hd),
    ]


BOX_FACE_COUNTS = Vt.IntArray([4, 4, 4, 4, 4, 4])
BOX_FACE_INDICES = Vt.IntArray([
    0, 1, 5, 4,  # front
    1, 2, 6, 5,  # right
    2, 3, 7, 6,  # back
    3, 0, 4, 7,  # left
    4, 5, 6, 7,  # top
    3, 2, 1, 0,  # bottom
])


def _add_box_mesh(stage, path, cx, cy, cz, hw, hh, hd):
    """Define a UsdGeom.Mesh box at the given path."""
    mesh = UsdGeom.Mesh.Define(stage, path)
    verts = Vt.Vec3fArray(_box_verts(cx, cy, cz, hw, hh, hd))
    mesh.GetPointsAttr().Set(verts)
    mesh.GetFaceVertexCountsAttr().Set(BOX_FACE_COUNTS)
    mesh.GetFaceVertexIndicesAttr().Set(BOX_FACE_INDICES)
    mesh.GetExtentAttr().Set(Vt.Vec3fArray([
        Gf.Vec3f(cx - hw, cy, cz - hd),
        Gf.Vec3f(cx + hw, cy + 2*hh, cz + hd),
    ]))
    return mesh


def create_chair_asset(filepath):
    """Create a USD file with a chair model (6 meshes, 48 vertices).

    Structure:
        /Model          (Xform, defaultPrim)
        /Model/Seat     (Mesh — 2x0.4x2 box at y=0.6)
        /Model/Leg_0..3 (Mesh — thin boxes at corners)
        /Model/Back     (Mesh — thin slab at back edge)

    Returns the stage (caller can inspect or just let it go out of scope).
    """
    stage = Usd.Stage.CreateNew(filepath)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.Xform.Define(stage, "/Model")

    # Seat: 2x0.4x2 centered at y=0.8
    _add_box_mesh(stage, "/Model/Seat", 0, 0.6, 0, 1.0, 0.2, 1.0)

    # 4 Legs: thin boxes at corners
    leg_positions = [(-0.8, -0.8), (0.8, -0.8), (0.8, 0.8), (-0.8, 0.8)]
    for i, (lx, lz) in enumerate(leg_positions):
        _add_box_mesh(stage, f"/Model/Leg_{i}", lx, 0, lz, 0.08, 0.3, 0.08)

    # Backrest: thin slab at back
    _add_box_mesh(stage, "/Model/Back", 0, 1.0, -0.92, 1.0, 0.3, 0.08)

    stage.GetRootLayer().defaultPrim = "Model"
    stage.Save()
    return stage
