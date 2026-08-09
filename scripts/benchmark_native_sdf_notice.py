#!/usr/bin/env python3
"""Compare native Sdf delegate tracking with the complete Python layer tracker."""

from __future__ import annotations

import argparse
import ctypes
import difflib
import statistics
import sys
import tempfile
import time
from pathlib import Path

if sys.platform != "win32":
    import resource

from pxr import Sdf, Usd

from openusdconnect.event_apply import apply_events
from openusdconnect.sdf_delegate_bridge import NativeDelegateTracker, NativeSdfLayerChangeTracker
from openusdconnect.sdf_layer_tracker import SdfLayerChangeTracker
from openusdconnect.sdf_spec_delta import (
    serialize_spec_fields,
    spec_kind_for_object,
)
from openusdconnect.shared_layer_graph import SharedLayerGraph


def _median_us(samples: list[int]) -> float:
    return statistics.median(samples) / 1_000


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


def _dense_stage(spec_count: int):
    stage = Usd.Stage.CreateInMemory()
    prim = stage.DefinePrim("/World/Values", "Xform")
    attrs = []
    for index in range(spec_count):
        attr = prim.CreateAttribute(f"user:value_{index}", Sdf.ValueTypeNames.Int, True)
        attr.Set(index)
        attrs.append(attr)
    return stage, attrs


def _verify_change_coverage(library: Path) -> None:
    with tempfile.TemporaryDirectory() as directory:
        _verify_change_coverage_in_directory(library, Path(directory))


def _verify_change_coverage_in_directory(library: Path, directory: Path) -> None:
    root = Sdf.Layer.CreateNew(str(directory / "root.usda"))
    stage = Usd.Stage.Open(root)
    prim = stage.DefinePrim("/Model", "Xform")
    visible = prim.CreateAttribute("user:visible", Sdf.ValueTypeNames.Int, True)
    visible.Set(1)
    removed = prim.CreateAttribute("user:removed", Sdf.ValueTypeNames.Int, True)
    removed.Set(1)
    sampled = prim.CreateAttribute("user:sampled", Sdf.ValueTypeNames.Double, True)
    sampled.Set(1.0)
    source = prim.CreateAttribute("outputs:source", Sdf.ValueTypeNames.Float, True)
    source.Set(1.0)
    connected = prim.CreateAttribute("inputs:connected", Sdf.ValueTypeNames.Float, True)
    relation = prim.CreateRelationship("user:link", True)
    prim.CreateAttribute("user:rename_before", Sdf.ValueTypeNames.Int, True)

    prim_spec = root.GetPrimAtPath("/Model")
    variant_set = Sdf.VariantSetSpec(prim_spec, "look")
    Sdf.VariantSpec(variant_set, "active")
    inactive = Sdf.VariantSpec(variant_set, "inactive")
    removed_variant = Sdf.VariantSpec(variant_set, "removed")
    hidden = Sdf.AttributeSpec(inactive.primSpec, "user:hidden", Sdf.ValueTypeNames.Int)
    hidden.default = 1
    prim.GetVariantSet("look").SetVariantSelection("active")
    child = Sdf.Layer.CreateNew(str(directory / "child.usda"))
    Sdf.CreatePrimInLayer(child, "/Child")
    child.Save()
    asset = Sdf.Layer.CreateNew(str(directory / "asset.usda"))
    Sdf.CreatePrimInLayer(asset, "/Asset")
    asset.Save()
    graph = SharedLayerGraph(stage, authoritative=True)
    baseline = Sdf.Layer.CreateNew(str(directory / "baseline.usda"))
    baseline.TransferContent(root)

    exact_tracker = NativeSdfLayerChangeTracker(stage, graph, library)
    try:
        with Sdf.ChangeBlock():
            visible.SetDocumentation("changed")
            hidden.default = 2
            prim.RemoveProperty("user:removed")
            sampled.Set(2.0, 1.0)
            root.subLayerPaths.append("./child.usda")
            connected.AddConnection(source.GetPath())
            relation.AddTarget(Sdf.Path("/Model"))
            prim.GetReferences().AddReference("./asset.usda", "/Asset")
            prim.SetCustomDataByKey("benchmark:nested", 7)
            variant_set.RemoveVariant(removed_variant)
            edits = Sdf.BatchNamespaceEdit()
            edits.Add(
                Sdf.NamespaceEdit.Rename(
                    Sdf.Path("/Model.user:rename_before"),
                    "user:rename_after",
                )
            )
            assert root.Apply(edits)
        start = time.perf_counter_ns()
        exact_tracker.prepare_local_changes()
        routed = exact_tracker.next_routed_batch()
        if routed is None:
            raise RuntimeError("native tracker did not produce a routed batch")
        _batch, _layer_key, events = routed
        replay_build_samples = [time.perf_counter_ns() - start]
    finally:
        exact_tracker.close()

    target = Usd.Stage.Open(baseline)
    apply_events(target, events)
    target_text = target.GetRootLayer().ExportToString()
    source_text = root.ExportToString()
    if target_text != source_text:
        difference = "".join(
            difflib.unified_diff(
                target_text.splitlines(keepends=True),
                source_text.splitlines(keepends=True),
                fromfile="target",
                tofile="source",
            )
        )
        raise AssertionError(f"native replay did not reach exact parity:\n{difference}")
    print(
        f"coverage probe: {len(events)} replay events, exact layer parity, "
        f"{_median_us(replay_build_samples):.2f} us materialization"
    )


def _benchmark_python(spec_count: int, iterations: int) -> dict[str, float]:
    stage, attrs = _dense_stage(spec_count)
    graph = SharedLayerGraph(stage, authoritative=True)
    rss_before = _rss_bytes()
    start = time.perf_counter_ns()
    tracker = SdfLayerChangeTracker(stage, graph)
    initialization = time.perf_counter_ns() - start
    snapshot_bytes = max(0, _rss_bytes() - rss_before)
    author_samples = []
    prepare_samples = []
    commit_samples = []
    try:
        for iteration in range(iterations + 3):
            start = time.perf_counter_ns()
            attrs[0].SetDocumentation(f"python {iteration}")
            authored = time.perf_counter_ns() - start
            start = time.perf_counter_ns()
            batches = tracker.prepare_local_changes()
            prepared = time.perf_counter_ns() - start
            if len(batches) != 1 or len(batches[0].events) != 1:
                raise RuntimeError("Python tracker did not produce one event")
            start = time.perf_counter_ns()
            tracker.mark_prepared_sent(batches[0])
            committed = time.perf_counter_ns() - start
            if iteration >= 3:
                author_samples.append(authored)
                prepare_samples.append(prepared)
                commit_samples.append(committed)
    finally:
        tracker.close()
    return {
        "initialization_us": initialization / 1_000,
        "snapshot_mib": snapshot_bytes / (1024 * 1024),
        "author_us": _median_us(author_samples),
        "prepare_us": _median_us(prepare_samples),
        "commit_us": _median_us(commit_samples),
    }


def _materialize_record(layer: Sdf.Layer, record) -> dict:
    path = Sdf.Path(record.path)
    spec = layer.pseudoRoot if path == Sdf.Path.absoluteRootPath else layer.GetObjectAtPath(path)
    if spec is None:
        raise RuntimeError(f"native record points to missing spec {path}")
    kind = spec_kind_for_object(spec)
    return {
        "spec_path": str(path),
        "spec_kind": kind,
        "fields": record.fields,
        "fragment": serialize_spec_fields(
            layer,
            path,
            kind,
            record.fields,
            stabilize_asset_paths=False,
        ),
    }


def _benchmark_native(library: Path, spec_count: int, iterations: int) -> dict[str, float]:
    stage, attrs = _dense_stage(spec_count)
    root = stage.GetRootLayer()
    baseline_samples = []
    for iteration in range(iterations + 3):
        start = time.perf_counter_ns()
        attrs[0].SetDocumentation(f"baseline {iteration}")
        elapsed = time.perf_counter_ns() - start
        if iteration >= 3:
            baseline_samples.append(elapsed)

    author_samples = []
    drain_samples = []
    materialize_samples = []
    with NativeDelegateTracker(library, [root.identifier]) as tracker:
        for iteration in range(iterations + 3):
            start = time.perf_counter_ns()
            attrs[0].SetDocumentation(f"native {iteration}")
            authored = time.perf_counter_ns() - start
            start = time.perf_counter_ns()
            records = tracker.drain()
            drained = time.perf_counter_ns() - start
            if len(records) != 1 or records[0].fields != ("documentation",):
                raise RuntimeError(f"unexpected native records: {records!r}")
            start = time.perf_counter_ns()
            _materialize_record(root, records[0])
            materialized = time.perf_counter_ns() - start
            if iteration >= 3:
                author_samples.append(authored)
                drain_samples.append(drained)
                materialize_samples.append(materialized)

        with Sdf.ChangeBlock():
            for index, attr in enumerate(attrs):
                attr.SetDocumentation(f"burst {index}")
        burst_batches = tracker.pending_batch_count
        start = time.perf_counter_ns()
        burst_records = len(tracker.drain())
        burst_drain = time.perf_counter_ns() - start

    baseline = _median_us(baseline_samples)
    author = _median_us(author_samples)
    drain = _median_us(drain_samples)
    materialize = _median_us(materialize_samples)
    return {
        "baseline_author_us": baseline,
        "author_us": author,
        "callback_overhead_us": max(0.0, author - baseline),
        "drain_us": drain,
        "materialize_us": materialize,
        "total_us": author + drain + materialize,
        "burst_batches": burst_batches,
        "burst_records": burst_records,
        "burst_drain_us": burst_drain / 1_000,
    }


def _benchmark_native_tracker(
    library: Path,
    spec_count: int,
    iterations: int,
) -> dict[str, float]:
    stage, attrs = _dense_stage(spec_count)
    graph = SharedLayerGraph(stage, authoritative=True)
    rss_before = _rss_bytes()
    start = time.perf_counter_ns()
    tracker = NativeSdfLayerChangeTracker(stage, graph, library)
    initialization = time.perf_counter_ns() - start
    tracker_bytes = max(0, _rss_bytes() - rss_before)
    author_samples = []
    prepare_samples = []
    commit_samples = []
    try:
        for iteration in range(iterations + 3):
            start = time.perf_counter_ns()
            attrs[0].SetDocumentation(f"native tracker {iteration}")
            authored = time.perf_counter_ns() - start
            start = time.perf_counter_ns()
            batches = tracker.prepare_local_changes()
            prepared = time.perf_counter_ns() - start
            if len(batches) != 1 or len(batches[0].events) != 1:
                raise RuntimeError("native tracker did not produce one event")
            start = time.perf_counter_ns()
            tracker.mark_prepared_sent(batches[0])
            committed = time.perf_counter_ns() - start
            if iteration >= 3:
                author_samples.append(authored)
                prepare_samples.append(prepared)
                commit_samples.append(committed)
    finally:
        tracker.close()
    return {
        "initialization_us": initialization / 1_000,
        "tracker_mib": tracker_bytes / (1024 * 1024),
        "author_us": _median_us(author_samples),
        "prepare_us": _median_us(prepare_samples),
        "commit_us": _median_us(commit_samples),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--spec-count", type=int, default=10_000)
    parser.add_argument("--iterations", type=int, default=20)
    args = parser.parse_args()

    _verify_change_coverage(args.bridge)
    python = _benchmark_python(args.spec_count, args.iterations)
    native = _benchmark_native(args.bridge, args.spec_count, args.iterations)
    tracker = _benchmark_native_tracker(args.bridge, args.spec_count, args.iterations)

    print("\nPython tracker")
    print(f"  initialization: {python['initialization_us']:.2f} us")
    print(f"  snapshot RSS:   {python['snapshot_mib']:.2f} MiB")
    print(f"  author:         {python['author_us']:.2f} us median")
    print(f"  prepare:        {python['prepare_us']:.2f} us median")
    print(f"  commit:         {python['commit_us']:.2f} us median")

    print("\nNative delegate bridge")
    print(f"  baseline author:  {native['baseline_author_us']:.2f} us median")
    print(f"  author + callback:{native['author_us']:9.2f} us median")
    print(f"  callback overhead:{native['callback_overhead_us']:9.2f} us median")
    print(f"  ctypes drain:     {native['drain_us']:9.2f} us median")
    print(f"  event fragment:   {native['materialize_us']:9.2f} us median")
    print(f"  end-to-end:       {native['total_us']:9.2f} us median")
    print(
        "  burst queue:      "
        f"{native['burst_records']} records in {native['burst_batches']} batch, "
        f"{native['burst_drain_us']:.2f} us drain"
    )

    print("\nNative layer tracker")
    print(f"  initialization: {tracker['initialization_us']:.2f} us")
    print(f"  tracker RSS:    {tracker['tracker_mib']:.2f} MiB")
    print(f"  author:         {tracker['author_us']:.2f} us median")
    print(f"  prepare:        {tracker['prepare_us']:.2f} us median")
    print(f"  commit:         {tracker['commit_us']:.2f} us median")


if __name__ == "__main__":
    main()
