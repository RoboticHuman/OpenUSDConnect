"""T1: Bishop — MaterialX with NodeGraph texture connections.

Verifies multi-node shader network, texture loading, and connections
through Mix/HueSat chain from diffuse texture to Base Color.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from helpers import PROJECT_ROOT, TestHarness

ASSET = os.path.join(
    PROJECT_ROOT, "assets", "full_assets", "OpenChessSet", "assets", "Bishop", "Bishop.usd",
)

harness = TestHarness("BISHOP")

_step = 0
def _run():
    global _step
    if _step == 0:
        harness.setup()
        _step = 1
        return 2.0
    elif _step == 1:
        harness.send_reference("/World/Bishop", ASSET, "/Bishop")
        _step = 2
        return 8.0
    elif _step == 2:
        # M_Bishop_B: MaterialX with texture
        harness.check_material("M_Bishop_B", path_contains="Bishop",
                               min_nodes=5, base_color_linked=True)
        harness.check_texture("M_Bishop_B", "diffuse", loaded=True)
        harness.check_connection("M_Bishop_B", "Base Color", linked=True, from_type="MIX")

        # M_Bishop_W: second variant material
        harness.check_material("M_Bishop_W", min_nodes=5, base_color_linked=True)
        harness.check_texture("M_Bishop_W", "diffuse", loaded=True)

        # Binding
        harness.check_binding("Render", "M_Bishop_B")

        # Shader maps seeded for reverse path
        harness.check_shader_maps_seeded("Bishop")

        harness.done()
        return None

import bpy  # noqa: E402

bpy.app.timers.register(_run, first_interval=2.0)
