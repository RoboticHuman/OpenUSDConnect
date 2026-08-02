#!/usr/bin/env python3
"""Measure Sdf spec deltas and emitter paths without transport or SQLite."""

from __future__ import annotations

import argparse
import statistics
import time

from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from openusdconnect.composed_projection import ComposedChangeProjection
from openusdconnect.emitter import NoticeEmitter
from openusdconnect.event_apply import apply_events
from openusdconnect.logical_layers import LogicalLayerRouter
from openusdconnect.protocol_constants import (
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_SET_SDF_SPEC_FIELDS,
    K_SET_XFORM_TRS,
)
from openusdconnect.sdf_spec_delta import (
    SDF_SPEC_KIND_ATTRIBUTE,
    apply_spec_delta,
    serialize_spec_fields,
)


def _measure(fn, iterations: int) -> tuple[float, float]:
    samples = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        fn()
        samples.append((time.perf_counter_ns() - start) / 1_000)
    p95 = samples[0] if len(samples) == 1 else statistics.quantiles(samples, n=100)[94]
    return statistics.median(samples), p95


def _print_timing(label: str, timing: tuple[float, float]) -> None:
    print(f"{label}: {timing[0]:.2f} us median, {timing[1]:.2f} us p95")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=2_000)
    parser.add_argument("--array-size", type=int, default=10_000)
    parser.add_argument("--spec-count", type=int, default=10_000)
    parser.add_argument("--snapshot-iterations", type=int, default=5)
    parser.add_argument("--projection-prims", type=int, default=1_000)
    parser.add_argument("--projection-iterations", type=int, default=3)
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
    metadata_fragment = serialize_spec_fields(
        layer,
        path,
        SDF_SPEC_KIND_ATTRIBUTE,
        {"documentation"},
    )
    full_fields = {str(field) for field in layer.GetPropertyAtPath(path).ListInfoKeys()}
    full_fragment = serialize_spec_fields(
        layer,
        path,
        SDF_SPEC_KIND_ATTRIBUTE,
        full_fields,
    )

    target = Usd.Stage.CreateInMemory()
    full_event = {
        "spec_path": path,
        "spec_kind": SDF_SPEC_KIND_ATTRIBUTE,
        "fields": sorted(full_fields),
        "fragment": full_fragment,
        "removed": False,
    }
    apply_spec_delta(target, full_event)
    metadata_event = {
        "spec_path": path,
        "spec_kind": SDF_SPEC_KIND_ATTRIBUTE,
        "fields": ["documentation"],
        "fragment": metadata_fragment,
        "removed": False,
    }

    metadata_write = _measure(
        lambda: serialize_spec_fields(
            layer,
            path,
            SDF_SPEC_KIND_ATTRIBUTE,
            {"documentation"},
        ),
        args.iterations,
    )
    metadata_apply = _measure(
        lambda: apply_spec_delta(target, metadata_event),
        args.iterations,
    )
    full_write = _measure(
        lambda: serialize_spec_fields(
            layer,
            path,
            SDF_SPEC_KIND_ATTRIBUTE,
            full_fields,
        ),
        max(20, args.iterations // 40),
    )
    full_apply = _measure(
        lambda: apply_spec_delta(target, full_event),
        max(20, args.iterations // 40),
    )

    transform_stage = Usd.Stage.CreateInMemory()
    xform = UsdGeom.Xform.Define(transform_stage, "/World/Thing")
    translate = xform.AddTranslateOp()
    translate.Set(Gf.Vec3d(0.0))
    transform_emitter = NoticeEmitter(transform_stage)
    transform_emitter.snapshot_events()
    transform_iteration = 0

    def _transform_notice() -> None:
        nonlocal transform_iteration
        transform_iteration += 1
        translate.Set(Gf.Vec3d(float(transform_iteration), 0.0, 0.0))
        events = transform_emitter.build_events_for_dirty()
        if not any(event["k"] == K_SET_XFORM_TRS for event in events):
            raise RuntimeError("transform benchmark edit produced no transform event")

    transform_timing = _measure(_transform_notice, args.iterations)
    transform_emitter.cleanup()

    metadata_stage = Usd.Stage.CreateInMemory()
    metadata_attr = metadata_stage.DefinePrim("/World/Thing", "Xform").CreateAttribute(
        "userProperties:value",
        Sdf.ValueTypeNames.Int,
        custom=True,
    )
    metadata_attr.Set(1)
    metadata_emitter = NoticeEmitter(metadata_stage)
    metadata_emitter.snapshot_events()
    metadata_iteration = 0

    def _metadata_notice() -> None:
        nonlocal metadata_iteration
        metadata_iteration += 1
        metadata_attr.SetDocumentation(f"iteration {metadata_iteration}")
        events = metadata_emitter.build_events_for_dirty()
        if not any(event["k"] == K_SET_SDF_SPEC_FIELDS for event in events):
            raise RuntimeError("metadata benchmark edit produced no Sdf event")

    metadata_timing = _measure(_metadata_notice, args.iterations)
    metadata_emitter.cleanup()

    variant_stage = Usd.Stage.CreateInMemory()
    variant_prim = variant_stage.DefinePrim("/World/Thing", "Xform")
    variants = variant_prim.GetVariantSets().AddVariantSet("look")
    variants.AddVariant("active")
    variants.AddVariant("inactive")
    variants.SetVariantSelection("active")
    variant_emitter = NoticeEmitter(variant_stage)
    variant_emitter.snapshot_events()
    inactive_path = Sdf.Path("/World/Thing{look=inactive}Probe")
    inactive_present = False

    def _inactive_variant_notice() -> None:
        nonlocal inactive_present
        variants.SetVariantSelection("inactive")
        with variants.GetVariantEditContext():
            if inactive_present:
                variant_stage.RemovePrim("/World/Thing/Probe")
            else:
                variant_stage.DefinePrim("/World/Thing/Probe", "Scope")
        variants.SetVariantSelection("active")
        inactive_present = not inactive_present
        events = variant_emitter.build_events_for_dirty()
        if not any(
            event["k"] == K_SET_SDF_SPEC_FIELDS and event["spec_path"] == str(inactive_path)
            for event in events
        ):
            raise RuntimeError("inactive variant benchmark edit produced no exact Sdf event")

    variant_timing = _measure(_inactive_variant_notice, args.iterations)
    variant_emitter.cleanup()

    snapshot_stage = Usd.Stage.CreateInMemory()
    for index in range(args.spec_count):
        prim = snapshot_stage.DefinePrim(f"/World/Prim_{index:05d}", "Xform")
        prim.SetDocumentation("snapshot benchmark")
    snapshot_emitter = NoticeEmitter(snapshot_stage)
    snapshot_event_count = len(snapshot_emitter.snapshot_events())
    snapshot_timing = _measure(
        snapshot_emitter.snapshot_events,
        args.snapshot_iterations,
    )
    snapshot_emitter.cleanup()

    projection_stage = Usd.Stage.CreateInMemory()
    projection_path = "/World/Thing"
    apply_events(
        projection_stage,
        [
            {"k": K_ENSURE_PRIM, "prim": projection_path, "typeName": "Xform"},
            {"k": K_ENSURE_XFORM_OPS, "prim": projection_path},
            {
                "k": K_SET_XFORM_TRS,
                "prim": projection_path,
                "fields": ["t"],
                "t": [0.0, 0.0, 0.0],
            },
        ],
    )
    projection_iteration = 0

    def _project_transform_change() -> None:
        nonlocal projection_iteration
        projection_iteration += 1
        event = {
            "k": K_SET_XFORM_TRS,
            "prim": projection_path,
            "fields": ["t"],
            "t": [float(projection_iteration), 0.0, 0.0],
        }
        projection = ComposedChangeProjection(projection_stage, [event])
        apply_events(projection_stage, [event])
        projected = projection.build_events()
        if not any(item["k"] == K_SET_XFORM_TRS for item in projected):
            raise RuntimeError("composed transform benchmark produced no projection")

    projection_transform_timing = _measure(
        _project_transform_change,
        args.iterations,
    )

    masked_stage = Usd.Stage.CreateInMemory()
    masked_router = LogicalLayerRouter(masked_stage)
    strong_key = "benchmark:strong"
    weak_key = "benchmark:weak"
    masked_router.apply_state(
        {
            "type": "layer_stack_state",
            "generation": "benchmark",
            "revision": 1,
            "layers": [
                {"layer_key": strong_key, "label": "Strong", "muted": False},
                {"layer_key": weak_key, "label": "Weak", "muted": False},
            ],
        }
    )
    for layer_key, value in ((strong_key, 2.0), (weak_key, 1.0)):
        with Usd.EditContext(masked_stage, masked_router.edit_target_for(layer_key)):
            apply_events(
                masked_stage,
                [
                    {"k": K_ENSURE_PRIM, "prim": projection_path, "typeName": "Xform"},
                    {"k": K_ENSURE_XFORM_OPS, "prim": projection_path},
                    {
                        "k": K_SET_XFORM_TRS,
                        "prim": projection_path,
                        "fields": ["t"],
                        "t": [value, 0.0, 0.0],
                    },
                ],
            )
    masked_stage.SetEditTarget(masked_router.edit_target_for(weak_key))
    masked_iteration = 0

    def _project_masked_transform() -> None:
        nonlocal masked_iteration
        masked_iteration += 1
        event = {
            "k": K_SET_XFORM_TRS,
            "prim": projection_path,
            "fields": ["t"],
            "t": [float(masked_iteration + 2), 0.0, 0.0],
        }
        projection = ComposedChangeProjection(masked_stage, [event])
        apply_events(masked_stage, [event])
        if projection.build_events():
            raise RuntimeError("masked transform benchmark produced a native projection")

    projection_masked_timing = _measure(
        _project_masked_transform,
        args.iterations,
    )

    reorder_stage = Usd.Stage.CreateInMemory()
    reorder_router = LogicalLayerRouter(reorder_stage)
    reorder_router.apply_state(
        {
            "type": "layer_stack_state",
            "generation": "benchmark",
            "revision": 1,
            "layers": [
                {"layer_key": strong_key, "label": "Strong", "muted": False},
                {"layer_key": weak_key, "label": "Weak", "muted": False},
            ],
        }
    )
    for index in range(args.projection_prims):
        prim_path = f"/World/Prim_{index:05d}"
        for layer_key, value in ((strong_key, 2.0), (weak_key, 1.0)):
            with Usd.EditContext(reorder_stage, reorder_router.edit_target_for(layer_key)):
                apply_events(
                    reorder_stage,
                    [
                        {"k": K_ENSURE_PRIM, "prim": prim_path, "typeName": "Xform"},
                        {"k": K_ENSURE_XFORM_OPS, "prim": prim_path},
                        {
                            "k": K_SET_XFORM_TRS,
                            "prim": prim_path,
                            "fields": ["t"],
                            "t": [value, 0.0, 0.0],
                        },
                    ],
                )
    reorder_revision = 1
    weak_first = False

    def _project_layer_reorder() -> None:
        nonlocal reorder_revision, weak_first
        paths, arcs = reorder_router.authored_projection_candidates()
        projection = ComposedChangeProjection(
            reorder_stage,
            [],
            extra_scene_paths=paths,
            extra_arc_candidates=arcs,
        )
        reorder_revision += 1
        weak_first = not weak_first
        keys = (weak_key, strong_key) if weak_first else (strong_key, weak_key)
        reorder_router.apply_state(
            {
                "type": "layer_stack_state",
                "generation": "benchmark",
                "revision": reorder_revision,
                "layers": [{"layer_key": key, "label": key, "muted": False} for key in keys],
            }
        )
        projected = projection.build_events()
        changed = sum(item["k"] == K_SET_XFORM_TRS for item in projected)
        if changed != args.projection_prims:
            raise RuntimeError(
                f"layer reorder benchmark projected {changed} transforms; "
                f"expected {args.projection_prims}"
            )

    projection_reorder_timing = _measure(
        _project_layer_reorder,
        args.projection_iterations,
    )
    masked_router.close()
    reorder_router.close()

    print(f"array elements: {args.array_size}")
    print(f"metadata fragment: {len(metadata_fragment.encode())} bytes")
    print(f"full fragment: {len(full_fragment.encode())} bytes")
    _print_timing("metadata serialize", metadata_write)
    _print_timing("metadata apply", metadata_apply)
    _print_timing("full serialize", full_write)
    _print_timing("full apply", full_apply)
    _print_timing("specialized transform notice", transform_timing)
    _print_timing("generic metadata notice", metadata_timing)
    _print_timing("inactive variant structural notice", variant_timing)
    print(f"snapshot specs: {args.spec_count}")
    print(f"snapshot events: {snapshot_event_count}")
    _print_timing("full snapshot", snapshot_timing)
    _print_timing("native transform projection", projection_transform_timing)
    _print_timing("masked transform projection", projection_masked_timing)
    print(f"layer reorder prims: {args.projection_prims}")
    _print_timing("native layer reorder projection", projection_reorder_timing)


if __name__ == "__main__":
    main()
