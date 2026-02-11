"""
usd_compose_diff.py

Compose an emitted USD "diff layer" (typically a SessionLayer export from a DCC/add-on)
on top of a base USD file, using vanilla OpenUSD Python (pxr).

This file provides TWO composition patterns:

  1) Session overlay (recommended):
     - Open base stage
     - Import diff payload into an in-memory layer
     - Add that layer as the STRONGEST sublayer of the stage's SessionLayer
     - Base file is NOT modified

  2) Overlay-root stage:
     - Create a new anonymous root layer that sublayers [diff, base]
     - Open a stage from that overlay root
     - Base file is NOT modified

USAGE EXAMPLES
==============

A) Compose diff payload from a file onto a base file (session overlay), then export composed result:

    python usd_compose_diff.py \
        --base test_scene.usda \
        --diff-file emitted_diff.usda \
        --mode session \
        --export composed_output.usda

B) Same, but using overlay-root mode:

    python usd_compose_diff.py \
        --base test_scene.usda \
        --diff-file emitted_diff.usda \
        --mode overlay-root \
        --export composed_output.usda

C) Read diff payload from stdin (useful for piping):
    cat emitted_diff.usda | python usd_compose_diff.py --base test_scene.usda --mode session --export composed_output.usda

D) Quick inspection without export (prints a couple transforms if present):
    python usd_compose_diff.py --base test_scene.usda --diff-file emitted_diff.usda --mode session

NOTES
=====
- Requires OpenUSD Python bindings installed and importable: `from pxr import Usd, Sdf, UsdGeom`
- Works with diff payloads that are valid USD layer text (e.g. .usda) as emitted by ExportToString().
- This composes *opinions*; it does not "merge-save" changes back into the base file.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Tuple

from pxr import Usd, Sdf, UsdGeom


# -----------------------------------------------------------------------------
# Function: open_with_diff_session_overlay
#
# Recommended when you want to "apply diff" ephemerally (like a DCC session):
# - Base stage remains unchanged on disk
# - Diff is composed strongly via the stage's SessionLayer subLayerPaths
#
# Usage:
#   stage, handles = open_with_diff_session_overlay("test_scene.usda", diff_payload)
#   stage.Export("composed.usda")
# -----------------------------------------------------------------------------
def open_with_diff_session_overlay(
    base_usd_path: str, diff_usda_payload: str
) -> Tuple[Usd.Stage, dict]:
    # 1) Open base layer from disk (resolver/path identifier)
    base_layer = Sdf.Layer.FindOrOpen(base_usd_path)
    if base_layer is None:
        raise RuntimeError(f"Failed to open base layer: {base_usd_path}")

    stage = Usd.Stage.Open(base_layer)
    if stage is None:
        raise RuntimeError("Failed to open stage from base layer")

    # 2) Create an in-memory layer from the diff payload
    diff_layer = Sdf.Layer.CreateAnonymous("diff.usda")
    ok = diff_layer.ImportFromString(diff_usda_payload)
    if not ok:
        raise RuntimeError("Failed to import diff payload into a USD layer")

    # 3) Compose it strongly by sublayering into session layer
    # In subLayerPaths: earlier entries are stronger.
    session_layer = stage.GetSessionLayer()
    session_layer.subLayerPaths.insert(0, diff_layer.identifier)

    # Keep important handles alive for callers (avoid GC surprises)
    handles = {
        "base_layer": base_layer,
        "diff_layer": diff_layer,
        "session_layer": session_layer,
    }
    return stage, handles


# -----------------------------------------------------------------------------
# Function: open_with_diff_overlay_root
#
# Use this if you want a "single root" that represents the composition:
# - Creates an anonymous overlay root layer that sublayers [diff (strongest), base (weaker)]
# - Opens a stage from overlay root
# - Base stage remains unchanged on disk
#
# Usage:
#   stage, handles = open_with_diff_overlay_root("test_scene.usda", diff_payload)
#   stage.Export("composed.usda")
# -----------------------------------------------------------------------------
def open_with_diff_overlay_root(
    base_usd_path: str, diff_usda_payload: str
) -> Tuple[Usd.Stage, dict]:
    base_layer = Sdf.Layer.FindOrOpen(base_usd_path)
    if base_layer is None:
        raise RuntimeError(f"Failed to open base layer: {base_usd_path}")

    diff_layer = Sdf.Layer.CreateAnonymous("diff.usda")
    ok = diff_layer.ImportFromString(diff_usda_payload)
    if not ok:
        raise RuntimeError("Failed to import diff payload into a USD layer")

    overlay_root = Sdf.Layer.CreateAnonymous("overlay_root.usda")
    overlay_root.subLayerPaths = [diff_layer.identifier, base_layer.identifier]

    stage = Usd.Stage.Open(overlay_root)
    if stage is None:
        raise RuntimeError("Failed to open stage from overlay root layer")

    handles = {
        "base_layer": base_layer,
        "diff_layer": diff_layer,
        "overlay_root": overlay_root,
    }
    return stage, handles


# -----------------------------------------------------------------------------
# Helper: load diff payload
#
# Usage:
#   payload = load_diff_payload(diff_file="emitted_diff.usda", diff_string=None, stdin_ok=True)
# -----------------------------------------------------------------------------
def load_diff_payload(
    diff_file: Optional[str],
    diff_string: Optional[str],
    stdin_ok: bool = True,
) -> str:
    if diff_string and diff_string.strip():
        return diff_string

    if diff_file and diff_file.strip():
        with open(diff_file, "r", encoding="utf-8") as f:
            return f.read()

    if stdin_ok and not sys.stdin.isatty():
        return sys.stdin.read()

    raise RuntimeError("No diff payload provided. Use --diff-file, --diff-string, or pipe via stdin.")


# -----------------------------------------------------------------------------
# Helper: simple inspection
# Prints translate/rotate/scale if present on given prim paths (best-effort).
# -----------------------------------------------------------------------------
def print_xform_summary(stage: Usd.Stage, prim_paths=("/World/Cube", "/World/Sphere", "/World/Cone", "/World/Cylinder")):
    for p in prim_paths:
        prim = stage.GetPrimAtPath(p)
        if not prim or not prim.IsValid():
            print(f"{p}: (no prim)")
            continue

        xf = UsdGeom.Xformable(prim)
        if not xf:
            print(f"{p}: (not xformable)")
            continue

        ops = xf.GetOrderedXformOps()
        print(f"{p}: {len(ops)} xformOps")
        for op in ops:
            try:
                v = op.Get()
            except Exception:
                v = "(unreadable)"
            print(f"  - {op.GetName()} = {v}")


# -----------------------------------------------------------------------------
# CLI entrypoint
# -----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Compose a USD diff layer (payload) on top of a base USD file.")
    ap.add_argument("--base", required=True, help="Base USD file path (e.g. test_scene.usda)")
    ap.add_argument(
        "--mode",
        choices=["session", "overlay-root"],
        default="session",
        help="Composition strategy: session (recommended) or overlay-root",
    )
    ap.add_argument("--diff-file", default=None, help="Path to diff payload file (.usda text)")
    ap.add_argument("--diff-string", default=None, help="Diff payload as a literal string (advanced)")
    ap.add_argument(
        "--export",
        default=None,
        help="Optional output path to export composed stage (e.g. composed_output.usda)",
    )
    ap.add_argument(
        "--no-inspect",
        action="store_true",
        help="Do not print a basic xform summary after composing",
    )

    args = ap.parse_args()

    diff_payload = load_diff_payload(args.diff_file, args.diff_string, stdin_ok=True)

    if args.mode == "session":
        stage, handles = open_with_diff_session_overlay(args.base, diff_payload)
    else:
        stage, handles = open_with_diff_overlay_root(args.base, diff_payload)

    if not args.no_inspect:
        print("=== Composed Stage Xform Summary (best-effort) ===")
        print_xform_summary(stage)

    if args.export:
        # Exports the composed stage to a single layer file.
        # Note: This is a "flattened" export of composed results, not just the diff.
        ok = stage.Export(args.export)
        if not ok:
            raise RuntimeError(f"Stage export failed: {args.export}")
        print(f"Exported composed stage to: {args.export}")


if __name__ == "__main__":
    main()
