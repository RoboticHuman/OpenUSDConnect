"""Compare native and tagged imports of a UsdPreviewSurface texture graph."""

from __future__ import annotations

import argparse
import json
import sys

import bpy


def _args():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene", required=True)
    parser.add_argument("--addon", required=True)
    parser.add_argument("--out", required=True)
    return parser.parse_args(argv)


def _metrics():
    material = next(mat for mat in bpy.data.materials if mat.name.startswith("PreviewMat"))
    nodes = list(material.node_tree.nodes)
    return {
        "nodes": len(nodes),
        "links": len(material.node_tree.links),
        "image_nodes": sum(node.bl_idname == "ShaderNodeTexImage" for node in nodes),
        "images": sorted(image.name for image in bpy.data.images if image.source == "FILE"),
    }


def main():
    args = _args()

    assert bpy.ops.wm.usd_import(filepath=args.scene) == {"FINISHED"}
    native = _metrics()

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for collection in (bpy.data.meshes, bpy.data.materials, bpy.data.images):
        for datablock in list(collection):
            if datablock.users == 0:
                collection.remove(datablock)
    bpy.ops.preferences.addon_install(filepath=args.addon, overwrite=True)
    bpy.ops.preferences.addon_enable(module="usd_connect")
    scene = bpy.context.scene
    scene.usd_connect_live_auto_start_emitter = False
    scene.usd_connect_live_auto_start_receiver = False
    assert bpy.ops.usd_connect.import_with_hook(filepath=args.scene) == {"FINISHED"}
    tagged = _metrics()

    result = {"native": native, "tagged": tagged, "equal": native == tagged}
    with open(args.out, "w", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    print("OUC_PREVIEW_ENRICHMENT", json.dumps(result), flush=True)
    assert result["equal"], result


main()
