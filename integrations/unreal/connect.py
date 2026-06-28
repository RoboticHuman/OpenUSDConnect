"""Launcher for OpenUSDConnect in Unreal Engine.

Run from the UE console (use this checkout's absolute path):
    py "<OpenUSDConnect>/integrations/unreal/connect.py"

To stop:
    py "<OpenUSDConnect>/integrations/unreal/disconnect.py"
"""

import os
import sys

# --- Configuration (edit these) -----------------------------------------
HOST = "127.0.0.1"
PORT = 7200
ROOT_LAYER = "test_scene.usda"
# ------------------------------------------------------------------------

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# Flush cached modules so code changes take effect without restarting UE
for _k in [k for k in sys.modules if "usd_connect" in k or "openusdconnect" in k]:
    del sys.modules[_k]

from integrations.unreal.usd_connect import start, status

start(HOST, PORT, ROOT_LAYER)
for k, v in status().items():
    print(f"  {k}: {v}")
