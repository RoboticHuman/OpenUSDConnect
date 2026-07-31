"""Material Zoo backlog replay through the packaged Blender integration.

The pytest wrapper starts an authoritative server with test_scene.usda and
publishes tests/visual/fixtures/material_zoo.jsonl before Blender connects.
This script imports only that base stage, starts the addon receiver at seq=1,
and verifies the DCC scene after the full transaction settles.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from helpers import PROJECT_ROOT, TestHarness  # noqa: E402,I001

import bpy  # noqa: E402


def _parse_expected_seq() -> int:
    forwarded = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-seq", type=int, required=True)
    return parser.parse_known_args(forwarded)[0].expected_seq


EXPECTED_SEQ = _parse_expected_seq()
BASE_USD = os.path.join(PROJECT_ROOT, "test_scene.usda")
BASE_TRANSFORMS = {
    "/World/Cube": ("/World/Cube/Geom", (0.0, 0.0, 0.0)),
    "/World/Sphere": ("/World/Sphere/Geom", (0.0, 1.5, 0.0)),
    "/World/Cone": ("/World/Cone/Geom", (0.0, 3.0, 0.0)),
    "/World/Cylinder": ("/World/Cylinder/Geom", (0.0, 4.5, 0.0)),
}
CHAIR_PATHS = (
    "/World/Chair/Seat",
    "/World/Chair/LegFL",
    "/World/Chair/LegFR",
    "/World/Chair/LegBL",
    "/World/Chair/LegBR",
    "/World/Chair/Backrest",
)
WOOD_RGB = (0.40, 0.26, 0.13)

harness = TestHarness("MATERIAL_ZOO_REPLAY")
receiver_addon = None
deadline = 0.0
settled_at = None


def _setup() -> bool:
    global receiver_addon, deadline

    harness.log("Installing addon and importing authoritative base scene...")
    bpy.ops.preferences.addon_install(filepath=harness.addon_zip, overwrite=True)
    bpy.ops.preferences.addon_enable(module="usd_connect")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    scene = bpy.context.scene
    scene.usd_connect_live_auto_start_emitter = False
    scene.usd_connect_live_auto_start_receiver = False
    scene.usd_connect_asset_root = PROJECT_ROOT
    result = bpy.ops.usd_connect.import_with_hook(filepath=BASE_USD)
    if "FINISHED" not in result:
        harness._fail(f"base USD import failed: {result}")
        harness.done()
        return False

    scene.usd_connect_recv_host = harness.host
    scene.usd_connect_recv_port = harness.port
    scene.usd_connect_recv_last_seq = 0

    import usd_connect.receiver_addon as addon_receiver

    receiver_addon = addon_receiver
    receiver_addon._LAST_SEQ = 0
    receiver_addon._ADAPTER = None
    result = bpy.ops.usd_connect.start_receiver()
    if "FINISHED" not in result:
        harness._fail(f"receiver start failed: {result}")
        harness.done()
        return False

    deadline = time.monotonic() + 120.0
    harness.log(f"Waiting for Material Zoo sequence {EXPECTED_SEQ}...")
    return True


def _close_tuple(actual, expected, tolerance=1e-5) -> bool:
    return len(actual) == len(expected) and all(
        abs(float(got) - want) <= tolerance for got, want in zip(actual, expected, strict=True)
    )


def _check_base_transforms(adapter) -> None:
    for parent_path, (geom_path, expected_location) in BASE_TRANSFORMS.items():
        parent_obj = adapter._find_object_by_prim(parent_path)
        geom_obj = adapter._find_object_by_prim(geom_path)
        if parent_obj is None or parent_obj is not geom_obj:
            harness._fail(f"{parent_path} and {geom_path} do not resolve to one object")
            continue
        aliases = tuple(parent_obj.get("_usd_prim_path_aliases", ()))
        if parent_path not in aliases:
            harness._fail(f"{geom_path} is missing collapsed alias {parent_path}")
            continue
        if not _close_tuple(tuple(parent_obj.location), expected_location):
            harness._fail(
                f"{parent_path}: location={tuple(parent_obj.location)}, "
                f"expected={expected_location}"
            )
            continue
        harness._pass(f"{parent_path} replays onto collapsed {geom_path}")


def _principled_node(material):
    if material is None or material.node_tree is None:
        return None
    return next(
        (node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"),
        None,
    )


def _check_chair(adapter) -> None:
    failures = []
    for prim_path in CHAIR_PATHS:
        obj = adapter._find_object_by_prim(prim_path)
        if obj is None or obj.type != "MESH":
            failures.append(f"{prim_path}: mesh missing")
            continue
        material = obj.data.materials[0] if obj.data.materials else None
        if material is None or material.get("usd_material_path") != "/World/Looks/Wood":
            failures.append(f"{prim_path}: Wood binding missing")
            continue
        node = _principled_node(material)
        if node is None:
            failures.append(f"{prim_path}: Principled node missing")
            continue
        color = node.inputs["Base Color"].default_value
        if not _close_tuple(tuple(color[:3]), WOOD_RGB, tolerance=0.08):
            failures.append(f"{prim_path}: color={tuple(color[:3])}")
    if failures:
        harness._fail("chair replay: " + "; ".join(failures))
    else:
        harness._pass("six chair meshes inherit the Wood material")


def _check_woodball2(adapter) -> None:
    obj = adapter._find_object_by_prim("/World/WoodBall2")
    if obj is None or obj.type != "MESH":
        harness._fail("/World/WoodBall2 mesh missing")
        return
    material = obj.data.materials[0] if obj.data.materials else None
    if material is None or material.get("usd_material_path") != "/World/Looks/WoodUV":
        harness._fail("/World/WoodBall2 is missing its WoodUV binding")
        return
    nodes = list(material.node_tree.nodes) if material.node_tree else []
    node_types = {node.type for node in nodes}
    required_types = {"TEX_IMAGE", "HUE_SAT", "MIX", "BSDF_PRINCIPLED"}
    if not required_types.issubset(node_types):
        harness._fail(f"WoodUV node types={sorted(node_types)}")
        return
    images = [
        node.image
        for node in nodes
        if node.type == "TEX_IMAGE" and node.image is not None
    ]
    if not images or any(image.size[0] <= 0 or image.size[1] <= 0 for image in images):
        harness._fail("WoodUV texture image is not loaded")
        return
    harness._pass("WoodBall2 has the connected WoodUV texture graph")


def _check_openpbr(adapter) -> None:
    obj = adapter._find_object_by_prim("/World/OpenPBRTest")
    material = obj.data.materials[0] if obj and obj.data and obj.data.materials else None
    if material is None or material.get("usd_material_path") != "/World/Looks/OpenPBRPurple":
        harness._fail("OpenPBRTest is missing its OpenPBRPurple binding")
        return
    if _principled_node(material) is None:
        harness._fail("OpenPBRPurple did not translate to a Principled node")
        return
    harness._pass("OpenPBRTest has its translated OpenPBR material")


def _check_bishop_import() -> None:
    bishop_objects = [
        obj
        for obj in bpy.data.objects
        if obj.get("usd_prim_path", "") == "/World/Bishop"
        or obj.get("usd_prim_path", "").startswith("/World/Bishop/")
    ]
    if bishop_objects:
        harness._pass(f"Bishop reference imported {len(bishop_objects)} objects")
    else:
        harness._fail("Bishop reference produced no Blender objects")


def _inspect_scene(last_seq: int) -> None:
    from usd_connect.blender_adapter import BlenderAdapter

    harness._pass(f"receiver settled at sequence {last_seq}")
    adapter = BlenderAdapter()
    _check_base_transforms(adapter)
    _check_chair(adapter)
    _check_woodball2(adapter)
    _check_openpbr(adapter)
    _check_bishop_import()


def _poll() -> float | None:
    global settled_at

    dispatcher = receiver_addon._DISPATCHER
    last_seq = dispatcher.last_seq if dispatcher is not None else 0
    receiver = receiver_addon._RECEIVER
    if receiver is not None and receiver.auth_rejected:
        harness._fail("receiver authentication rejected")
        harness.done()
        return None
    if last_seq >= EXPECTED_SEQ:
        if settled_at is None:
            settled_at = time.monotonic()
            return 1.0
        _inspect_scene(last_seq)
        harness.done()
        return None
    if time.monotonic() >= deadline:
        harness._fail(f"timed out at sequence {last_seq}/{EXPECTED_SEQ}")
        harness.done()
        return None
    return 0.25


if _setup():
    bpy.app.timers.register(_poll, first_interval=0.25)
