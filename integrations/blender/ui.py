"""Combined sidebar panel for USD Connect Blender addon."""

from __future__ import annotations

try:
    import bpy

    BPY_AVAILABLE = True
except Exception:
    BPY_AVAILABLE = False

try:
    from pxr import Usd  # noqa: F401

    PXR_AVAILABLE = True
except Exception:
    PXR_AVAILABLE = False


def _draw_import_section(layout, scene):
    box = layout.box()
    box.label(text="Import", icon="IMPORT")
    box.operator("usd_connect.import_with_hook")
    box.operator("usd_connect.print_import_props", icon="CONSOLE")


def _draw_capture_section(layout, scene):
    box = layout.box()
    box.label(text="Local Capture", icon="REC")
    box.prop(scene, "usd_connect_base_usd_path")

    from . import capture

    author = capture._state.author
    running = bool(author and author.enabled)
    row = box.row()
    if running:
        row.operator("usd_connect.stop_capture", icon="PAUSE")
    else:
        row.operator("usd_connect.start_capture", icon="PLAY")

    if author is not None:
        sub = box.column(align=True)
        sub.label(text=f"Base: {author.base_usd_path}")
        sub.label(text=f"Tracked prims: {len(author._prim_refs)}")

    row = box.row(align=True)
    row.operator("usd_connect.emit_diff", icon="EXPORT")
    row.operator("usd_connect.clear_diff", icon="TRASH")

    col = box.column(align=True)
    col.prop(scene, "usd_connect_emit_to_file")
    if scene.usd_connect_emit_to_file:
        col.prop(scene, "usd_connect_emit_file_path")
    box.prop(scene, "usd_connect_coalesce_seconds")


def _draw_emitter_section(layout, scene):
    box = layout.box()
    box.label(text="Network Emitter", icon="EXPORT")
    col = box.column(align=True)
    col.prop(scene, "usd_connect_emit_host")
    col.prop(scene, "usd_connect_emit_port")
    col.prop(scene, "usd_connect_emit_hz")

    col = box.column(align=True)
    col.prop(scene, "usd_connect_auto_track")
    if scene.usd_connect_auto_track:
        col.prop(scene, "usd_connect_auto_track_root")

    row = box.row()
    if scene.usd_connect_net_emitter_running:
        row.operator("usd_connect.disconnect_emitter", icon="PAUSE")
        box.label(text="Emitter connected", icon="CHECKMARK")
    else:
        row.operator("usd_connect.connect_emitter", icon="PLAY")


def _draw_receiver_section(layout, scene):
    box = layout.box()
    box.label(text="Network Receiver", icon="IMPORT")
    col = box.column(align=True)
    col.prop(scene, "usd_connect_recv_host")
    col.prop(scene, "usd_connect_recv_port")

    row = box.row()
    if scene.usd_connect_recv_running:
        row.operator("usd_connect.stop_receiver", icon="PAUSE")
        box.label(text="Receiver running", icon="CHECKMARK")
        if hasattr(scene, "usd_connect_recv_last_seq"):
            box.label(text=f"Last seq: {scene.usd_connect_recv_last_seq}")
    else:
        row.operator("usd_connect.start_receiver", icon="PLAY")
        if hasattr(scene, "usd_connect_recv_last_seq") and scene.usd_connect_recv_last_seq > 0:
            box.label(text=f"Will sync from seq: {scene.usd_connect_recv_last_seq + 1}")
            box.operator("usd_connect.reset_receiver_seq", icon="LOOP_BACK")


class USD_CONNECT_PT_main_panel(bpy.types.Panel):
    bl_label = "USD Connect"
    bl_idname = "USD_CONNECT_PT_main_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "USD Connect"

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        if not PXR_AVAILABLE:
            layout.label(text="OpenUSD (pxr) not available", icon="ERROR")
            return

        _draw_import_section(layout, scene)
        _draw_capture_section(layout, scene)
        _draw_emitter_section(layout, scene)
        _draw_receiver_section(layout, scene)


_UI_CLASSES = (USD_CONNECT_PT_main_panel,)


def register():
    if BPY_AVAILABLE:
        for c in _UI_CLASSES:
            bpy.utils.register_class(c)


def unregister():
    if BPY_AVAILABLE:
        for c in reversed(_UI_CLASSES):
            bpy.utils.unregister_class(c)
