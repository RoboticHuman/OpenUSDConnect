"""Verify surviving USD samples and Blender's static pose after replay."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import bpy  # noqa: E402
from helpers import TestHarness  # noqa: E402

harness = TestHarness("TIMESAMPLE_OBSERVER")
_step = 0
_poll_count = 0
_MAX_POLLS = 30

# Must match what the pytest wrapper sends.
_PRIM_PATH = "/World/AnimSphere"
_ERASE_LATEST = "--erase-latest" in sys.argv
_EXPECTED_LOCATION = (10.0 if _ERASE_LATEST else 20.0, 0.0, 0.0)


def _find_blender_object(prim_path: str):
    for obj in bpy.data.objects:
        if obj.get("usd_prim_path", "") == prim_path:
            return obj
    return None


def _observe_and_report():
    from usd_connect import receiver_addon

    stage = receiver_addon._DISPATCHER.mirror_stage
    attr = stage.GetAttributeAtPath(_PRIM_PATH + ".xformOp:translate")
    expected_times = [1.0, 12.0] if _ERASE_LATEST else [1.0, 12.0, 24.0]
    if attr and attr.GetTimeSamples() == expected_times:
        harness._pass(f"Mirror sample times == {expected_times}")
    else:
        harness._fail(f"Mirror sample times != {expected_times}")

    obj = _find_blender_object(_PRIM_PATH)
    if obj is None:
        harness._fail(f"No Blender object found at usd_prim_path={_PRIM_PATH!r}")
        return False

    harness._pass(f"Blender object exists: {obj.name} (type={obj.type})")

    loc = tuple(round(float(c), 3) for c in obj.location)
    harness.log(f"  observed obj.location = {loc}")

    if loc == _EXPECTED_LOCATION:
        harness._pass(f"obj.location == {_EXPECTED_LOCATION}")
    else:
        harness._fail(
            f"obj.location == {loc}, expected {_EXPECTED_LOCATION}",
        )

    # Replay updates the displayed pose without creating animation curves.
    has_fcurves = bool(
        obj.animation_data and obj.animation_data.action
        and len(obj.animation_data.action.fcurves) > 0
    )
    if has_fcurves:
        harness._fail("Sample replay created unexpected F-curves")
    else:
        harness._pass("Sample replay did not create F-curves")

    return True


def _run():
    global _step, _poll_count
    if _step == 0:
        harness.setup()
        harness.log(
            "Receiver connected. Waiting for event-log replay to deliver "
            f"the headless-emit events for {_PRIM_PATH}...",
        )
        _step = 1
        return 1.0
    if _step == 1:
        _poll_count += 1
        obj = _find_blender_object(_PRIM_PATH)
        if obj is not None:
            harness.log(f"Sphere appeared after {_poll_count} polls; settling 1s")
            _step = 2
            return 1.0  # settle so all events have applied
        if _poll_count >= _MAX_POLLS:
            harness._fail(f"Sphere never appeared after {_poll_count} polls")
            harness.done()
            return None
        return 1.0
    if _step == 2:
        _observe_and_report()
        harness.done()
        return None
    return None


bpy.app.timers.register(_run, first_interval=2.0)
