#!/usr/bin/env python3
"""Benchmark Sdf spec field transports: USDA text vs usdc crates vs per-field JSON.

Baseline (current): serialize_spec_fields() copies selected fields into a
scratch layer via Sdf.CopySpec, exports the scratch as USDA text
(ExportToString), and the receive side re-imports the text (ImportFromString)
before copying the fields onto the target layer.

Alternatives:
  B  Sdf.CopySpec into an anonymous layer exported as a binary .usdc crate
     (layer.Export), imported on the receive side (layer.Import), then copied
     onto the target layer. Same copy filter as the baseline, binary wire.
  C  Per-field value extraction via spec.GetInfo(field) encoded as JSON and
     applied on the receive side via spec.SetInfo(field, value), skipping the
     layer round trip entirely.
  D  scratch.TransferContent(source_layer) with every spec except the target
     stripped, exported as a .usdc crate, then TransferContent'ed into the
     target layer.

Each alternative is measured on encode time, cold and warm decode time, wire
payload size, and fidelity (target layer ExportToString equality against the
baseline output plus per-field GetInfo equality against the source spec).

Run: uv run python scripts/benchmark_sdf_transport.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import tempfile
import time

from pxr import Gf, Sdf, Usd, Vt

from openusdconnect.cli_common import positive_int
from openusdconnect.sdf_spec_delta import (
    SDF_SPEC_KIND_ATTRIBUTE,
    SDF_SPEC_KIND_PRIM,
    SDF_SPEC_KIND_RELATIONSHIP,
    SDF_SPEC_KIND_VARIANT,
    apply_spec_delta_to_layer,
    serialize_spec_fields,
)

# Mirror of the core's required declaration fields per spec kind.
_REQUIRED_FIELDS = {
    "layer": frozenset(),
    SDF_SPEC_KIND_PRIM: frozenset({"specifier"}),
    SDF_SPEC_KIND_ATTRIBUTE: frozenset({"custom", "typeName", "variability"}),
    SDF_SPEC_KIND_RELATIONSHIP: frozenset({"custom", "variability"}),
    "variant_set": frozenset(),
    SDF_SPEC_KIND_VARIANT: frozenset({"specifier"}),
}

_SPECIFIER_NAMES = (
    (Sdf.SpecifierDef, "def"),
    (Sdf.SpecifierOver, "over"),
    (Sdf.SpecifierClass, "class"),
)
_SPECIFIER_BY_NAME = {name: enum for enum, name in _SPECIFIER_NAMES}
_VARIABILITY_NAMES = (
    (Sdf.VariabilityVarying, "varying"),
    (Sdf.VariabilityUniform, "uniform"),
)
_VARIABILITY_BY_NAME = {name: enum for enum, name in _VARIABILITY_NAMES}

_VT_ARRAY_BUILDERS = {
    "Vec3fArray": lambda items: Vt.Vec3fArray([Gf.Vec3f(*item) for item in items]),
    "Vec3dArray": lambda items: Vt.Vec3dArray([Gf.Vec3d(*item) for item in items]),
}


def _specifier_name(value) -> str:
    for enum, name in _SPECIFIER_NAMES:
        if value == enum:
            return name
    raise TypeError(f"unknown Sdf.Specifier {value}")


def _variability_name(value) -> str:
    for enum, name in _VARIABILITY_NAMES:
        if value == enum:
            return name
    raise TypeError(f"unknown Sdf.Variability {value}")


def _to_json(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Sdf.Path):
        return {"__sdf__": "path", "v": str(value)}
    if isinstance(value, Sdf.AssetPath):
        return {"__sdf__": "asset_path", "v": value.path}
    if isinstance(value, Sdf.Specifier):
        return {"__sdf__": "specifier", "v": _specifier_name(value)}
    if isinstance(value, Sdf.Variability):
        return {"__sdf__": "variability", "v": _variability_name(value)}
    if isinstance(value, Sdf.PathListOp):
        return {
            "__sdf__": "path_list_op",
            "explicit": [str(p) for p in value.explicitItems],
            "added": [str(p) for p in value.addedItems],
            "prepended": [str(p) for p in value.prependedItems],
            "appended": [str(p) for p in value.appendedItems],
            "deleted": [str(p) for p in value.deletedItems],
        }
    if type(value).__module__ == "pxr.Gf":
        return {"__gf__": type(value).__name__, "v": [float(c) for c in value]}
    name = type(value).__name__
    if name in _VT_ARRAY_BUILDERS:
        return {"__vt__": name, "items": [[float(c) for c in item] for item in value]}
    raise TypeError(f"cannot JSON-encode {type(value).__name__}")


def _from_json(data):
    if not isinstance(data, dict):
        return data
    if "__sdf__" in data:
        tag = data["__sdf__"]
        if tag == "path":
            return Sdf.Path(data["v"])
        if tag == "asset_path":
            return Sdf.AssetPath(data["v"])
        if tag == "specifier":
            return _SPECIFIER_BY_NAME[data["v"]]
        if tag == "variability":
            return _VARIABILITY_BY_NAME[data["v"]]
        if tag == "path_list_op":
            return data  # applied via the targetPathList proxy, not SetInfo
        raise TypeError(f"unknown __sdf__ tag {tag}")
    if "__vt__" in data:
        builder = _VT_ARRAY_BUILDERS[data["__vt__"]]
        return builder([[float(c) for c in item] for item in data["items"]])
    if "__gf__" in data:
        return getattr(Gf, data["__gf__"])(*data["v"])
    raise TypeError(f"cannot decode {data!r}")


def _apply_field(spec, field: str, data) -> None:
    if field == "targetPaths":
        spec.targetPathList.explicitItems = [Sdf.Path(p) for p in data["explicit"]]
    elif field == "specifier":
        spec.SetInfo(field, _SPECIFIER_BY_NAME[data["v"]])
    elif field == "variability":
        spec.SetInfo(field, _VARIABILITY_BY_NAME[data["v"]])
    else:
        spec.SetInfo(field, _from_json(data))


def _apply_fields(spec, data: dict, kind: str) -> None:
    fields = [f for f in data if f != "typeName"]
    if kind == SDF_SPEC_KIND_ATTRIBUTE:
        fields = ["typeName"] + fields
    for field in fields:
        _apply_field(spec, field, data[field])


def _get_spec(layer, path, kind):
    if kind == "layer":
        return layer.pseudoRoot
    return layer.GetObjectAtPath(path)


def _prepare_destination(layer, path, kind) -> None:
    path = path if isinstance(path, Sdf.Path) else Sdf.Path(path)
    if kind == SDF_SPEC_KIND_PRIM:
        Sdf.CreatePrimInLayer(layer, path)
        return
    if kind in (SDF_SPEC_KIND_ATTRIBUTE, SDF_SPEC_KIND_RELATIONSHIP):
        Sdf.CreatePrimInLayer(layer, path.GetPrimPath())
        return
    owner = Sdf.CreatePrimInLayer(layer, path.GetPrimPath())
    if kind == SDF_SPEC_KIND_VARIANT:
        set_name, _variant_name = path.GetVariantSelection()
        if not owner.variantSets.get(set_name):
            Sdf.VariantSetSpec(owner, set_name)


def _copy_selected(
    source,
    source_path,
    target,
    target_path,
    kind: str,
    fields,
    include_required: bool,
) -> None:
    """Copy selected value fields without child specs, mirroring the core."""
    selected = set(fields)
    if include_required:
        selected |= _REQUIRED_FIELDS[kind]
    if not _get_spec(target, target_path, kind):
        _prepare_destination(target, target_path, kind)

    def _copy_value(spec_type, field, *args):
        return field in selected

    if not Sdf.CopySpec(source, source_path, target, target_path, _copy_value, lambda *a: False):
        raise RuntimeError(f"Sdf.CopySpec failed for {source_path}")


def _create_target_spec(layer, path, kind: str):
    path = path if isinstance(path, Sdf.Path) else Sdf.Path(path)
    if kind == SDF_SPEC_KIND_PRIM:
        return Sdf.CreatePrimInLayer(layer, path)
    if kind == SDF_SPEC_KIND_ATTRIBUTE:
        owner = Sdf.CreatePrimInLayer(layer, path.GetPrimPath())
        return Sdf.AttributeSpec(owner, path.name, Sdf.ValueTypeNames.Double)
    if kind == SDF_SPEC_KIND_RELATIONSHIP:
        owner = Sdf.CreatePrimInLayer(layer, path.GetPrimPath())
        return Sdf.RelationshipSpec(owner, path.name)
    if kind == SDF_SPEC_KIND_VARIANT:
        owner = Sdf.CreatePrimInLayer(layer, path.GetPrimPath())
        set_name, variant_name = path.GetVariantSelection()
        variantset = owner.variantSets.get(set_name)
        if not variantset:
            variantset = Sdf.VariantSetSpec(owner, set_name)
        return Sdf.VariantSpec(variantset, variant_name)
    raise ValueError(f"unsupported spec kind {kind}")


def _all_specs(layer):
    out = []

    def visit_prim(prim):
        out.append(prim)
        for child in prim.nameChildren:
            visit_prim(child)
        for prop in prim.properties:
            out.append(prop)
        for variantset in prim.variantSets.values():
            out.append(variantset)
            for variant in variantset.variants.values():
                out.append(variant)

    for prim in layer.rootPrims:
        visit_prim(prim)
    return out


def _remove_spec(layer, spec) -> None:
    if isinstance(spec, Sdf.VariantSpec):
        spec.owner.RemoveVariant(spec)
    elif isinstance(spec, Sdf.VariantSetSpec):
        del spec.owner.variantSets[spec.name]
    else:
        edits = Sdf.BatchNamespaceEdit()
        edits.Add(Sdf.NamespaceEdit.Remove(spec.path))
        if not layer.Apply(edits):
            raise RuntimeError(f"failed to remove {spec.path}")


def _strip_to_target(layer, path, kind) -> None:
    path = path if isinstance(path, Sdf.Path) else Sdf.Path(path)
    keep = set(path.GetPrefixes())
    if kind == SDF_SPEC_KIND_VARIANT:
        set_name, _variant_name = path.GetVariantSelection()
        keep.add(path.GetPrimPath().AppendVariantSelection(set_name, ""))
    specs = sorted(
        ((s.path, s) for s in _all_specs(layer)),
        key=lambda pair: len(pair[0].GetPrefixes()),
        reverse=True,
    )
    for spec_path, spec in specs:
        if spec_path not in keep and spec_path != Sdf.Path.absoluteRootPath:
            if layer.GetObjectAtPath(spec_path):
                _remove_spec(layer, spec)
    layer.subLayerPaths.clear()
    root = layer.pseudoRoot
    for field in root.ListInfoKeys():
        root.ClearInfo(field)


class _AltContext:
    def __init__(self, tmpdir: str):
        self.b_scratch = Sdf.Layer.CreateAnonymous("bench-b")
        self.d_scratch = Sdf.Layer.CreateAnonymous("bench-d")
        self.b_out = os.path.join(tmpdir, "payload_b.usdc")
        self.d_out = os.path.join(tmpdir, "payload_d.usdc")
        self.b_in = os.path.join(tmpdir, "incoming_b.usdc")
        self.d_in = os.path.join(tmpdir, "incoming_d.usdc")


def _encode_a(source, case):
    _name, path, kind, fields = case
    return serialize_spec_fields(source, path, kind, fields).encode("utf-8")


def _encode_b(ctx: _AltContext, source, case):
    _name, path, kind, fields = case
    scratch = ctx.b_scratch
    scratch.Clear()
    _copy_selected(source, path, scratch, path, kind, fields, include_required=True)
    scratch.Export(ctx.b_out)
    with open(ctx.b_out, "rb") as f:
        return f.read()


def _encode_c(source, case):
    _name, path, kind, fields = case
    spec = _get_spec(source, path, kind)
    selected = set(fields) | _REQUIRED_FIELDS[kind]
    data = {field: _to_json(spec.GetInfo(field)) for field in sorted(selected)}
    return json.dumps(data, separators=(",", ":")).encode("utf-8")


def _encode_d(ctx: _AltContext, source, case):
    _name, path, kind, fields = case
    scratch = ctx.d_scratch
    scratch.TransferContent(source)
    _strip_to_target(scratch, path, kind)
    scratch.Export(ctx.d_out)
    with open(ctx.d_out, "rb") as f:
        return f.read()


def _decode_a(_ctx, _source, case, payload, target):
    _name, path, kind, fields = case
    apply_spec_delta_to_layer(
        target,
        {
            "spec_path": str(path),
            "spec_kind": kind,
            "fields": list(fields),
            "fragment": payload.decode("utf-8"),
            "removed": False,
        },
    )


def _decode_b(ctx: _AltContext, _source, case, payload, target):
    _name, path, kind, fields = case
    with open(ctx.b_in, "wb") as f:
        f.write(payload)
    incoming = Sdf.Layer.CreateAnonymous("bench-b-in.usdc")
    incoming.Import(ctx.b_in)
    try:
        created = _get_spec(target, path, kind) is None
        _copy_selected(incoming, path, target, path, kind, fields, include_required=created)
    finally:
        incoming.Clear()


def _decode_c(_ctx, _source, case, payload, target):
    _name, path, kind, fields = case
    data = json.loads(payload)
    spec = _get_spec(target, path, kind)
    if not spec:
        spec = _create_target_spec(target, path, kind)
    _apply_fields(spec, data, kind)


def _decode_d(ctx: _AltContext, _source, case, payload, target):
    with open(ctx.d_in, "wb") as f:
        f.write(payload)
    incoming = Sdf.Layer.CreateAnonymous("bench-d-in.usdc")
    incoming.Import(ctx.d_in)
    try:
        target.TransferContent(incoming)
    finally:
        incoming.Clear()


def _measure(fn, iterations: int) -> tuple[float, float]:
    samples = []
    for _ in range(iterations):
        start = time.perf_counter_ns()
        fn()
        samples.append((time.perf_counter_ns() - start) / 1_000)
    p95 = samples[0] if len(samples) == 1 else statistics.quantiles(samples, n=100)[94]
    return statistics.median(samples), p95


def _check_fidelity(source, case, payloads, decoders, ctx):
    name, path, kind, fields = case
    expected = Sdf.Layer.CreateAnonymous("fid-expected")
    decoders["A"](case, payloads["A"], expected)
    expected_text = expected.ExportToString()
    source_spec = _get_spec(source, path, kind)
    checked_fields = sorted(set(fields) | _REQUIRED_FIELDS[kind])
    results = {}
    for alt, payload in payloads.items():
        target = Sdf.Layer.CreateAnonymous(f"fid-{alt}")
        decoders[alt](case, payload, target)
        text_ok = target.ExportToString() == expected_text
        target_spec = _get_spec(target, path, kind)
        semantic_ok = all(
            str(target_spec.GetInfo(field)) == str(source_spec.GetInfo(field))
            for field in checked_fields
        )
        results[alt] = (text_ok, semantic_ok)
    return results


def _build_scene(array_size: int) -> Sdf.Layer:
    layer = Sdf.Layer.CreateAnonymous("benchmark-source")

    prim = Sdf.CreatePrimInLayer(layer, "/Delta/PrimDelta")
    prim.SetInfo("specifier", Sdf.SpecifierOver)
    prim.SetInfo("kind", "component")
    prim.SetInfo("comment", "delta prim")

    attr_prim = Sdf.CreatePrimInLayer(layer, "/Delta/AttrDelta")
    attr = Sdf.AttributeSpec(
        attr_prim, "myAttr", Sdf.ValueTypeNames.Double, Sdf.VariabilityVarying, True
    )
    attr.SetInfo("default", 2.5)

    rel_prim = Sdf.CreatePrimInLayer(layer, "/Delta/RelDelta")
    rel = Sdf.RelationshipSpec(rel_prim, "myRel", False)
    rel.SetInfo("variability", Sdf.VariabilityVarying)
    rel.targetPathList.explicitItems = [Sdf.Path("/Delta/Other"), Sdf.Path("/Delta/Third")]

    variant_prim = Sdf.CreatePrimInLayer(layer, "/Delta/VariantDelta")
    variantset = Sdf.VariantSetSpec(variant_prim, "look")
    variant = Sdf.VariantSpec(variantset, "active")
    variant.SetInfo("specifier", Sdf.SpecifierDef)

    array_prim = Sdf.CreatePrimInLayer(layer, "/Delta/ArrayDelta")
    array_attr = Sdf.AttributeSpec(
        array_prim, "points", Sdf.ValueTypeNames.Float3Array, Sdf.VariabilityVarying, True
    )
    array_attr.SetInfo(
        "default",
        Vt.Vec3fArray(
            [Gf.Vec3f(float(i), float(i + 1), float(i + 2)) for i in range(array_size)]
        ),
    )
    return layer


CASES = [
    ("prim", "/Delta/PrimDelta", SDF_SPEC_KIND_PRIM, ("kind", "specifier", "comment")),
    (
        "attribute",
        "/Delta/AttrDelta.myAttr",
        SDF_SPEC_KIND_ATTRIBUTE,
        ("custom", "default", "typeName", "variability"),
    ),
    ("relationship", "/Delta/RelDelta.myRel", SDF_SPEC_KIND_RELATIONSHIP, ("targetPaths",)),
    ("variant", "/Delta/VariantDelta{look=active}", SDF_SPEC_KIND_VARIANT, ("specifier",)),
    (
        "array attribute",
        "/Delta/ArrayDelta.points",
        SDF_SPEC_KIND_ATTRIBUTE,
        ("custom", "default", "typeName", "variability"),
    ),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=positive_int, default=1_000)
    parser.add_argument("--array-iterations", type=positive_int, default=50)
    parser.add_argument("--array-size", type=positive_int, default=10_000)
    args = parser.parse_args()

    source = _build_scene(args.array_size)
    tmpdir = tempfile.mkdtemp(prefix="bench-sdf-transport-")
    try:
        ctx = _AltContext(tmpdir)
        encoders = {
            "A": lambda case: _encode_a(source, case),
            "B": lambda case: _encode_b(ctx, source, case),
            "C": lambda case: _encode_c(source, case),
            "D": lambda case: _encode_d(ctx, source, case),
        }
        decoders = {
            "A": lambda case, payload, target: _decode_a(ctx, source, case, payload, target),
            "B": lambda case, payload, target: _decode_b(ctx, source, case, payload, target),
            "C": lambda case, payload, target: _decode_c(ctx, source, case, payload, target),
            "D": lambda case, payload, target: _decode_d(ctx, source, case, payload, target),
        }

        print(f"pxr {Usd.GetVersion()}, array size {args.array_size}")
        payload_totals = {alt: 0 for alt in encoders}
        for case in CASES:
            name, _path, _kind, fields = case
            iterations = args.array_iterations if "array" in name else args.iterations
            payloads = {alt: encoders[alt](case) for alt in encoders}
            for alt in payloads:
                payload_totals[alt] += len(payloads[alt])
            fidelity = _check_fidelity(source, case, payloads, decoders, ctx)
            print(f"case={name} fields={','.join(fields)} iterations={iterations}")
            for alt in ("A", "B", "C", "D"):
                encode = _measure(
                    lambda alt=alt, case=case: encoders[alt](case),
                    iterations,
                )
                cold = _measure(
                    lambda alt=alt, case=case, payloads=payloads: decoders[alt](
                        case,
                        payloads[alt],
                        Sdf.Layer.CreateAnonymous("cold"),
                    ),
                    iterations,
                )
                warm_target = Sdf.Layer.CreateAnonymous("warm")
                decoders["A"](case, payloads["A"], warm_target)
                warm = _measure(
                    lambda alt=alt,
                    case=case,
                    payloads=payloads,
                    warm_target=warm_target: decoders[alt](case, payloads[alt], warm_target),
                    iterations,
                )
                text_ok, semantic_ok = fidelity[alt]
                print(
                    f"  {alt}"
                    f"  encode {encode[0]:9.1f}us (p95 {encode[1]:8.1f}us)"
                    f"  cold {cold[0]:9.1f}us (p95 {cold[1]:8.1f}us)"
                    f"  warm {warm[0]:9.1f}us (p95 {warm[1]:8.1f}us)"
                    f"  payload {len(payloads[alt]):6d}B"
                    f"  text {'PASS' if text_ok else 'FAIL'}"
                    f"  semantic {'PASS' if semantic_ok else 'FAIL'}"
                )
        print()
        print("total wire payload for one event of each case:")
        for alt in ("A", "B", "C", "D"):
            print(f"  {alt}: {payload_totals[alt]} bytes")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
