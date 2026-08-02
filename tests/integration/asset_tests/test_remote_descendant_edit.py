"""Remote reference descendants remain overrides when edited in Blender."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import bpy
from helpers import PROJECT_ROOT, TestHarness
from pxr import Sdf

ASSET = os.path.join(
    PROJECT_ROOT,
    "assets",
    "full_assets",
    "OpenChessSet",
    "assets",
    "Bishop",
    "Bishop.usd",
)
PRIM_PATH = "/World/Bishop/Geom/Render"

harness = TestHarness("REMOTE_DESCENDANT_EDIT")
_step = 0


def _run():
    global _step
    if _step == 0:
        harness.setup()
        _step = 1
        return 2.0
    if _step == 1:
        harness.send_reference("/World/Bishop", ASSET, "/Bishop")
        _step = 2
        return 8.0
    if _step == 2:
        from usd_connect import receiver_addon

        obj = receiver_addon._ADAPTER._find_object_by_prim(PRIM_PATH)
        if obj is None:
            harness._fail(f"missing imported object {PRIM_PATH}")
            _step = 99
            return 1.0
        obj.location.x += 1.0
        bpy.context.view_layer.update()
        _step = 3
        return 4.0
    if _step == 3:
        from usd_connect import capture, receiver_addon

        delta = capture._state.author.delta_layer
        specs = [
            delta.GetPrimAtPath(path) for path in ("/World/Bishop", "/World/Bishop/Geom", PRIM_PATH)
        ]
        if all(
            spec is not None and spec.specifier == Sdf.SpecifierOver and not spec.typeName
            for spec in specs
        ):
            harness._pass("remote hierarchy remains authored as over specs")
        else:
            harness._fail("remote hierarchy materialized local definitions")

        attr = receiver_addon._MIRROR_STAGE.GetAttributeAtPath(f"{PRIM_PATH}.xformOp:translate")
        value = attr.Get() if attr and attr.IsValid() else None
        if value is not None and abs(float(value[0]) - 1.0) < 1e-4:
            harness._pass("local descendant transform reached the layered mirror")
        else:
            harness._fail(f"layered mirror translate is {value}")
        _step = 99
        return 1.0

    harness.done()
    return None


bpy.app.timers.register(_run, first_interval=2.0)
