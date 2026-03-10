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

from . import capture
from . import receiver_addon
from . import ui


def register():
    capture.register()
    receiver_addon.register()
    ui.register()


def unregister():
    ui.unregister()
    receiver_addon.unregister()
    capture.unregister()
