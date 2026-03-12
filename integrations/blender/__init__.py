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

# Ensure the addon directory is on sys.path so vendored openusdconnect is importable
_addon_dir = os.path.dirname(os.path.abspath(__file__))
if _addon_dir not in sys.path:
    sys.path.insert(0, _addon_dir)

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
    # Reload vendored core library first (dependency)
    from . import openusdconnect as _ouc_pkg
    for _sub in ("protocol", "transport", "event_apply", "emitter", "receiver", "adapters", "server"):
        _mod = getattr(_ouc_pkg, _sub, None)
        if _mod is not None:
            importlib.reload(_mod)
    # Then reload addon modules
    importlib.reload(capture)        # noqa: F821
    importlib.reload(receiver_addon) # noqa: F821
    importlib.reload(blender_adapter) # noqa: F821
    importlib.reload(ui)             # noqa: F821

from . import capture
from . import receiver_addon
from . import blender_adapter
from . import ui


def register():
    capture.register()
    receiver_addon.register()
    ui.register()


def unregister():
    ui.unregister()
    receiver_addon.unregister()
    capture.unregister()
