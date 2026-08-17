"""Validate and render a VFS-flattened MaterialX scene in Blender."""

import argparse
import hashlib
import json
import math
import os
import sys
import traceback

import bpy
from mathutils import Vector


def _args():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene", required=True)
    parser.add_argument("--addon", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--render", required=True)
    return parser.parse_args(argv)


def _look_at(obj, point):
    obj.rotation_euler = (Vector(point) - obj.location).to_track_quat("-Z", "Y").to_euler()


def _add_light(light_type, location, energy, size=1.0):
    data = bpy.data.lights.new(f"Test{light_type}", light_type)
    data.energy = energy
    if light_type == "AREA":
        data.shape = "DISK"
        data.size = size
    obj = bpy.data.objects.new(data.name, data)
    bpy.context.collection.objects.link(obj)
    obj.location = location
    return obj


def _render(meshes, output_path):
    points = [obj.matrix_world @ Vector(corner) for obj in meshes for corner in obj.bound_box]
    minimum = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
    maximum = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
    center = (minimum + maximum) * 0.5
    radius = max((maximum - minimum).length * 0.5, 1e-4)
    print(
        "MaterialX render bounds:",
        tuple(minimum),
        tuple(maximum),
        "center=",
        tuple(center),
        "radius=",
        radius,
        flush=True,
    )

    camera_data = bpy.data.cameras.new("MaterialXTestCamera")
    camera = bpy.data.objects.new(camera_data.name, camera_data)
    bpy.context.collection.objects.link(camera)
    camera.location = center + Vector((radius * 2.2, -radius * 3.4, radius * 1.8))
    _look_at(camera, center)
    camera_data.lens = 58
    camera_data.clip_start = max(radius * 0.01, 1e-6)
    camera_data.clip_end = max(radius * 100.0, 10.0)
    bpy.context.scene.camera = camera

    sun = _add_light("SUN", center + Vector((0, 0, radius * 4)), 2.5)
    sun.rotation_euler = (math.radians(35), math.radians(-25), math.radians(-35))
    key = _add_light(
        "AREA",
        center + Vector((-radius * 2.5, -radius * 2.5, radius * 3.0)),
        900.0 * radius * radius,
        radius * 2.0,
    )
    _look_at(key, center)

    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = 512
    scene.render.resolution_y = 512
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.film_transparent = True
    scene.render.filepath = output_path
    scene.view_settings.look = "AgX - Medium High Contrast"
    bpy.ops.render.render(write_still=True)

    render = bpy.data.images.load(output_path, check_existing=False)
    pixels = list(render.pixels)
    pixel_bytes = bytes(max(0, min(255, round(value * 255.0))) for value in pixels)
    visible = [
        pixels[index : index + 3] for index in range(0, len(pixels), 4) if pixels[index + 3] > 0.1
    ]
    assert visible, "render contains no opaque object pixels"
    luminance = [0.2126 * p[0] + 0.7152 * p[1] + 0.0722 * p[2] for p in visible]
    channels = [value for pixel in visible for value in pixel]
    return {
        "visible_pixels": len(visible),
        "mean_luminance": sum(luminance) / len(luminance),
        "luminance_range": max(luminance) - min(luminance),
        "channel_range": max(channels) - min(channels),
        "pixel_digest": hashlib.sha256(pixel_bytes).hexdigest(),
    }


def main():
    args = _args()
    result = {"status": "FAIL"}
    try:
        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete(use_global=False)
        bpy.ops.preferences.addon_install(filepath=args.addon, overwrite=True)
        bpy.ops.preferences.addon_enable(module="usd_connect")
        scene = bpy.context.scene
        scene.usd_connect_live_auto_start_emitter = False
        scene.usd_connect_live_auto_start_receiver = False
        assert bpy.ops.usd_connect.import_with_hook(filepath=args.scene) == {"FINISHED"}

        material = bpy.data.materials.get("Tiled_Brass")
        assert material is not None and material.node_tree is not None
        nodes = list(material.node_tree.nodes)
        image_nodes = [node for node in nodes if node.bl_idname == "ShaderNodeTexImage"]
        image_paths = [node.image.filepath if node.image else "" for node in image_nodes]
        print(
            "MaterialX nodes:",
            [(node.name, node.bl_idname) for node in nodes],
            "images=",
            image_paths,
            flush=True,
        )
        assert len(image_nodes) >= 2
        assert all(path and os.path.isfile(path) for path in image_paths)

        meshes = [obj for obj in bpy.data.objects if obj.type == "MESH"]
        assert meshes
        bound = [
            obj for obj in meshes if material.name in {slot.name for slot in obj.material_slots}
        ]
        assert bound, "Tiled_Brass is not assigned to imported geometry"
        links = list(material.node_tree.links)
        image_links = [link for link in links if link.from_node in image_nodes]
        assert len(nodes) >= 8
        assert len(image_links) >= 3

        render_metrics = _render(bound, args.render)
        assert render_metrics["visible_pixels"] > 5000
        assert 0.02 < render_metrics["mean_luminance"] < 0.98
        assert render_metrics["luminance_range"] > 0.1
        result = {
            "status": "PASS",
            "material": material.name,
            "node_count": len(nodes),
            "link_count": len(links),
            "image_paths": image_paths,
            **render_metrics,
        }
    except Exception as exc:
        traceback.print_exc()
        result["reason"] = str(exc)
    with open(args.out, "w", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    if result["status"] != "PASS":
        raise SystemExit(1)


main()
