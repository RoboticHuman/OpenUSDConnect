"""Author the Fourier-wave procedural prim and (optionally) animate it.

Creates a ``UsdProcGenerativeProcedural`` prim carrying the wave parameters as
primvars. The prim is declaration-only; the companion ``wave_client.py``
evaluates it. With ``--animate`` the phases advance continuously so the wave
rolls in every connected viewer.

Usage (against a running server):
    uv run python examples/fourier_waves/author.py --port 7301 --animate 0
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from openusdconnect.cli_common import (  # noqa: E402
    add_sync_endpoint_args,
    nonnegative_seconds,
    positive_float,
    positive_int,
)
from openusdconnect.sender import EventSender  # noqa: E402


def _floats(text: str) -> list[float]:
    return [float(v) for v in text.split(",") if v.strip()]


def setup_events(args) -> list[dict]:
    freqs = _floats(args.frequencies)
    amps = _floats(args.amplitudes)
    phases = _floats(args.phases)
    return [
        {"k": "ensure_prim", "prim": args.prim, "typeName": "GenerativeProcedural",
         "api_schemas": ["HydraGenerativeProceduralAPI"]},
        {"k": "set_gprim_attrs", "prim": args.prim,
         "attrs": {
             "proceduralSystem": "openusdconnect:computeClient",
             "primvars:hdGp:proceduralType": "FourierWave",
             "primvars:frequencies": freqs,
             "primvars:amplitudes": amps,
             "primvars:phases": phases,
             "primvars:resolution": args.resolution,
             "primvars:size": args.size,
         },
         "primvar_meta": {
             "primvars:hdGp:proceduralType": {"typeName": "token"},
             "primvars:frequencies": {"typeName": "float[]", "interpolation": "constant"},
             "primvars:amplitudes": {"typeName": "float[]", "interpolation": "constant"},
             "primvars:phases": {"typeName": "float[]", "interpolation": "constant"},
             "primvars:resolution": {"typeName": "int", "interpolation": "constant"},
             "primvars:size": {"typeName": "float", "interpolation": "constant"},
         }},
    ]


def phase_events(args, t: float) -> list[dict]:
    """Advance each component's phase at a slightly different rate."""
    base = _floats(args.phases)
    n = max(len(base), len(_floats(args.frequencies)))
    phases = [
        (base[i] if i < len(base) else 0.0) + args.rate * t * (1.0 + 0.35 * i)
        for i in range(n)
    ]
    return [
        {"k": "set_gprim_attrs", "prim": args.prim,
         "attrs": {"primvars:phases": phases},
         "primvar_meta": {
             "primvars:phases": {"typeName": "float[]", "interpolation": "constant"},
         }},
    ]


def build_parser(add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        add_help=add_help,
        description="Author (and optionally animate) the Fourier-wave procedural prim.",
    )
    add_sync_endpoint_args(parser, port_default=7301)
    parser.add_argument("--prim", default="/World/FourierWave")
    parser.add_argument(
        "--frequencies", default="1.0,2.3,4.1",
        help="comma-separated wave frequencies (default 1.0,2.3,4.1)",
    )
    parser.add_argument(
        "--amplitudes", default="0.6,0.25,0.12",
        help="comma-separated amplitudes (default 0.6,0.25,0.12)",
    )
    parser.add_argument(
        "--phases", default="0,0,0",
        help="comma-separated starting phases in radians (default 0,0,0)",
    )
    parser.add_argument(
        "--resolution", type=positive_int, default=96,
        help="grid vertices per side (default 96)",
    )
    parser.add_argument(
        "--size", type=positive_float, default=10.0,
        help="grid extent in scene units (default 10)",
    )
    parser.add_argument(
        "--animate", type=nonnegative_seconds, default=None, metavar="SECONDS",
        help="advance phases continuously for SECONDS (0 = until Ctrl+C)",
    )
    parser.add_argument(
        "--rate", type=positive_float, default=1.5,
        help="phase advance in radians/second while animating (default 1.5)",
    )
    return parser


def run_author(args) -> int:
    sender = EventSender(
        args.host, args.port, client_id="fourier-wave-author", role="emitter",
        origin="fourier-wave-author",
    )
    if not sender.connect():
        print(f"could not connect to {args.host}:{args.port}")
        return 1
    try:
        sender.send_events(setup_events(args))
        print(f"authored {args.prim} "
              f"(frequencies={args.frequencies}, resolution={args.resolution})")
        if args.animate is None:
            return 0
        print("animating phases"
              + (f" for {args.animate:.0f}s" if args.animate else " until Ctrl+C"))
        start = time.monotonic()
        while True:
            t = time.monotonic() - start
            if args.animate and t >= args.animate:
                return 0
            if not sender.send_events(phase_events(args, t)):
                print("server connection lost")
                return 1
            time.sleep(1.0 / 20.0)
    except KeyboardInterrupt:
        print("\nstopping.")
        return 0
    finally:
        sender.disconnect()


def main() -> int:
    return run_author(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
