"""Diagnostic: import Bishop and dump the resulting Blender material tree.

Manual run (not part of the pytest suite):
    1. Start a server on port 7299 with a scratch base + log
    2. Launch Blender with this script:
       blender --python tests/integration/asset_tests/diag_bishop_tree.py \\
           -- --port 7299
    3. Inspect output: bishop_diag.json

Useful when investigating why a MaterialX scene's textures/connections
look wrong in Blender after import — the dump shows every node, every
link, and every texture image's resolved state.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import bpy
from helpers import PROJECT_ROOT, TestHarness

ASSET = os.path.join(
    PROJECT_ROOT, "assets", "full_assets", "OpenChessSet",
    "assets", "Bishop", "Bishop.usd",
)

harness = TestHarness("BISHOP_DIAG")

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
        out_path = os.path.join(PROJECT_ROOT, "bishop_diag.json")
        dump = {"_all_materials": [m.name for m in bpy.data.materials]}
        for mat in bpy.data.materials:
            if not mat.node_tree:
                continue
            entry = {"nodes": [], "links": []}
            for node in mat.node_tree.nodes:
                info = {"name": node.name, "type": node.type}
                if node.type == "TEX_IMAGE":
                    info["image"] = node.image.name if node.image else None
                    info["image_size"] = (
                        list(node.image.size) if node.image else None
                    )
                    info["colorspace"] = (
                        node.image.colorspace_settings.name
                        if node.image else None
                    )
                entry["nodes"].append(info)
            for link in mat.node_tree.links:
                entry["links"].append(
                    f"{link.from_node.name}.{link.from_socket.name}"
                    f" -> {link.to_node.name}.{link.to_socket.name}"
                )
            dump[mat.name] = entry
        with open(out_path, "w") as f:
            json.dump(dump, f, indent=2)
        harness._pass(f"Dump written to {out_path}")
        harness.done()
        return None

bpy.app.timers.register(_run, first_interval=2.0)
