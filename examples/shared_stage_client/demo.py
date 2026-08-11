"""Edit and observe one shared USD sublayer with ordinary pxr authoring."""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

from pxr import Gf, Usd, UsdGeom

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from openusdconnect import ClientPhase, SharedStageClient  # noqa: E402

DEFAULT_STAGE = Path(__file__).with_name("scene.usda")
SPHERE_PATH = "/World/SharedSphere"


def _content_layer(stage: Usd.Stage):
    root = stage.GetRootLayer()
    return next(
        layer
        for layer in stage.GetLayerStack(includeSessionLayers=False)
        if layer.identifier != root.identifier
    )


def _translate_op(sphere: UsdGeom.Sphere):
    xformable = UsdGeom.Xformable(sphere)
    for operation in xformable.GetOrderedXformOps():
        if operation.GetOpType() == UsdGeom.XformOp.TypeTranslate:
            return operation
    return xformable.AddTranslateOp()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, default=DEFAULT_STAGE)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7200)
    parser.add_argument("--app-name", default="shared-stage-demo")
    parser.add_argument("--author", action="store_true")
    parser.add_argument("--rate", type=float, default=30.0)
    parser.add_argument("--seconds", type=float, default=0.0)
    parser.add_argument("--sdf-notice-bridge", type=Path)
    args = parser.parse_args()

    stage = Usd.Stage.Open(str(args.stage.resolve()))
    if stage is None:
        print(f"could not open {args.stage}", file=sys.stderr)
        return 1
    content = _content_layer(stage)

    with SharedStageClient(
        stage,
        app_name=args.app_name,
        host=args.host,
        port=args.port,
        persist_token=False,
        delegate_bridge_path=args.sdf_notice_bridge,
    ) as client:
        if not client.connect(timeout=5):
            print("server is unavailable", file=sys.stderr)
            return 1

        deadline = time.monotonic() + 5.0
        while client.status.phase is not ClientPhase.READY:
            client.update()
            status = client.status
            if status.phase in (ClientPhase.RECOVERY_REQUIRED, ClientPhase.REJECTED):
                print(status.reason or status.phase.value, file=sys.stderr)
                return 2
            if time.monotonic() >= deadline:
                print(f"client did not become ready: {status.phase.value}", file=sys.stderr)
                return 1
            time.sleep(0.01)

        if not client.is_layer_reachable(content):
            print("content layer is outside the synchronized graph", file=sys.stderr)
            return 1

        translate = None
        if args.author:
            stage.SetEditTarget(Usd.EditTarget(content))
            sphere = UsdGeom.Sphere.Define(stage, SPHERE_PATH)
            sphere.CreateRadiusAttr(0.75)
            sphere.CreateDisplayColorAttr([Gf.Vec3f(0.08, 0.45, 1.0)])
            translate = _translate_op(sphere)

        started = time.monotonic()
        next_tick = started
        next_report = started
        interval = 1.0 / max(args.rate, 1.0)
        while args.seconds <= 0 or time.monotonic() - started < args.seconds:
            elapsed = time.monotonic() - started
            if translate is not None and client.status.phase is ClientPhase.READY:
                translate.Set(Gf.Vec3d(math.sin(elapsed) * 2.5, 1.0, 0.0))
            update = client.update()

            if client.status.phase is ClientPhase.RECOVERY_REQUIRED:
                failure = client.status.failure
                detail = failure.reason if failure is not None else "transaction rejected"
                print(f"recovery required: {detail}", file=sys.stderr)
                return 2

            now = time.monotonic()
            if now >= next_report:
                position_attr = stage.GetAttributeAtPath(f"{SPHERE_PATH}.xformOp:translate")
                position = position_attr.Get() if position_attr else None
                print(
                    f"seq={client.last_seq} applied={update.applied_events} "
                    f"submitted={update.submitted_events} position={position}"
                )
                next_report = now + 1.0
            next_tick += interval
            if (sleep_for := next_tick - time.monotonic()) > 0:
                time.sleep(sleep_for)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
