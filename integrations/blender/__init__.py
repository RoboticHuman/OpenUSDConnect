"""USD Connect — Blender addon for real-time USD sync via OpenUSDConnect.

Install via: Preferences > Add-ons > Install from Disk > select the zip.
The zip bundles the openusdconnect core library inside the addon directory.
"""

bl_info = {
    "name": "USD Connect",
    "author": "OpenUSDConnect",
    "version": (0, 1, 0),
    "blender": (4, 4, 0),
    "location": "View3D > Sidebar > USD Connect",
    "description": "Real-time USD sync: capture and receive transform edits over the network",
    "category": "Import-Export",
}

import os
import sys
import uuid

# Session-level origin identifier shared by emitter and receiver connections
# from this Blender instance.  The server uses it to suppress echo — events
# are not broadcast back to connections with the same origin.
SESSION_ORIGIN = f"blender-{uuid.uuid4().hex[:12]}"

# Ensure the addon directory is on sys.path so vendored openusdconnect is importable.
# Purge openusdconnect modules loaded from a different path (e.g. the uv dev
# environment) so the bundled copy is used.  Keep modules already loaded from
# this addon's directory (e.g. during hot-reload).
_addon_dir = os.path.dirname(os.path.abspath(__file__))
_bundled_prefix = os.path.join(_addon_dir, "openusdconnect")
for _k in [k for k in sys.modules if k.startswith("openusdconnect")]:
    _mod = sys.modules[_k]
    _mod_file = getattr(_mod, "__file__", "") or ""
    if not _mod_file.startswith(_bundled_prefix):
        del sys.modules[_k]
if _addon_dir not in sys.path:
    sys.path.insert(0, _addon_dir)

# io_blender_mtlx (Activision MaterialX node handlers) lives under vendor/
_vendor_mtlx = os.path.join(_addon_dir, "vendor")
if _vendor_mtlx not in sys.path:
    sys.path.insert(0, _vendor_mtlx)

# Make bundled pxr module available if Blender provides it
try:
    import bpy

    bpy.utils.expose_bundled_modules()
except Exception:
    pass

# Reload submodules when the addon is re-enabled (F3 → Reload Scripts,
# or disable/enable toggle).  Without this, Blender only notices changes
# to __init__.py — edits in capture.py, receiver_addon.py, etc. are
# silently ignored until a full restart.
if "capture" in locals():
    import importlib

    # Reload vendored core library first (dependency).
    # The package itself must be reloaded so submodule references are fresh,
    # then each submodule is reloaded so `from .protocol import X` picks up
    # new symbols added since the last load.
    from . import openusdconnect as _ouc_pkg

    importlib.reload(_ouc_pkg)
    _subs = ("protocol", "transport", "event_apply", "emitter", "receiver", "adapters", "server")
    for _sub in _subs:
        _mod = getattr(_ouc_pkg, _sub, None)
        if _mod is not None:
            importlib.reload(_mod)
    # Then reload addon modules (shader_mapper before blender_adapter which imports it)
    importlib.reload(capture)  # noqa: F821
    importlib.reload(receiver_addon)  # noqa: F821
    importlib.reload(shader_mapper)  # noqa: F821
    importlib.reload(blender_adapter)  # noqa: F821
    importlib.reload(ui)  # noqa: F821

from . import capture
from . import receiver_addon
from . import shader_mapper as shader_mapper
from . import blender_adapter as blender_adapter
from . import ui


def register():
    capture.register()
    receiver_addon.register()
    ui.register()


def unregister():
    # Grab the timer ref before module reload creates a new function object.
    import bpy
    timer = getattr(capture, "_timer_tick", None)
    ui.unregister()
    receiver_addon.unregister()
    capture.unregister()
    if timer is not None and bpy.app.timers.is_registered(timer):
        bpy.app.timers.unregister(timer)
