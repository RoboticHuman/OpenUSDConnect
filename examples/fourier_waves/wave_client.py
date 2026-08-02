"""Compute-client procedural: expands a Fourier-wave prim into live geometry.

Watches a ``UsdProcGenerativeProcedural`` prim over the sync protocol and, on
every parameter change, regenerates a wave-displaced grid mesh and emits it as
a child prim. The evaluator is an ordinary network client, so every connected
receiver (usdview, Blender, an engine) sees the expanded result. The authored
procedural prim itself stays declaration-only, exactly as the UsdProc schema
intends; this client plays the role a Hydra ``HdGpGenerativeProcedural``
plugin would play inside a renderer.

Usage (against a running server):
    uv run python examples/fourier_waves/wave_client.py --port 7301
"""
from __future__ import annotations

import argparse
import math
import sys
import time
import uuid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from openusdconnect.adapters import UsdStageAdapter  # noqa: E402
from openusdconnect.cli_common import add_sync_endpoint_args  # noqa: E402
from openusdconnect.dispatcher import EventDispatcher  # noqa: E402
from openusdconnect.receiver import ReceiverThread  # noqa: E402
from openusdconnect.sender import EventSender  # noqa: E402

DEFAULTS = {
    "frequencies": [1.0, 2.3],
    "amplitudes": [0.6, 0.25],
    "phases": [0.0, 0.0],
    "resolution": 96,
    "size": 10.0,
}
GOLDEN_ANGLE = math.radians(137.508)


def generate_wave(freqs, amps, phases, resolution, size):
    """Grid points displaced on Y by a sum of directional sinusoids.

    Each component travels along a golden-angle-spaced direction so a
    handful of components already reads as natural interference.
    """
    n = max(int(resolution), 2)
    axis = np.linspace(-size / 2.0, size / 2.0, n, dtype=np.float32)
    grid_x, grid_z = np.meshgrid(axis, axis)
    height = np.zeros_like(grid_x)
    for i, freq in enumerate(freqs):
        amp = amps[i] if i < len(amps) else amps[-1] if amps else 0.0
        phase = phases[i] if i < len(phases) else 0.0
        theta = i * GOLDEN_ANGLE
        along = grid_x * math.cos(theta) + grid_z * math.sin(theta)
        height += np.float32(amp) * np.sin(
            2.0 * np.pi * np.float32(freq) * along / np.float32(size)
            + np.float32(phase)
        )
    points = np.stack([grid_x, height, grid_z], axis=-1).reshape(-1, 3)
    return points, n


def grid_topology(n):
    """Quad faceVertexCounts / faceVertexIndices for an n x n vertex grid."""
    counts = [4] * ((n - 1) * (n - 1))
    indices = []
    for row in range(n - 1):
        for col in range(n - 1):
            a = row * n + col
            indices.extend([a, a + 1, a + n + 1, a + n])
    return counts, indices


class WaveClient:
    """Subscribes to the procedural prim; emits the expanded mesh."""

    def __init__(self, host, port, proc_path):
        self.proc_path = proc_path
        self.result_path = proc_path + "/Result"
        origin = f"fourier-wave-{uuid.uuid4().hex[:8]}"
        self.sender = EventSender(
            host, port, client_id="fourier-wave-client", role="emitter",
            origin=f"{origin}-emit",
        )
        if not self.sender.connect():
            raise RuntimeError(f"could not connect to {host}:{port}")
        from pxr import Usd

        self.mirror = Usd.Stage.CreateInMemory()
        self.receiver = ReceiverThread(
            host=host, port=port, sync_from=1,
            client_id="fourier-wave-client", origin=f"{origin}-recv",
        )
        self.receiver.start()
        self.dispatcher = EventDispatcher(
            receiver=self.receiver,
            adapter=UsdStageAdapter(self.mirror),
            on_applied=self._on_applied,
        )
        self._params_dirty = False
        self._emitted_resolution = None

    def _on_applied(self, prim_paths):
        # Only edits to the procedural prim itself are parameters; its
        # Result child is our own output echoed back into the mirror.
        if any(p == self.proc_path for p in prim_paths):
            self._params_dirty = True

    def _read_params(self):
        from pxr import UsdGeom

        prim = self.mirror.GetPrimAtPath(self.proc_path)
        if not prim or not prim.IsValid():
            return None
        pv = UsdGeom.PrimvarsAPI(prim)

        def get(name, fallback):
            var = pv.GetPrimvar(name)
            value = var.Get() if var else None
            return value if value is not None else fallback

        return {
            "frequencies": [float(v) for v in get("frequencies", DEFAULTS["frequencies"])],
            "amplitudes": [float(v) for v in get("amplitudes", DEFAULTS["amplitudes"])],
            "phases": [float(v) for v in get("phases", DEFAULTS["phases"])],
            "resolution": int(get("resolution", DEFAULTS["resolution"])),
            "size": float(get("size", DEFAULTS["size"])),
        }

    def _emit_mesh(self, params):
        points, n = generate_wave(
            params["frequencies"], params["amplitudes"], params["phases"],
            params["resolution"], params["size"],
        )
        lo = points.min(axis=0)
        hi = points.max(axis=0)
        attrs = {
            "points": points.tolist(),
            "extent": [lo.tolist(), hi.tolist()],
        }
        events = []
        if self._emitted_resolution != n:
            counts, indices = grid_topology(n)
            attrs["faceVertexCounts"] = counts
            attrs["faceVertexIndices"] = indices
            events.append({
                "k": "ensure_prim", "prim": self.result_path, "typeName": "Mesh",
            })
            self._emitted_resolution = n
        events.append({
            "k": "set_gprim_attrs", "prim": self.result_path, "attrs": attrs,
        })
        ok = self.sender.send_events(events)
        print(f"  expanded {n}x{n} grid ({len(points)} points) -> {self.result_path}"
              + ("" if ok else "  [send failed]"))
        return ok

    def run(self):
        print(f"watching {self.proc_path}; expanding into {self.result_path}")
        while True:
            self.dispatcher.drain_and_apply()
            if self._params_dirty:
                self._params_dirty = False
                params = self._read_params()
                if params is not None:
                    self._emit_mesh(params)
            time.sleep(1.0 / 30.0)

    def stop(self):
        self.receiver.stop()
        self.sender.disconnect()


def build_parser(add_help: bool = True) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        add_help=add_help,
        description="Fourier-wave compute-client procedural evaluator.",
    )
    add_sync_endpoint_args(parser, port_default=7301)
    parser.add_argument(
        "--prim", default="/World/FourierWave",
        help="procedural prim to watch (default /World/FourierWave)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    client = WaveClient(args.host, args.port, args.prim)
    try:
        client.run()
    except KeyboardInterrupt:
        print("\nstopping.")
    finally:
        client.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
