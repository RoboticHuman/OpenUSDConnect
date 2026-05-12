"""Emitter that uses the REAL NoticeEmitter on a composed USD stage.

Creates a USD stage with /World/Chair referencing test_asset.usda,
then uses NoticeEmitter to detect changes and generate the exact events
the real Blender emitter would produce.

Two phases:
  Phase 1 — Initial setup: define /World/Chair, add reference, set transform.
            NoticeEmitter picks up all composed prims as first-encounter.
  Phase 2 — Movement: change /World/Chair translate.
            NoticeEmitter picks up /World/Chair (known) + any children
            that get dirty from ObjectsChanged.

Run via: python tests/ref_emitter_script.py --port PORT --asset-path PATH
"""

import os
import socket
import sys
import time

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(os.path.dirname(_scripts_dir)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
_venv_sp = os.path.join(project_root, ".venv", "Lib", "site-packages")
if os.path.isdir(_venv_sp) and _venv_sp not in sys.path:
    sys.path.append(_venv_sp)
for _k in [k for k in sys.modules if k.startswith("openusdconnect")]:
    del sys.modules[_k]

from pxr import Gf, Usd, UsdGeom

from openusdconnect.emitter import NoticeEmitter
from openusdconnect.event_apply import ensure_canonical_ops
from openusdconnect.protocol import (
    make_hello,
    make_quit,
    make_txn,
)
from openusdconnect.transport import send_line


def main():
    argv = sys.argv
    port = 7200
    asset_path = ""

    args = argv[1:]
    for i, arg in enumerate(args):
        if arg == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
        if arg == "--asset-path" and i + 1 < len(args):
            asset_path = args[i + 1]

    if not asset_path:
        print("[RefEmitter] ERROR: --asset-path required")
        sys.exit(1)

    # Use forward slashes for USD asset resolution
    asset_path = asset_path.replace("\\", "/")

    print(f"[RefEmitter] Asset path: {asset_path}")

    # -----------------------------------------------------------------
    # Build a composed USD stage with a reference, just like the real
    # emitter would have after importing a scene file.
    # -----------------------------------------------------------------
    stage = Usd.Stage.CreateInMemory()
    stage.SetEditTarget(Usd.EditTarget(stage.GetSessionLayer()))

    # Define /World/Chair and add a reference to test_asset.usda
    chair = stage.DefinePrim("/World/Chair", "Xform")
    chair.GetReferences().AddReference(asset_path, "/Model")
    ensure_canonical_ops(stage, "/World/Chair")

    # Set initial transform
    xf = UsdGeom.Xformable(chair)
    for op in xf.GetOrderedXformOps():
        if op.GetAttr().GetName() == "xformOp:translate":
            op.Set(Gf.Vec3d(2.0, 0.0, 0.0))
            break

    # Print composed prim tree for debugging
    print("\n=== Composed Stage ===")
    for prim in stage.Traverse():
        print(f"  {prim.GetPath()} type={prim.GetTypeName()}")

    # -----------------------------------------------------------------
    # Phase 1 — NoticeEmitter picks up the initial state.
    # The prim definition + reference add triggered ObjectsChanged,
    # so all composed prims are now dirty.
    # -----------------------------------------------------------------
    emitter = NoticeEmitter(stage)

    # The notices fired during construction for the DefinePrim/AddReference
    # but the emitter wasn't listening yet.  Mark all prims dirty manually,
    # exactly like BlenderStageAuthor would on first depsgraph pass.
    for prim in stage.Traverse():
        emitter.mark_dirty(str(prim.GetPath()))

    phase1_events = emitter.build_events_for_dirty(include_matrices=False)

    print(f"\n=== Phase 1 events ({len(phase1_events)}) ===")
    for ev in phase1_events:
        print(f"  {ev.get('k')} {ev.get('prim')}")

    # -----------------------------------------------------------------
    # Phase 2 — Simulate user moving the Chair.
    # Change the translate on /World/Chair.  The ObjectsChanged notice
    # fires, and NoticeEmitter picks up whatever USD reports as dirty.
    # -----------------------------------------------------------------
    for op in xf.GetOrderedXformOps():
        if op.GetAttr().GetName() == "xformOp:translate":
            op.Set(Gf.Vec3d(3.0, 0.0, 0.0))
            break

    # In the real Blender flow, BlenderStageAuthor also writes children's
    # local transforms (because depsgraph fires for all children when
    # parent moves).  Simulate that: touch each child's translate.
    for prim in stage.Traverse():
        pp = str(prim.GetPath())
        if pp == "/World/Chair" or pp == "/World":
            continue
        child_xf = UsdGeom.Xformable(prim)
        for op in child_xf.GetOrderedXformOps():
            if op.GetAttr().GetName() == "xformOp:translate":
                # Re-write the same value — this still triggers a notice
                op.Set(op.Get())
                break

    phase2_events = emitter.build_events_for_dirty(include_matrices=False)

    print(f"\n=== Phase 2 events ({len(phase2_events)}) ===")
    for ev in phase2_events:
        print(f"  {ev.get('k')} {ev.get('prim')}")

    # -----------------------------------------------------------------
    # Send all events to the server
    # -----------------------------------------------------------------
    print(f"\n[RefEmitter] Connecting to 127.0.0.1:{port}")
    sock = socket.create_connection(("127.0.0.1", port))
    send_line(sock, make_hello("emitter"))

    if phase1_events:
        send_line(sock, make_txn("ref-test-emitter", phase1_events))
        print(f"[RefEmitter] Sent txn1: {len(phase1_events)} events (initial)")

    time.sleep(0.3)

    if phase2_events:
        send_line(sock, make_txn("ref-test-emitter", phase2_events))
        print(f"[RefEmitter] Sent txn2: {len(phase2_events)} events (movement)")

    time.sleep(0.5)
    send_line(sock, make_quit())
    time.sleep(0.5)
    sock.close()
    print("[RefEmitter] Done")


if __name__ == "__main__":
    main()
