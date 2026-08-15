"""USD Connect Blender addon for real-time USD sync via OpenUSDConnect.

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

# Ensure the addon directory is on sys.path so vendored openusdconnect is importable.
# Purge openusdconnect modules loaded from a different path (e.g. the uv dev
# environment) so the bundled copy is used.  Keep modules already loaded from
# this addon's directory (e.g. during hot-reload).
_addon_dir = os.path.dirname(os.path.abspath(__file__))
_bundled_prefix = os.path.join(_addon_dir, "openusdconnect")
# On reload, also purge bundled openusdconnect modules so the freshly-extracted
# addon files load otherwise the package __init__'s `from .codec import ...`
# resolves against the stale cached submodule and misses newly-added symbols.
_is_addon_reload = "capture" in dir()
if os.path.isdir(_bundled_prefix):
    for _k in [k for k in sys.modules if k.startswith("openusdconnect")]:
        _mod = sys.modules[_k]
        _mod_file = getattr(_mod, "__file__", "") or ""
        if _is_addon_reload or not _mod_file.startswith(_bundled_prefix):
            del sys.modules[_k]
    if _addon_dir not in sys.path:
        sys.path.insert(0, _addon_dir)

from openusdconnect.client_id import make_stable_client_id

# Stable client ID based on username + hostname. Persists across sessions for
# authentication, producer replay, and collaboration attribution.
STABLE_CLIENT_ID = make_stable_client_id("blender")

# Session-level origin identifier shared by emitter and receiver connections
# from this Blender instance. Durable events return in the complete commit
# stream; Blender's apply guard prevents those records from being re-emitted.
# Random per session unlike STABLE_CLIENT_ID, this changes on restart so
# the server can distinguish multiple sessions from the same machine.
SESSION_ORIGIN = f"blender-{uuid.uuid4().hex[:12]}"

# Make bundled pxr module available if Blender provides it
try:
    import bpy

    bpy.utils.expose_bundled_modules()
except Exception:
    pass

# Reload addon submodules when the addon is re-enabled (F3 → Reload Scripts
# or disable/enable toggle).  Order matters: receiver_addon imports
# BlenderAdapter, so blender_adapter must be reloaded first.
if _is_addon_reload:
    import importlib

    if "live_discovery" in globals():
        importlib.reload(globals()["live_discovery"])
    importlib.reload(shader_mapper)  # noqa: F821
    importlib.reload(blender_adapter)  # noqa: F821
    importlib.reload(capture)  # noqa: F821
    importlib.reload(receiver_addon)  # noqa: F821
    importlib.reload(ui)  # noqa: F821

from . import live_discovery as live_discovery
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
