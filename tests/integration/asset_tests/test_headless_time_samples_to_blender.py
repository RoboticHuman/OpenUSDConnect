"""Observer-side Blender script for the headless-emit time-samples test.

Counterpart to ``test_assets.py::test_headless_time_samples_to_blender``.
The pytest wrapper sends a sequence of time-sampled ``set_xform_trs``
events to the server BEFORE this Blender starts — the events live in
the event log. When this script connects via the addon's receiver, the
server replays them; the addon applies them; this script then dumps
the observed state.

What this is verifying:

  * Protocol layer end-to-end: time-sampled events survive encode →
    server log → replay → decode → apply.
  * The "Q1 gap": ``BlenderAdapter.set_xform_trs`` currently ignores
    the ``time`` argument and just sets ``obj.location`` at default
    time, so each time-sampled event overwrites the previous and the
    sphere ends at the LAST received position with no F-curves.

What this is intentionally NOT verifying:

  * Time samples on the receiver-side mirror USD stage. For a
    Blender (DCC-backed) receiver, ``BlenderAdapter`` writes to Blender
    objects, not USD. The mirror stage is fed by capture's depsgraph
    roundtrip, which authors only default-time opinions. Time samples
    don't land on the Blender receiver's mirror by design — animation
    data for Blender comes from local USD import (Blender Action
    F-curves), not from the wire. For stage-backed receivers
    (Unreal via UsdStageAdapter, headless scripts), time samples DO
    land on the consumer's stage via the adapter's apply path
    (``event_apply`` honors ``time``); those would be tested
    separately if/when wired up.

Outputs PASS / FAIL based on whether observed state matches the
documented current behavior. If/when Q1 is implemented (incoming
time-sampled events insert Blender keyframes), this test will need
to be updated.
"""

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
_EXPECTED_LOCATION = (20.0, 0.0, 0.0)  # latest time-sample's position


def _find_blender_object(prim_path: str):
    for obj in bpy.data.objects:
        if obj.get("usd_prim_path", "") == prim_path:
            return obj
    return None


def _observe_and_report():
    obj = _find_blender_object(_PRIM_PATH)
    if obj is None:
        harness._fail(f"No Blender object found at usd_prim_path={_PRIM_PATH!r}")
        return False

    harness._pass(f"Blender object exists: {obj.name} (type={obj.type})")

    loc = tuple(round(float(c), 3) for c in obj.location)
    harness.log(f"  observed obj.location = {loc}")

    if loc == _EXPECTED_LOCATION:
        harness._pass(
            f"obj.location == {_EXPECTED_LOCATION} (latest time-sample's value, "
            f"static-pose semantics — confirms BlenderAdapter ignores `time`)",
        )
    else:
        harness._fail(
            f"obj.location == {loc}, expected {_EXPECTED_LOCATION}",
        )

    # Q1 gap: no F-curves should exist on this object — incoming
    # time-sampled events don't get translated into Blender keyframes.
    has_fcurves = bool(
        obj.animation_data and obj.animation_data.action
        and len(obj.animation_data.action.fcurves) > 0
    )
    if has_fcurves:
        harness._fail(
            "Unexpectedly found F-curves on the receiver's sphere — "
            "Q1 (Blender keyframe insertion on receive) is not implemented yet, "
            "so F-curves shouldn't be there. If this fails, either Q1 was "
            "implemented or Blender is auto-keying.",
        )
    else:
        harness._pass("No F-curves on receiver's sphere (Q1 gap confirmed)")

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
