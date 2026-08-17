"""Exercise the packaged add-on's OpenPBR translation dependency."""

import argparse
import json
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
parser = argparse.ArgumentParser()
parser.add_argument("--addon", required=True)
parser.add_argument("--out", required=True)
args = parser.parse_args(argv)

bpy.ops.preferences.addon_install(filepath=args.addon, overwrite=True)
bpy.ops.preferences.addon_enable(module="usd_connect")

from usd_connect.shader_mapper import create_default_registry

material = bpy.data.materials.new("PackagedOpenPBR")
material.use_nodes = True
tree = material.node_tree
tree.nodes.clear()
mapper = create_default_registry().get("ND_open_pbr_surface_surfaceshader")
assert mapper is not None
nodes, input_map, output_map = mapper.create_network(
    tree,
    {
        "base_weight": 1.0,
        "base_color": [0.2, 0.4, 0.8],
        "base_metalness": 0.25,
        "specular_weight": 1.0,
        "specular_roughness": 0.3,
    },
)
result = {
    "nodes": len(nodes),
    "inputs": sorted(input_map),
    "outputs": sorted(output_map),
}
assert result["nodes"] >= 1
assert "base_color" in input_map
assert output_map
with open(args.out, "w", encoding="utf-8") as stream:
    json.dump(result, stream, indent=2)
