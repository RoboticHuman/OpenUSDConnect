"""Leader-side Blender script for the two-Blender playback test.

Creates a local sphere with translate keyframes at frames 1 / 12 / 24,
claims the playback-leader role on the server, then scrubs through the
frames. The follower script (test_playback_follower.py) runs in a
sibling Blender process and verifies its own ``frame_current`` follows.

Verifies:
  * Fix 1: ``frame_change_post`` → ``PlaybackControl(set_time, ...)``
    propagates the leader's playhead to the server.
  * (Implicitly) Fix 2: F-curve-driven depsgraph evals during scrub do
    not flood the wire with default-time TRS overwrites — the follower
    relies on its own local F-curve evaluation for the sphere's pose;
    if Fix 2 were broken, the leader would clobber the follower's
    static-time TRS, but the static pose would lose to the F-curve on
    the next depsgraph tick anyway, so this script only directly proves
    Fix 1's frame propagation. Fix 2 is observable via the server log.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import bpy  # noqa: E402

from helpers import TestHarness  # noqa: E402

harness = TestHarness("PLAYBACK_LEADER")
_step = 0


def _build_sphere_with_keyframes():
    """Create /World/TestSphere with translate keyframes at 1, 12, 24."""
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
    global _step
    if _step == 0:
        harness.setup()
        _build_sphere_with_keyframes()
        harness.log("Built sphere with keyframes at frames 1, 12, 24")
        _step = 1
        return 4.0  # let follower connect + complete its own setup
    if _step == 1:
        result = bpy.ops.usd_connect.claim_playback()
        harness.log(f"Claim playback: {result}")
        _step = 2
        return 1.5  # wait for server PlaybackClaimed + PlaybackState round-trip
    if _step == 2:
        if not bpy.context.scene.usd_connect_playback_is_leader:
            harness._fail(
                "Did not become playback leader after claim "
                f"(leader_id={bpy.context.scene.usd_connect_playback_leader_id!r})",
            )
            harness.done()
            return None
        harness._pass("Became playback leader")
        bpy.context.scene.frame_set(12)
        harness.log("Scrubbed to frame 12")
        _step = 3
        return 2.0
    if _step == 3:
        bpy.context.scene.frame_set(24)
        harness.log("Scrubbed to frame 24")
        _step = 4
        return 3.0  # give follower time to catch up before we quit
    harness.done()
    return None


bpy.app.timers.register(_run, first_interval=2.0)
