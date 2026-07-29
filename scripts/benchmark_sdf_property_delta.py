#!/usr/bin/env python3
"""Measure Sdf property field deltas without transport or SQLite."""

from __future__ import annotations

import argparse
import statistics
import time

from pxr import Sdf, Usd, Vt

from openusdconnect.sdf_property_delta import (
    apply_property_spec_delta,
    serialize_property_spec_fields,
)


def _measure(fn, iterations: int) -> tuple[float, float]:
    samples = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        fn()
        samples.append((time.perf_counter_ns() - start) / 1_000)
    return statistics.median(samples), statistics.quantiles(samples, n=100)[94]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=2_000)
    parser.add_argument("--array-size", type=int, default=10_000)
    args = parser.parse_args()

    source = Usd.Stage.CreateInMemory()
    prim = source.DefinePrim("/World/Thing", "Xform")
    attr = prim.CreateAttribute(
        "userProperties:points",
        Sdf.ValueTypeNames.Point3fArray,
        True,
    )
    attr.Set(
        Vt.Vec3fArray([(float(i), float(i + 1), float(i + 2)) for i in range(args.array_size)])
    )
    attr.SetDocumentation("metadata-only benchmark")

    path = "/World/Thing.userProperties:points"
    layer = source.GetRootLayer()
    metadata_fragment = serialize_property_spec_fields(layer, path, path, {"documentation"})
    full_fields = {str(field) for field in layer.GetPropertyAtPath(path).ListInfoKeys()}
    full_fragment = serialize_property_spec_fields(layer, path, path, full_fields)

    target = Usd.Stage.CreateInMemory()
    full_event = {
        "spec_path": path,
        "fields": sorted(full_fields),
        "fragment": full_fragment,
        "removed": False,
    }
    apply_property_spec_delta(target, full_event)
    metadata_event = {
        "spec_path": path,
        "fields": ["documentation"],
        "fragment": metadata_fragment,
        "removed": False,
    }

    metadata_write = _measure(
        lambda: serialize_property_spec_fields(layer, path, path, {"documentation"}),
        args.iterations,
    )
    metadata_apply = _measure(
        lambda: apply_property_spec_delta(target, metadata_event),
        args.iterations,
    )
    full_write = _measure(
        lambda: serialize_property_spec_fields(layer, path, path, full_fields),
        max(20, args.iterations // 40),
    )
    full_apply = _measure(
        lambda: apply_property_spec_delta(target, full_event),
        max(20, args.iterations // 40),
    )

    print(f"array elements: {args.array_size}")
    print(f"metadata fragment: {len(metadata_fragment.encode())} bytes")
    print(f"full fragment: {len(full_fragment.encode())} bytes")
    print(f"metadata serialize: {metadata_write[0]:.2f} us median, {metadata_write[1]:.2f} us p95")
    print(f"metadata apply: {metadata_apply[0]:.2f} us median, {metadata_apply[1]:.2f} us p95")
    print(f"full serialize: {full_write[0]:.2f} us median, {full_write[1]:.2f} us p95")
    print(f"full apply: {full_apply[0]:.2f} us median, {full_apply[1]:.2f} us p95")


if __name__ == "__main__":
    main()
