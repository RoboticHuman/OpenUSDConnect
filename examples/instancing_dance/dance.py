"""Live instancing demo: a ring of pyramids bobbing on sine waves.

Sends events to a running OpenUSDConnect server to:
  1. Define ``/Protos/Pyramid`` as a reference to the Pyramid asset.
  2. Create N instanceable Xform prims arranged in a circle, each
     internal-referencing the prototype.
  3. Drive each prim's Y translation with a phase-offset sine wave so
     the whole ring bobs while the prototype's own animated
     ``xformOp:transform`` keeps the pyramids spinning.

The pyramids replicate through ``set_instanceable``; the bobbing rides
``set_xform_trs`` on top of the prototype's existing matrix animation.
Any connected viewer (usdview, a ``UsdStageAdapter`` client, or a DCC
integration) composes the two and renders the result live.

See ``README.md`` next to this file for the three-terminal recipe.
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

# Allow ``python examples/instancing_dance/dance.py`` from the repo root
# without requiring an editable install on the active PYTHONPATH.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from openusdconnect.sender import EventSender  # noqa: E402


DEFAULT_ASSET = (
    _REPO_ROOT
    / "assets" / "full_assets" / "SubdivisionSurfaces" / "Pyramid" / "Pyramid.usd"
)


def setup_events(asset_path: str, instances: int) -> list[dict]:
    """One-time scene construction events."""
    events: list[dict] = [
        {"k": "ensure_prim", "prim": "/World", "typeName": "Xform"},
        {"k": "ensure_prim", "prim": "/Protos", "typeName": "Scope"},
        {"k": "ensure_prim", "prim": "/Protos/Pyramid", "typeName": "Xform"},
        {
            "k": "set_reference", "prim": "/Protos/Pyramid",
            "refs": [{"asset_path": asset_path}],
        },
    ]
    for i in range(instances):
        path = f"/World/Tower_{i}"
        events.extend([
            {"k": "ensure_prim", "prim": path, "typeName": "Xform"},
            {"k": "ensure_xform_ops", "prim": path},
            {
                "k": "set_reference", "prim": path,
                "refs": [{"asset_path": "", "prim_path": "/Protos/Pyramid"}],
            },
            {"k": "set_instanceable", "prim": path, "instanceable": True},
        ])
    return events


def sine_events(
    t: float, instances: int, radius: float, amplitude: float, period: float,
) -> list[dict]:
    """One frame of per-prim sine-wave translations."""
    omega = 2 * math.pi / period
    events = []
    for i in range(instances):
        ring_angle = 2 * math.pi * i / instances
        x = radius * math.cos(ring_angle)
        z = radius * math.sin(ring_angle)
        y = amplitude * math.sin(omega * t + ring_angle)
        events.append({
            "k": "set_xform_trs",
            "prim": f"/World/Tower_{i}",
            "fields": ["t"],
            "t": [float(x), float(y), float(z)],
        })
    return events


def build_parser(add_help: bool = True) -> argparse.ArgumentParser:
    """Argparse parser for the dance demo.

    ``add_help=False`` lets an orchestrator embed this as a ``parents=``
    entry without duplicate ``-h`` handlers.
    """
    parser = argparse.ArgumentParser(
        add_help=add_help,
        description="Live instancing demo: a ring of pyramids on sine waves.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7300)
    parser.add_argument(
        "--instances", type=int, default=8,
        help="number of pyramid instances in the ring (default 8)",
    )
    parser.add_argument(
        "--radius", type=float, default=6.0,
        help="ring radius in scene units (default 6.0)",
    )
    parser.add_argument(
        "--amplitude", type=float, default=3.0,
        help="sine wave amplitude in scene units (default 3.0)",
    )
    parser.add_argument(
        "--period", type=float, default=4.0,
        help="seconds per sine wave cycle (default 4.0)",
    )
    parser.add_argument(
        "--rate", type=float, default=30.0,
        help="event-send rate in Hz (default 30)",
    )
    parser.add_argument(
        "--asset", default=str(DEFAULT_ASSET),
        help="path to a USD asset to instance (default: the Pyramid asset)",
    )
    return parser


def run_dance(args: argparse.Namespace) -> int:
    """Connect to a running server and drive the sine-wave ring."""
    asset = str(Path(args.asset).resolve())
    if not Path(asset).exists():
        print(f"asset not found: {asset}")
        return 1

    sender = EventSender(args.host, args.port, client_id="dance", origin="demo")
    if not sender.connect():
        print(f"connect failed; is the server running on {args.host}:{args.port}?")
        return 1
    print(f"connected to {args.host}:{args.port}")
    print(f"asset:  {asset}")

    print(f"sending setup events ({args.instances} instances)...")
    if not sender.send_events(setup_events(asset, args.instances)):
        print("setup send failed")
        sender.disconnect()
        return 1
    print("setup complete.")

    tick = 1.0 / args.rate
    start = time.monotonic()
    next_send = start + tick
    log_every = max(1, int(args.rate * 5))
    sent = 0
    try:
        print(f"dancing at {args.rate:g} Hz. Ctrl+C to stop.")
        while True:
            now = time.monotonic()
            events = sine_events(
                now - start, args.instances, args.radius,
                args.amplitude, args.period,
            )
            if not sender.send_events(events):
                print("\nconnection lost.")
                return 1
            sent += 1
            if sent % log_every == 0:
                print(f"  {sent} frames sent ({now - start:.1f}s elapsed)")
            next_send += tick
            sleep_for = next_send - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
    except KeyboardInterrupt:
        print("\nstopping.")
    finally:
        sender.disconnect()
    return 0


def main() -> int:
    return run_dance(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
