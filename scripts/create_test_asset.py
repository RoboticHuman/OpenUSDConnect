"""Create a test USD asset with real mesh geometry for manual testing.

Usage:
    uv run python scripts/create_test_asset.py [output_path]

Default output: test_asset.usda
"""

import argparse
import os
import sys

# Add tests/ to path so we can import the shared builder
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(project_root, "tests"))

from unit.test_asset_builder import EXPECTED_MESH_COUNT, EXPECTED_VERTEX_COUNT, create_chair_asset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", default="test_asset.usda")
    args = parser.parse_args(argv)

    create_chair_asset(args.output)
    print(
        f"Created {args.output} - chair with "
        f"{EXPECTED_VERTEX_COUNT} vertices across {EXPECTED_MESH_COUNT} meshes"
    )
    print("  Root prim: /Model (defaultPrim)")
    print("  Children: /Model/Seat, /Model/Leg_0..3, /Model/Back")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
