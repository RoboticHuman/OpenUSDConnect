"""Follower-side Blender script for the two-Blender playback test.

Creates a local sphere with the SAME translate keyframes the leader will
use (frames 1 / 12 / 24 → positions 0/10/20 on X). Then polls
``scene.frame_current`` until it advances to 24 — proving that the
leader's `PlaybackControl(set_time, ...)` broadcasts reached us and the
receiver applied them via `scene.frame_set`.

Also asserts the sphere ends at the F-curve-evaluated pose for frame 24,
proving the follower's local F-curves drive the visible state (we don't
need the leader's transform events for this — only the playhead sync).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import bpy  # noqa: E402

from helpers import TestHarness  # noqa: E402

harness = TestHarness("PLAYBACK_FOLLOWER")
_step = 0
_poll_count = 0
_MAX_POLLS = 30


def _build_sphere_with_keyframes():
    """Create the same /World/TestSphere with matching local F-curves."""
    bpy.ops.mesh.primitive_uv_sphere_add(location=(0, 0, 0))
    sphere = bpy.context.active_object
    sphere.name = "TestSphere"
    sphere["usd_prim_path"] = "/World/TestSphere"
    for frame, loc in [(1, (0.0, 0.0, 0.0)), (12, (10.0, 0.0, 0.0)), (24, (20.0, 0.0, 0.0))]:
        bpy.context.scene.frame_set(frame)
        sphere.location = loc
        sphere.keyframe_insert(data_path="location", frame=frame)
    bpy.context.scene.frame_set(1)


def _run():
    global _step, _poll_count
    if _step == 0:
        harness.setup()
        _build_sphere_with_keyframes()
        harness.log("Built local sphere with keyframes")
        _step = 1
        return 1.0
    if _step == 1:
        _poll_count += 1
        scene = bpy.context.scene
        cur = scene.frame_current
        leader = scene.usd_connect_playback_leader_id
        harness.log(
            f"poll {_poll_count}: frame_current={cur} leader={leader!r}",
        )
        if cur == 24:
            harness._pass(f"frame_current reached 24 after {_poll_count} polls")
            sphere = bpy.data.objects.get("TestSphere")
            if sphere is None:
                harness._fail("TestSphere object missing on follower")
            else:
                loc = tuple(round(x, 3) for x in sphere.location)
                expected = (20.0, 0.0, 0.0)
                if loc == expected:
                    harness._pass(f"Sphere at expected position {loc}")
                else:
                    harness._fail(
                        f"Sphere at {loc}, expected {expected} "
                        f"(F-curve eval at frame 24)",
                    )
            harness.done()
            return None
        if _poll_count >= _MAX_POLLS:
            harness._fail(
                f"Timed out waiting for frame=24; current={cur} "
                f"leader={leader!r}",
            )
            harness.done()
            return None
        return 1.0
    return None


bpy.app.timers.register(_run, first_interval=2.0)
