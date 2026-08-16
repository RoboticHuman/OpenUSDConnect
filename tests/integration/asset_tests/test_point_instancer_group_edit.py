"""A selected pawn PointInstancer can move as one semantic USD group."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import bpy
from helpers import PROJECT_ROOT, TestHarness

ASSET = os.path.join(
    PROJECT_ROOT,
    "assets",
    "full_assets",
    "OpenChessSet",
    "chess_set.usda",
)
ROOT_PATH = "/World/ChessSet"
PAWNS_PATH = "/World/ChessSet/Black/Pawns"

harness = TestHarness("POINT_INSTANCER_GROUP_EDIT")
_step = 0


def _run():
    global _step
    if _step == 0:
        harness.setup()
        _step = 1
        return 2.0
    if _step == 1:
        harness.send_reference(ROOT_PATH, ASSET, "/ChessSet")
        _step = 2
        return 8.0
    if _step == 2:
        from usd_connect import receiver_addon

        pawns = receiver_addon._ADAPTER._find_object_by_prim(PAWNS_PATH)
        if pawns is None:
            harness._fail(f"missing PointInstancer object {PAWNS_PATH}")
            _step = 99
            return 1.0
        bpy.ops.object.select_all(action="DESELECT")
        pawns.select_set(True)
        bpy.context.view_layer.objects.active = pawns
        pawns.location.x += 0.25
        bpy.context.view_layer.update()
        _step = 3
        return 4.0
    if _step == 3:
        from usd_connect import receiver_addon

        attr = receiver_addon._MIRROR_STAGE.GetAttributeAtPath(
            f"{PAWNS_PATH}.xformOp:translate",
        )
        value = attr.Get() if attr and attr.IsValid() else None
        if value is not None and abs(float(value[0]) - 0.25) < 1e-4:
            harness._pass("whole pawn group transform reached USD")
        else:
            harness._fail(f"pawn group translate is {value}")
        _step = 99
        return 1.0

    harness.done()
    return None


bpy.app.timers.register(_run, first_interval=2.0)
