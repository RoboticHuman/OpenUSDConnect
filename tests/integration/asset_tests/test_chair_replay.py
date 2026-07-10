"""Chair material + up-axis orientation on a single backlog replay, with the
base scene imported first (the real user flow).

test_scene.usda is imported first: Blender's USD importer puts a +90deg X root
rotation on the Y-up "World". THEN the receiver connects and replays the backlog
chair (authored by the pytest wrapper before Blender starts). The chair, created
under the already-rotated /World, must (1) inherit the wood material bound only
on the parent Xform and (2) stand upright -- inheriting the root's rotation, not
getting per-object axis-converted on top of it (which tips it onto its side).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from helpers import PROJECT_ROOT, TestHarness  # noqa: E402,I001

import bpy  # noqa: E402
import mathutils  # noqa: E402

harness = TestHarness("CHAIR_REPLAY")
BASE_USD = os.path.join(PROJECT_ROOT, "test_scene.usda")
_WOOD_RGB = (0.40, 0.26, 0.13)


def _setup():
    harness.log("Installing addon + importing base scene...")
    bpy.ops.preferences.addon_install(filepath=harness.addon_zip, overwrite=True)
    bpy.ops.preferences.addon_enable(module="usd_connect")
    bpy.ops.usd_connect.import_with_hook(filepath=BASE_USD)
    scene = bpy.context.scene
    scene.usd_connect_recv_host = harness.host
    scene.usd_connect_recv_port = harness.port
    scene.usd_connect_recv_last_seq = 0
    bpy.ops.usd_connect.start_receiver()
    harness.log("Setup done.")


def _check_wood(obj_name):
    obj = next((o for o in bpy.data.objects if obj_name in o.name and o.type == "MESH"), None)
    if obj is None:
        harness._fail(f"no mesh containing '{obj_name}'")
        return
    if not obj.data.materials or obj.data.materials[0] is None:
        harness._fail(f"{obj.name}: no material assigned")
        return
    mat = obj.data.materials[0]
    if "Wood" not in mat.name:
        harness._fail(f"{obj.name}: material is '{mat.name}', not Wood")
        return
    bsdf = next((n for n in (mat.node_tree.nodes if mat.node_tree else [])
                 if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None:
        harness._fail(f"{obj.name}/{mat.name}: no Principled BSDF (empty material)")
        return
    bc = bsdf.inputs["Base Color"].default_value
    if all(abs(bc[i] - _WOOD_RGB[i]) < 0.08 for i in range(3)):
        harness._pass(f"{obj.name} -> {mat.name} brown")
    else:
        harness._fail(
            f"{obj.name}/{mat.name}: base color "
            f"({bc[0]:.2f},{bc[1]:.2f},{bc[2]:.2f}) not wood brown"
        )


def _check_upright(obj_name):
    obj = next((o for o in bpy.data.objects if obj_name in o.name and o.type == "MESH"), None)
    if obj is None:
        harness._fail(f"no mesh containing '{obj_name}'")
        return
    corners = [obj.matrix_world @ mathutils.Vector(c) for c in obj.bound_box]
    xs = [c.x for c in corners]
    ys = [c.y for c in corners]
    zs = [c.z for c in corners]
    dx, dy, dz = max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)
    # The seat is a flat slab: upright means its thinnest world dimension is Z
    # (horizontal slab, normal pointing up). Tipped means thinnest is X or Y.
    if dz <= dx and dz <= dy:
        harness._pass(f"{obj.name} upright (world dims x={dx:.2f} y={dy:.2f} z={dz:.2f})")
    else:
        harness._fail(
            f"{obj.name} NOT upright "
            f"(world dims x={dx:.2f} y={dy:.2f} z={dz:.2f}): tipped"
        )


_step = 0


def _run():
    global _step
    if _step == 0:
        _setup()
        _step = 1
        return 10.0
    elif _step == 1:
        _check_wood("Chair_Seat")
        _check_upright("Chair_Seat")
        harness.done()
        return None


bpy.app.timers.register(_run, first_interval=2.0)
