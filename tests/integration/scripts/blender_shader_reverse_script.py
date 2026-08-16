"""Edit single- and multi-node shader sockets and publish them through capture."""

from __future__ import annotations

import argparse
import os
import sys

import bpy

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_SCRIPTS_DIR)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
_VENV_SITE_PACKAGES = os.path.join(PROJECT_ROOT, ".venv", "Lib", "site-packages")
if os.path.isdir(_VENV_SITE_PACKAGES) and _VENV_SITE_PACKAGES not in sys.path:
    sys.path.append(_VENV_SITE_PACKAGES)
for _module_name in [name for name in sys.modules if name.startswith("openusdconnect")]:
    del sys.modules[_module_name]

from integrations.blender import capture
from integrations.blender.capture import BlenderStageAuthor
from openusdconnect.emitter import NoticeEmitter
from openusdconnect.sender import EventSender

SINGLE_PATH = "/World/Looks/Single/Surface"
MULTI_PATH = "/World/Looks/Multi/Surface"

_pending_socket = None
_pending_value = None
_pending_material = None


class OUC_TEST_OT_change_shader_socket(bpy.types.Operator):
    bl_idname = "ouc_test.change_shader_socket"
    bl_label = "Change shader socket"

    def execute(self, _context):
        _pending_socket.default_value = _pending_value
        _pending_material.node_tree.update_tag()
        _pending_material.update_tag()
        return {"FINISHED"}


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--base", required=True)
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    return parser.parse_args(argv)


def _new_material(name: str):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    material.node_tree.nodes.clear()
    return material


def _assign_material(material, name: str) -> None:
    mesh = bpy.data.meshes.new(f"{name}Mesh")
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    mesh.materials.append(material)


def _single_node_setup(author: BlenderStageAuthor):
    material = _new_material("SingleReverse")
    material["usd_material_path"] = "/World/Looks/Single"
    node = material.node_tree.nodes.new("ShaderNodeBsdfPrincipled")
    node["usd_shader_path"] = SINGLE_PATH
    node["usd_shader_id"] = "UsdPreviewSurface"
    node.inputs["Roughness"].default_value = 0.2
    _assign_material(material, "SingleReverseObject")

    mapper = author._shader_registry.get("UsdPreviewSurface")
    author._last_shader_values[SINGLE_PATH] = mapper.read_all_inputs(node)
    return material, node.inputs["Roughness"]


def _multi_node_setup(author: BlenderStageAuthor):
    material = _new_material("MultiReverse")
    material["usd_material_path"] = "/World/Looks/Multi"
    mapper = author._shader_registry.get("ND_standard_surface_surfaceshader")
    nodes, input_map, _outputs = mapper.create_network(
        material.node_tree,
        {
            "base_color": [0.2, 0.3, 0.4],
            "metalness": 0.15,
            "specular_roughness": 0.35,
        },
    )
    input_map["base_color"].default_value = (0.2, 0.3, 0.4, 1.0)
    input_map["metalness"].default_value = 0.15
    input_map["specular_roughness"].default_value = 0.35
    assert len(nodes) == 5
    assert input_map["base_color"].node is not nodes[0]
    assert nodes[0].inputs["Base Color"].is_linked
    nodes[0]["usd_shader_path"] = MULTI_PATH
    nodes[0]["usd_shader_id"] = "ND_standard_surface_surfaceshader"
    author._shader_input_maps[MULTI_PATH] = input_map
    author._last_shader_values[MULTI_PATH] = mapper.read_all_inputs(input_map=input_map)
    _assign_material(material, "MultiReverseObject")
    return material, input_map["base_color"]


def _change_socket(material, socket, value) -> None:
    global _pending_material, _pending_socket, _pending_value
    _pending_material = material
    _pending_socket = socket
    _pending_value = value
    capture._state._last_send_time = 0.0
    result = bpy.ops.ouc_test.change_shader_socket()
    if result != {"FINISHED"}:
        raise RuntimeError(f"shader edit operator failed: {result}")
    bpy.context.view_layer.update()


def main() -> None:
    args = _args()
    author = BlenderStageAuthor(base_usd_path=args.base)
    author.enabled = True
    single_material, single_socket = _single_node_setup(author)
    multi_material, multi_socket = _multi_node_setup(author)

    # Clear setup notifications before observing the two deliberate edits.
    bpy.context.view_layer.update()
    emitter = NoticeEmitter(author.stage)
    sender = EventSender(
        "127.0.0.1",
        args.port,
        client_id="blender-shader-reverse",
        origin="blender-shader-reverse",
        session_id="blender-shader-reverse-session",
    )
    if not sender.connect(timeout=5):
        raise RuntimeError("could not connect shader reverse sender")

    capture._state.author = author
    capture._state.notice_emitter = emitter
    capture._state.sender = sender
    capture._state._last_seen_frame = bpy.context.scene.frame_current
    capture._remove_handler()
    bpy.app.handlers.depsgraph_update_post.append(capture._depsgraph_handler)
    bpy.utils.register_class(OUC_TEST_OT_change_shader_socket)
    try:
        _change_socket(single_material, single_socket, 0.61)
        if not sender.flush(timeout=5):
            raise RuntimeError("single-node shader event did not flush")

        _change_socket(multi_material, multi_socket, (0.8, 0.25, 0.1, 1.0))
        if not sender.flush(timeout=5):
            raise RuntimeError("multi-node shader event did not flush")
    finally:
        capture._remove_handler()
        bpy.utils.unregister_class(OUC_TEST_OT_change_shader_socket)
        sender.disconnect()
        emitter.cleanup()
        capture._state.author = None
        capture._state.notice_emitter = None
        capture._state.sender = None

    print("SHADER_REVERSE_SYNC_OK", flush=True)


if __name__ == "__main__":
    main()
