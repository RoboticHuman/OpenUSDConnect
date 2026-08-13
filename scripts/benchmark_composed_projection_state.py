"""Profile persistent composed projection state against a real USD asset.

Examples::

    python scripts/benchmark_composed_projection_state.py ASSET startup --iterations 20
    python scripts/benchmark_composed_projection_state.py ASSET memory
    python scripts/benchmark_composed_projection_state.py ASSET noop --iterations 1000
    python scripts/benchmark_composed_projection_state.py ASSET sparse --iterations 1000
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import statistics
import time
import tracemalloc
from pathlib import Path

from pxr import Usd, UsdGeom

from openusdconnect.composed_projection import (
    ComposedChangeProjection,
    ComposedProjectionState,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("asset", type=Path)
    parser.add_argument(
        "phase",
        choices=("startup", "baseline", "memory", "noop", "sparse"),
        help="'baseline' is a deprecated alias for 'startup'",
    )
    parser.add_argument("--iterations", type=int, default=100)
    return parser.parse_args()


def _measure(operation, iterations: int) -> list[float]:
    samples = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        operation()
        samples.append((time.perf_counter_ns() - start) / 1_000_000)
    return samples


def _print_samples(samples: list[float]) -> None:
    ordered = sorted(samples)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    print(
        f"median={statistics.median(samples):.3f} ms "
        f"p95={p95:.3f} ms min={ordered[0]:.3f} ms max={ordered[-1]:.3f} ms"
    )


def _process_memory_bytes() -> tuple[int, int] | None:
    if not hasattr(ctypes, "windll"):
        return None

    class ProcessMemoryCountersEx(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("page_fault_count", ctypes.c_ulong),
            ("peak_working_set_size", ctypes.c_size_t),
            ("working_set_size", ctypes.c_size_t),
            ("quota_peak_paged_pool_usage", ctypes.c_size_t),
            ("quota_paged_pool_usage", ctypes.c_size_t),
            ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
            ("quota_non_paged_pool_usage", ctypes.c_size_t),
            ("pagefile_usage", ctypes.c_size_t),
            ("peak_pagefile_usage", ctypes.c_size_t),
            ("private_usage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCountersEx()
    counters.cb = ctypes.sizeof(counters)
    get_current_process = ctypes.windll.kernel32.GetCurrentProcess
    get_current_process.restype = ctypes.c_void_p
    get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
    get_process_memory_info.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ProcessMemoryCountersEx),
        ctypes.c_ulong,
    ]
    get_process_memory_info.restype = ctypes.c_int
    if not get_process_memory_info(
        get_current_process(),
        ctypes.byref(counters),
        counters.cb,
    ):
        raise ctypes.WinError()
    return counters.working_set_size, counters.private_usage


def main() -> None:
    args = _parse_args()
    asset = args.asset.resolve()
    if args.iterations <= 0:
        raise ValueError("--iterations must be positive")

    if args.phase in {"startup", "baseline"}:
        stage = Usd.Stage.Open(str(asset))
        if stage is None:
            raise RuntimeError(f"could not open {asset}")

        def startup() -> None:
            state = ComposedProjectionState(stage)
            state.close()

        samples = _measure(startup, args.iterations)
        gc.collect()
        _print_samples(samples)
        return

    if args.phase == "memory":
        stage = Usd.Stage.Open(str(asset))
        if stage is None:
            raise RuntimeError(f"could not open {asset}")
        gc.collect()
        process_before = _process_memory_bytes()
        tracemalloc.start()
        state = ComposedProjectionState(stage)
        gc.collect()
        process_with_state = _process_memory_bytes()
        with_state = tracemalloc.get_traced_memory()[0]
        state.close()
        del state
        gc.collect()
        after_close = tracemalloc.get_traced_memory()[0]
        print(f"python_heap={(with_state - after_close) / (1024 * 1024):.3f} MiB")
        if process_before is not None and process_with_state is not None:
            print(
                "process_increment="
                f"{(process_with_state[0] - process_before[0]) / (1024 * 1024):.3f} MiB "
                "working_set, "
                f"{(process_with_state[1] - process_before[1]) / (1024 * 1024):.3f} MiB "
                "private"
            )
        return

    stage = Usd.Stage.Open(str(asset))
    if stage is None:
        raise RuntimeError(f"could not open {asset}")
    state = ComposedProjectionState(stage)

    if args.phase == "noop":
        def transaction() -> None:
            with ComposedChangeProjection(stage, [], state=state) as projection:
                if projection.build_events():
                    raise RuntimeError("no-op projection produced events")
                projection.commit()

    else:
        imageable = next(
            (UsdGeom.Imageable(prim) for prim in stage.Traverse() if UsdGeom.Imageable(prim)),
            None,
        )
        if imageable is None:
            raise RuntimeError(f"{asset} has no imageable prim")
        stage.SetEditTarget(stage.GetSessionLayer())
        visibility = imageable.GetVisibilityAttr()
        invisible = False

        def transaction() -> None:
            nonlocal invisible
            invisible = not invisible
            value = UsdGeom.Tokens.invisible if invisible else UsdGeom.Tokens.inherited
            with ComposedChangeProjection(stage, [], state=state) as projection:
                visibility.Set(value)
                projected = projection.build_events()
                if len(projected) != 1:
                    raise RuntimeError(f"sparse projection produced {len(projected)} events")
                projection.commit()

    samples = _measure(transaction, args.iterations)
    _print_samples(samples)


if __name__ == "__main__":
    main()
