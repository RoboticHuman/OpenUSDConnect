"""Stop OpenUSDConnect sync in Unreal Engine.

Run from the UE console:
    py "D:/gamedev/OpenUSDConnect/integrations/unreal/disconnect.py"
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from integrations.unreal.usd_connect import stop

stop()
print("OpenUSDConnect sync stopped")
