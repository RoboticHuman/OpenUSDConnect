"""Create a test USD asset with real mesh geometry for manual testing.

Usage:
    uv run python scripts/create_test_asset.py [output_path]

Default output: test_asset.usda
"""

import os
import sys

# Add tests/ to path so we can import the shared builder
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "tests"))

from test_asset_builder import create_chair_asset, EXPECTED_MESH_COUNT, EXPECTED_VERTEX_COUNT

output_path = sys.argv[1] if len(sys.argv) > 1 else "test_asset.usda"
create_chair_asset(output_path)
print(f"Created {output_path} — chair with {EXPECTED_VERTEX_COUNT} vertices across {EXPECTED_MESH_COUNT} meshes")
print(f"  Root prim: /Model (defaultPrim)")
print(f"  Children: /Model/Seat, /Model/Leg_0..3, /Model/Back")
