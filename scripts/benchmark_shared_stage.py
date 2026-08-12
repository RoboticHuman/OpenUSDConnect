#!/usr/bin/env python3
"""Measure shared-stage layer snapshots, dirty diffs, and graph baselines."""

from __future__ import annotations

import argparse
import ctypes
import statistics
import sys
import tempfile
import time
from pathlib import Path

if sys.platform != "win32":
    import resource

from pxr import Sdf, Usd

from openusdconnect.cli_common import positive_int
from openusdconnect.codec import encode_message
from openusdconnect.sdf_layer_tracker import SdfLayerChangeTracker
from openusdconnect.shared_layer_graph import SharedLayerGraph, apply_sublayer_entries


def _timing(samples: list[float]) -> tuple[float, float]:
    p95 = samples[0] if len(samples) == 1 else statistics.quantiles(samples, n=100)[94]
    return statistics.median(samples), p95


def _print_timing(label: str, samples: list[float]) -> None:
    median, p95 = _timing(samples)
    print(f"{label}: {median:.2f} us median, {p95:.2f} us p95")


def _rss_bytes() -> int:
    if sys.platform == "win32":
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        get_current_process = ctypes.windll.kernel32.GetCurrentProcess
        get_current_process.restype = wintypes.HANDLE
        get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
        get_process_memory_info.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        get_process_memory_info.restype = wintypes.BOOL
        if not get_process_memory_info(get_current_process(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError()
        return int(counters.PeakWorkingSetSize)
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def _dense_stage(spec_count: int) -> tuple[Usd.Stage, object]:
    stage = Usd.Stage.CreateInMemory()
    prim = stage.DefinePrim("/World/Values", "Xform")
    first = None
    for index in range(spec_count):
        attr = prim.CreateAttribute(f"user:value_{index}", Sdf.ValueTypeNames.Int, True)
        attr.Set(index)
        if first is None:
            first = attr
    return stage, first


def _create_graph(directory: Path, layer_count: int) -> Usd.Stage:
    root = Sdf.Layer.CreateNew(str(directory / "root.usda"))
    for index in range(layer_count):
        child = Sdf.Layer.CreateNew(str(directory / f"layer_{index}.usda"))
        Sdf.CreatePrimInLayer(child, f"/Layer_{index}")
        child.Save()
        root.subLayerPaths.append(f"./layer_{index}.usda")
    root.Save()
    return Usd.Stage.Open(root)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec-count", type=positive_int, default=10_000)
    parser.add_argument("--dirty-iterations", type=positive_int, default=20)
    parser.add_argument("--fallback-iterations", type=positive_int, default=5)
    parser.add_argument("--graph-layers", type=positive_int, default=100)
    parser.add_argument("--graph-iterations", type=positive_int, default=20)
    parser.add_argument("--idle-iterations", type=positive_int, default=100)
    args = parser.parse_args()

    stage, attr = _dense_stage(args.spec_count)
    graph = SharedLayerGraph(stage, authoritative=True)
    rss_before = _rss_bytes()
    start = time.perf_counter_ns()
    tracker = SdfLayerChangeTracker(stage, graph)
    tracker_init = (time.perf_counter_ns() - start) / 1_000
    snapshot_rss = max(0, _rss_bytes() - rss_before)

    prepare_samples = []
    commit_samples = []
    for iteration in range(args.dirty_iterations):
        attr.SetDocumentation(f"iteration {iteration}")
        start = time.perf_counter_ns()
        batches = tracker.prepare_local_changes()
        prepare_samples.append((time.perf_counter_ns() - start) / 1_000)
        if len(batches) != 1 or len(batches[0].events) != 1:
            raise RuntimeError("single-field edit did not produce one shared-stage event")
        start = time.perf_counter_ns()
        tracker.mark_prepared_sent(batches[0])
        commit_samples.append((time.perf_counter_ns() - start) / 1_000)

    fallback_samples = []
    prim_spec = stage.GetRootLayer().GetPrimAtPath("/World/Values")
    for iteration in range(args.fallback_iterations):
        with Sdf.ChangeBlock():
            attr.Set(-iteration - 1)
            prim_spec.active = False
            prim_spec.ClearInfo("active")
        start = time.perf_counter_ns()
        batches = tracker.prepare_local_changes()
        fallback_samples.append((time.perf_counter_ns() - start) / 1_000)
        if len(batches) != 1 or len(batches[0].events) != 1:
            raise RuntimeError("fallback edit did not produce one shared-stage event")
        tracker.mark_prepared_sent(batches[0])

    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        source_dir = directory / "source"
        target_dir = directory / "target"
        source_dir.mkdir()
        target_dir.mkdir()
        source_stage = _create_graph(source_dir, args.graph_layers)
        target_stage = _create_graph(target_dir, args.graph_layers)

        start = time.perf_counter_ns()
        source_graph = SharedLayerGraph(source_stage, authoritative=True)
        graph_capture = (time.perf_counter_ns() - start) / 1_000
        baseline = source_graph.state_message(seq=1)

        graph_tracker = SdfLayerChangeTracker(source_stage, source_graph)
        idle_samples = []
        for _ in range(args.idle_iterations):
            start = time.perf_counter_ns()
            graph_tracker.prepare_local_changes()
            idle_samples.append((time.perf_counter_ns() - start) / 1_000)

        encode_samples = []
        apply_samples = []
        target_graph = SharedLayerGraph(target_stage)
        preflight_samples = []
        for _ in range(args.graph_iterations):
            start = time.perf_counter_ns()
            target_graph._validate_local_graph()
            preflight_samples.append((time.perf_counter_ns() - start) / 1_000)
            start = time.perf_counter_ns()
            encode_message(baseline)
            encode_samples.append((time.perf_counter_ns() - start) / 1_000)
            start = time.perf_counter_ns()
            target_graph.apply_state(baseline)
            apply_samples.append((time.perf_counter_ns() - start) / 1_000)

        reorder_samples = []
        root = source_stage.GetRootLayer()
        for iteration in range(args.graph_iterations):
            entries = list(source_graph.sublayers_for(source_graph.root_layer_key))
            if iteration % 2:
                entries.reverse()
            request = {
                "k": "set_sublayers",
                "prim": "/",
                "generation": source_graph.generation,
                "revision": source_graph.parent_revision(source_graph.root_layer_key),
                "sublayers": entries,
            }
            start = time.perf_counter_ns()
            prepared = source_graph.canonicalize_sublayers(
                source_graph.root_layer_key,
                request,
            )
            apply_sublayer_entries(root, prepared.event["sublayers"])
            source_graph.accept_sublayers(prepared)
            reorder_samples.append((time.perf_counter_ns() - start) / 1_000)
        graph_tracker.close()

    print(f"snapshot specs: {args.spec_count}")
    print(f"tracker initialization: {tracker_init:.2f} us")
    print(f"snapshot RSS high-water increase: {snapshot_rss / (1024 * 1024):.2f} MiB")
    _print_timing("single-field dirty diff", prepare_samples)
    _print_timing("complete dirty-layer fallback", fallback_samples)
    _print_timing("accepted-delta baseline update", commit_samples)
    print(f"graph layers: {args.graph_layers + 1}")
    print(f"initial graph capture: {graph_capture:.2f} us")
    _print_timing("client graph preflight", preflight_samples)
    _print_timing("idle graph tracker update", idle_samples)
    _print_timing("graph baseline encode", encode_samples)
    _print_timing("graph baseline apply", apply_samples)
    _print_timing("full-parent topology reorder", reorder_samples)
    tracker.close()


if __name__ == "__main__":
    main()
