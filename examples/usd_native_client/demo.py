"""Minimal layered publisher and receiver using ordinary OpenUSD authoring."""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

from pxr import Gf, Sdf, Usd, UsdGeom

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from openusdconnect import UsdPublisher, UsdReceiver  # noqa: E402

BASE_USD = Path(__file__).with_name("empty.usda")
LOCAL_SPHERE_PATH = "/World/LocalSphere"
PEER_CUBE_PATH = "/World/PeerCube"


def build_parser(add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        add_help=add_help,
        description="Publish local edits and receive authoritative edits from another peer.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7320)
    parser.add_argument("--rate", type=float, default=30.0)
    parser.add_argument(
        "--seconds",
        type=float,
        default=0.0,
        help="stop after this many seconds; zero runs until Ctrl+C",
    )
    return parser


def _open_stages() -> tuple[Usd.Stage, Usd.Stage]:
    root = Sdf.Layer.FindOrOpen(str(BASE_USD))
    if root is None:
        raise RuntimeError(f"could not open {BASE_USD}")
    author = Usd.Stage.Open(root, Sdf.Layer.CreateAnonymous("author-session.usda"))
    mirror = Usd.Stage.Open(root, Sdf.Layer.CreateAnonymous("mirror-session.usda"))
    author.SetEditTarget(Usd.EditTarget(author.GetSessionLayer()))
    return author, mirror


def run(args: argparse.Namespace, *, expect_peer: bool = False) -> int:
    author_stage, mirror_stage = _open_stages()
    receiver = UsdReceiver(
        mirror_stage,
        app_name="usd-native-client-demo",
        host=args.host,
        port=args.port,
        persist_token=False,
    )
    publisher = UsdPublisher(
        author_stage,
        app_name="usd-native-client-demo",
        host=args.host,
        port=args.port,
        department="layout",
        persist_token=False,
    )

    try:
        receiver.start()
        if not receiver.wait_connected(timeout=5):
            print("receiver could not connect", file=sys.stderr)
            return 1
        if not publisher.connect():
            print("publisher could not connect", file=sys.stderr)
            return 1

        sphere = UsdGeom.Sphere.Define(author_stage, LOCAL_SPHERE_PATH)
        sphere.CreateRadiusAttr(1.0)
        sphere.CreateDisplayColorAttr([Gf.Vec3f(0.08, 0.45, 1.0)])
        translate = UsdGeom.Xformable(sphere).AddTranslateOp()
        translate.Set(Gf.Vec3d(0.0, 1.25, 0.0))
        if publisher.update() == 0:
            print("initial sphere batch was not sent", file=sys.stderr)
            return 1

        expected_paths = [LOCAL_SPHERE_PATH]
        if expect_peer:
            expected_paths.append(PEER_CUBE_PATH)

        replay_deadline = time.monotonic() + 5.0
        while time.monotonic() < replay_deadline:
            receiver.update()
            if all(mirror_stage.GetPrimAtPath(path) for path in expected_paths):
                break
            time.sleep(0.01)
        else:
            missing = [path for path in expected_paths if not mirror_stage.GetPrimAtPath(path)]
            print(
                f"authoritative mirror did not receive: {', '.join(missing)}",
                file=sys.stderr,
            )
            return 1

        started = time.monotonic()
        next_tick = started
        next_report = started
        interval = 1.0 / max(args.rate, 1.0)
        print("publishing LocalSphere and receiving PeerCube; press Ctrl+C to stop")
        while args.seconds <= 0 or time.monotonic() - started < args.seconds:
            elapsed = time.monotonic() - started
            translate.Set(Gf.Vec3d(math.sin(elapsed) * 2.5, 1.25, 0.0))
            publisher.update()
            receiver.update()

            now = time.monotonic()
            if now >= next_report:
                local = mirror_stage.GetPrimAtPath(LOCAL_SPHERE_PATH)
                peer = mirror_stage.GetPrimAtPath(PEER_CUBE_PATH)
                print(
                    f"  seq={receiver.last_seq} "
                    f"layered={receiver.layered_replay_active} "
                    f"local_valid={bool(local)} "
                    f"peer_valid={bool(peer)}"
                )
                next_report = now + 1.0
            next_tick += interval
            if (sleep_for := next_tick - time.monotonic()) > 0:
                time.sleep(sleep_for)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        publisher.close()
        receiver.close()


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
