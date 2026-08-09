"""Independent USD-native publisher used by the onboarding demo."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from pxr import Gf, Sdf, Usd, UsdGeom

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from openusdconnect import UsdPublisher  # noqa: E402

BASE_USD = Path(__file__).with_name("empty.usda")
PEER_CUBE_PATH = "/World/PeerCube"


def _build_stage() -> Usd.Stage:
    root = Sdf.Layer.FindOrOpen(str(BASE_USD))
    if root is None:
        raise RuntimeError(f"could not open {BASE_USD}")
    stage = Usd.Stage.Open(root, Sdf.Layer.CreateAnonymous("peer-session.usda"))
    stage.SetEditTarget(Usd.EditTarget(stage.GetSessionLayer()))
    return stage


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish a pre-authored cube from an independent USD client.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7320)
    parser.add_argument("--delay", type=float, default=0.75)
    args = parser.parse_args()

    stage = _build_stage()
    cube = UsdGeom.Cube.Define(stage, PEER_CUBE_PATH)
    cube.CreateSizeAttr(1.5)
    cube.CreateDisplayColorAttr([Gf.Vec3f(1.0, 0.24, 0.05)])
    UsdGeom.Xformable(cube).AddTranslateOp().Set(Gf.Vec3d(4.0, 1.0, 0.0))

    time.sleep(max(args.delay, 0.0))
    try:
        with UsdPublisher(
            stage,
            app_name="usd-native-client-peer",
            host=args.host,
            port=args.port,
            department="lookdev",
            persist_token=False,
        ) as publisher:
            sent = publisher.publish_current_edit_target()
    except ConnectionError as exc:
        print(f"peer could not connect: {exc}", file=sys.stderr)
        return 1

    if sent == 0:
        print("peer cube did not produce an authored batch", file=sys.stderr)
        return 1
    print(f"peer published {PEER_CUBE_PATH} ({sent} events)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
