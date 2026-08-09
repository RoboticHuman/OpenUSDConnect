"""Exact authored field deltas for Sdf specs outside specialized events."""

from __future__ import annotations

import threading
from collections.abc import Iterable

from pxr import Ar, Sdf, Usd

from .asset_paths import stabilize_layer_asset_paths, value_contains_asset_path
from .protocol_constants import (
    SDF_LAYER_TOPOLOGY_FIELDS,
    SDF_SPEC_KIND_ATTRIBUTE,
    SDF_SPEC_KIND_LAYER,
    SDF_SPEC_KIND_PRIM,
    SDF_SPEC_KIND_PROPERTY,
    SDF_SPEC_KIND_RELATIONSHIP,
    SDF_SPEC_KIND_VARIANT,
    SDF_SPEC_KIND_VARIANT_SET,
    SDF_SPEC_KINDS,
)

_PROPERTY_DECLARATION_FIELDS = frozenset({"custom", "typeName", "variability"})
_ASSET_FREE_FIELDS = _PROPERTY_DECLARATION_FIELDS | frozenset({"documentation", "specifier"})
_REQUIRED_FIELDS = {
    SDF_SPEC_KIND_LAYER: frozenset(),
    SDF_SPEC_KIND_PRIM: frozenset({"specifier"}),
    SDF_SPEC_KIND_ATTRIBUTE: _PROPERTY_DECLARATION_FIELDS,
    SDF_SPEC_KIND_RELATIONSHIP: frozenset({"custom", "variability"}),
    SDF_SPEC_KIND_VARIANT_SET: frozenset(),
    SDF_SPEC_KIND_VARIANT: frozenset({"specifier"}),
}
_RESERVED_LAYER_DATA_KEY = "openusdconnect"
# USDA omits empty variant sets, so fragments use a child that is never applied.
_FRAGMENT_VARIANT_NAME = "__openusdconnect_fragment__"
# Mutable scratch layers are reused per thread and cleared by each caller.
_SCRATCH = threading.local()


def _scratch_layer(name: str) -> Sdf.Layer:
    layers = getattr(_SCRATCH, "layers", None)
    if layers is None:
        layers = {}
        _SCRATCH.layers = layers
    layer = layers.get(name)
    if layer is None:
        layer = Sdf.Layer.CreateAnonymous(f"openusdconnect-{name}")
        layers[name] = layer
    return layer


def _spec_path(value: str | Sdf.Path, kind: str) -> Sdf.Path:
    if kind not in SDF_SPEC_KINDS:
        raise ValueError(f"unknown Sdf spec kind {kind!r}")
    path = value if isinstance(value, Sdf.Path) else Sdf.Path(value)
    if not path.IsAbsolutePath():
        raise ValueError(f"expected an absolute Sdf spec path, got {path}")
    if kind == SDF_SPEC_KIND_LAYER:
        valid = path == Sdf.Path.absoluteRootPath
    elif kind == SDF_SPEC_KIND_PRIM:
        valid = path.IsPrimPath()
    elif kind in (
        SDF_SPEC_KIND_ATTRIBUTE,
        SDF_SPEC_KIND_RELATIONSHIP,
        SDF_SPEC_KIND_PROPERTY,
    ):
        valid = path.IsPropertyPath()
    elif kind == SDF_SPEC_KIND_VARIANT_SET:
        valid = path.IsPrimVariantSelectionPath() and not path.GetVariantSelection()[1]
    else:
        valid = path.IsPrimVariantSelectionPath() and bool(path.GetVariantSelection()[1])
    if not valid:
        raise ValueError(f"path {path} is not valid for Sdf spec kind {kind!r}")
    return path


def spec_kind_for_object(spec) -> str:
    """Return the wire kind for one concrete Sdf spec object."""
    if isinstance(spec, Sdf.PseudoRootSpec):
        return SDF_SPEC_KIND_LAYER
    if isinstance(spec, Sdf.VariantSetSpec):
        return SDF_SPEC_KIND_VARIANT_SET
    if isinstance(spec, Sdf.VariantSpec):
        return SDF_SPEC_KIND_VARIANT
    if isinstance(spec, Sdf.AttributeSpec):
        return SDF_SPEC_KIND_ATTRIBUTE
    if isinstance(spec, Sdf.RelationshipSpec):
        return SDF_SPEC_KIND_RELATIONSHIP
    if isinstance(spec, Sdf.PrimSpec):
        return SDF_SPEC_KIND_PRIM
    raise TypeError(f"unsupported Sdf spec type {type(spec).__name__}")


def event_prim_path(spec_path: str | Sdf.Path, kind: str) -> str:
    """Return the scene prim used to route an exact Sdf spec event."""
    path = _spec_path(spec_path, kind)
    if kind == SDF_SPEC_KIND_LAYER:
        return "/"
    return str(path.GetPrimPath().StripAllVariantSelections())


def _get_spec(layer: Sdf.Layer, path: Sdf.Path, kind: str):
    spec = layer.pseudoRoot if kind == SDF_SPEC_KIND_LAYER else layer.GetObjectAtPath(path)
    if not spec:
        return None
    if kind == SDF_SPEC_KIND_PROPERTY:
        return spec if isinstance(spec, Sdf.PropertySpec) else None
    return spec if spec_kind_for_object(spec) == kind else None


def _remove_namespace_spec(layer: Sdf.Layer, path: Sdf.Path) -> None:
    if not layer.GetObjectAtPath(path):
        return
    edits = Sdf.BatchNamespaceEdit()
    edits.Add(Sdf.NamespaceEdit.Remove(path))
    if not layer.Apply(edits):
        raise RuntimeError(f"failed to remove Sdf spec {path}")


def _remove_spec(layer: Sdf.Layer, path: Sdf.Path, kind: str) -> None:
    if kind == SDF_SPEC_KIND_PROPERTY:
        _remove_namespace_spec(layer, path)
        return
    spec = _get_spec(layer, path, kind)
    if not spec:
        if kind == SDF_SPEC_KIND_VARIANT_SET:
            owner = layer.GetPrimAtPath(path.GetPrimPath())
            if owner:
                owner.variantSetNameList.RemoveItemEdits(path.GetVariantSelection()[0])
        return
    if kind == SDF_SPEC_KIND_LAYER:
        raise ValueError("the pseudo-root spec cannot be removed")
    if kind == SDF_SPEC_KIND_VARIANT:
        spec.owner.RemoveVariant(spec)
        return
    if kind == SDF_SPEC_KIND_VARIANT_SET:
        del spec.owner.variantSets[spec.name]
        return
    _remove_namespace_spec(layer, path)


def _field_value(source_spec, field: str):
    value = source_spec.GetInfo(field)
    if isinstance(source_spec, Sdf.PseudoRootSpec) and field == "customLayerData":
        value = dict(value)
        value.pop(_RESERVED_LAYER_DATA_KEY, None)
    return value


def _fields_contain_asset_paths(spec, fields: Iterable[str]) -> bool:
    for field in fields:
        if field in _ASSET_FREE_FIELDS or not spec.HasInfo(field):
            continue
        if value_contains_asset_path(_field_value(spec, field)):
            return True
    return False


def _prepare_copy_destination(layer: Sdf.Layer, path: Sdf.Path, kind: str) -> None:
    if kind == SDF_SPEC_KIND_LAYER:
        return
    if kind in (
        SDF_SPEC_KIND_PRIM,
        SDF_SPEC_KIND_ATTRIBUTE,
        SDF_SPEC_KIND_RELATIONSHIP,
    ):
        Sdf.CreatePrimInLayer(layer, path.GetParentPath())
        return

    owner = Sdf.CreatePrimInLayer(layer, path.GetPrimPath())
    if kind == SDF_SPEC_KIND_VARIANT:
        set_name = path.GetVariantSelection()[0]
        if not owner.variantSets.get(set_name):
            Sdf.VariantSetSpec(owner, set_name)


def _copy_spec_fields(
    source_layer: Sdf.Layer,
    source_path: Sdf.Path,
    target_layer: Sdf.Layer,
    target_path: Sdf.Path,
    kind: str,
    fields: Iterable[str],
    *,
    include_required: bool,
    preserve_target_layer_data: bool = False,
):
    """Copy selected value fields without copying child specs."""
    selected = set(fields)
    if include_required:
        selected.update(_REQUIRED_FIELDS[kind])
    source_spec = _get_spec(source_layer, source_path, kind)
    target_spec = _get_spec(target_layer, target_path, kind)
    if not target_spec:
        _prepare_copy_destination(target_layer, target_path, kind)

    def _should_copy_value(
        _spec_type,
        field,
        _source_layer,
        _source_path,
        field_in_source,
        _target_layer,
        _target_path,
        _field_in_target,
    ):
        if field not in selected:
            return False
        if kind == SDF_SPEC_KIND_LAYER and field == "customLayerData":
            value = dict(_field_value(source_spec, field)) if field_in_source else {}
            if preserve_target_layer_data and target_spec and target_spec.HasInfo(field):
                current = dict(target_spec.GetInfo(field))
                if _RESERVED_LAYER_DATA_KEY in current:
                    value[_RESERVED_LAYER_DATA_KEY] = current[_RESERVED_LAYER_DATA_KEY]
            if value or field_in_source:
                return True, value
        return True

    if not Sdf.CopySpec(
        source_layer,
        source_path,
        target_layer,
        target_path,
        _should_copy_value,
        lambda *_args: False,
    ):
        raise RuntimeError(f"failed to copy Sdf spec {source_path} to {target_path}")
    target_spec = _get_spec(target_layer, target_path, kind)
    if not target_spec:
        raise RuntimeError(f"copy did not create {kind} spec {target_path}")
    return target_spec


def serialize_spec_fields(
    source_layer: Sdf.Layer,
    spec_path: str | Sdf.Path,
    spec_kind: str,
    fields: Iterable[str],
    *,
    expression_variables: dict | None = None,
    resolver_context: Ar.ResolverContext | None = None,
    stabilize_asset_paths: bool = True,
) -> str:
    """Serialize selected fields and required declaration data to USDA."""
    spec_path = _spec_path(spec_path, spec_kind)
    if spec_kind == SDF_SPEC_KIND_PROPERTY:
        raise ValueError("the generic property kind cannot carry authored fields")
    source_spec = _get_spec(source_layer, spec_path, spec_kind)
    if not source_spec:
        raise ValueError(f"source layer has no {spec_kind} spec at {spec_path}")

    selected = tuple(dict.fromkeys(str(field) for field in fields))
    if spec_kind == SDF_SPEC_KIND_LAYER and set(selected) & SDF_LAYER_TOPOLOGY_FIELDS:
        raise ValueError("sublayer topology is not a field-delta operation")

    scratch = _scratch_layer("serialize")
    try:
        target_spec = _copy_spec_fields(
            source_layer,
            spec_path,
            scratch,
            spec_path,
            spec_kind,
            selected,
            include_required=True,
        )
        if spec_kind == SDF_SPEC_KIND_VARIANT_SET and not target_spec.variants:
            Sdf.VariantSpec(target_spec, _FRAGMENT_VARIANT_NAME)
        if stabilize_asset_paths and _fields_contain_asset_paths(source_spec, selected):
            stabilize_layer_asset_paths(
                scratch,
                source_layer,
                expression_variables=expression_variables,
                resolver_context=resolver_context,
            )
        return scratch.ExportToString()
    finally:
        scratch.Clear()


def serialize_layer_content(source_layer: Sdf.Layer) -> str:
    """Serialize all authored opinions except managed sublayer topology."""
    scratch = _scratch_layer("serialize-layer-content")
    try:
        scratch.TransferContent(source_layer)
        scratch.subLayerPaths.clear()
        custom_data = dict(scratch.customLayerData)
        custom_data.pop(_RESERVED_LAYER_DATA_KEY, None)
        if custom_data:
            scratch.customLayerData = custom_data
        else:
            scratch.pseudoRoot.ClearInfo("customLayerData")
        return scratch.ExportToString()
    finally:
        scratch.Clear()


def _replacement_layer(fragment: str) -> Sdf.Layer:
    if not isinstance(fragment, str) or not fragment:
        raise ValueError("replace_sdf_layer_content requires a valid Sdf fragment")
    incoming = _scratch_layer("replace-layer-content")
    incoming.Clear()
    if not incoming.ImportFromString(fragment):
        raise ValueError("replace_sdf_layer_content requires a valid Sdf fragment")
    if incoming.subLayerPaths:
        incoming.Clear()
        raise ValueError("layer content replacement cannot author sublayer topology")
    return incoming


def validate_layer_content_replacement(event: dict) -> None:
    """Validate one complete non-topology layer replacement."""
    incoming = _replacement_layer(event.get("fragment", ""))
    incoming.Clear()


def apply_layer_content_replacement(target_layer: Sdf.Layer, event: dict) -> None:
    """Replace authored content while retaining graph and collaboration data."""
    incoming = _replacement_layer(event.get("fragment", ""))
    paths = list(target_layer.subLayerPaths)
    offsets = list(target_layer.subLayerOffsets)
    reserved_data = target_layer.customLayerData.get(_RESERVED_LAYER_DATA_KEY)
    try:
        custom_data = dict(incoming.customLayerData)
        custom_data.pop(_RESERVED_LAYER_DATA_KEY, None)
        if reserved_data is not None:
            custom_data[_RESERVED_LAYER_DATA_KEY] = reserved_data
        if custom_data:
            incoming.customLayerData = custom_data
        else:
            incoming.pseudoRoot.ClearInfo("customLayerData")
        with Sdf.ChangeBlock():
            target_layer.TransferContent(incoming)
            target_layer.subLayerPaths.clear()
            for index, path in enumerate(paths):
                target_layer.subLayerPaths.append(path)
                target_layer.subLayerOffsets[index] = offsets[index]
    finally:
        incoming.Clear()


def fragment_authored_fields(
    fragment: str,
    spec_path: str | Sdf.Path,
    spec_kind: str,
) -> set[str]:
    """Return fields authored on one exact spec in a fragment."""
    path = _spec_path(spec_path, spec_kind)
    incoming = _scratch_layer("inspect")
    try:
        if not incoming.ImportFromString(fragment):
            raise ValueError("invalid Sdf spec fragment")
        spec = _get_spec(incoming, path, spec_kind)
        if not spec:
            raise ValueError(f"fragment has no {spec_kind} spec at {path}")
        return {str(key) for key in spec.ListInfoKeys()}
    finally:
        incoming.Clear()


def _spec_delta_info(event: dict) -> tuple[str, Sdf.Path, tuple[str, ...]]:
    kind = event["spec_kind"]
    path = _spec_path(event["spec_path"], kind)
    prim_path = event.get("prim")
    expected_prim_path = event_prim_path(path, kind)
    if prim_path is not None and prim_path != expected_prim_path:
        raise ValueError(f"Sdf spec path {path} belongs to {expected_prim_path}, not {prim_path}")
    raw_fields = event.get("fields", ())
    if not isinstance(raw_fields, (list, tuple)):
        raise ValueError("Sdf spec fields must be a list")
    if not all(isinstance(field, str) and field for field in raw_fields):
        raise ValueError("Sdf spec field names must be non-empty strings")
    fields = tuple(dict.fromkeys(raw_fields))
    if kind == SDF_SPEC_KIND_LAYER and set(fields) & SDF_LAYER_TOPOLOGY_FIELDS:
        raise ValueError("sublayer topology is not a field-delta operation")
    removed = event.get("removed", False)
    if not isinstance(removed, bool):
        raise ValueError("Sdf spec removed state must be boolean")
    if removed and kind == SDF_SPEC_KIND_LAYER:
        raise ValueError("the pseudo-root spec cannot be removed")
    if kind == SDF_SPEC_KIND_PROPERTY and not removed:
        raise ValueError("the generic property kind is valid only for removal")
    return kind, path, fields


def validate_spec_delta(event: dict) -> None:
    """Validate one exact Sdf event without mutating its destination layer."""
    kind, path, _fields = _spec_delta_info(event)
    if event.get("removed", False):
        return
    fragment = event.get("fragment", "")
    if not isinstance(fragment, str) or not fragment:
        raise ValueError("set_sdf_spec_fields requires a valid Sdf fragment")
    fragment_authored_fields(fragment, path, kind)


def apply_spec_delta(stage: Usd.Stage, event: dict) -> None:
    """Apply one ``set_sdf_spec_fields`` event to the current layer."""
    apply_spec_delta_to_layer(stage.GetEditTarget().GetLayer(), event)


def apply_spec_delta_to_layer(target_layer: Sdf.Layer, event: dict) -> None:
    """Apply one exact field delta directly to an ``Sdf.Layer``."""
    kind, event_path, fields = _spec_delta_info(event)

    if event.get("removed", False):
        _remove_spec(target_layer, event_path, kind)
        return

    fragment = event.get("fragment", "")
    incoming = _scratch_layer("apply")
    try:
        if not isinstance(fragment, str) or not fragment or not incoming.ImportFromString(fragment):
            raise ValueError("set_sdf_spec_fields requires a valid Sdf fragment")
        source_spec = _get_spec(incoming, event_path, kind)
        if not source_spec:
            raise ValueError(f"fragment has no {kind} spec at {event_path}")

        target_spec = (
            target_layer.pseudoRoot
            if kind == SDF_SPEC_KIND_LAYER
            else (target_layer.GetObjectAtPath(event_path))
        )
        if target_spec and spec_kind_for_object(target_spec) != kind:
            _remove_namespace_spec(target_layer, event_path)
            target_spec = None
        created = target_spec is None
        _copy_spec_fields(
            incoming,
            event_path,
            target_layer,
            event_path,
            kind,
            fields,
            include_required=created,
            preserve_target_layer_data=True,
        )
    finally:
        incoming.Clear()


def merge_spec_events(previous: dict, current: dict) -> dict:
    """Merge two deltas for one exact Sdf spec."""
    if (
        previous["spec_path"] != current["spec_path"]
        or previous["spec_kind"] != current["spec_kind"]
    ):
        raise ValueError("cannot merge deltas for different Sdf specs")
    if current.get("removed", False) or previous.get("removed", False):
        return dict(current)

    kind = current["spec_kind"]
    path = _spec_path(current["spec_path"], kind)
    output = _scratch_layer("merge-output")
    try:
        apply_spec_delta_to_layer(output, previous)
        apply_spec_delta_to_layer(output, current)
        fields = list(
            dict.fromkeys(
                [
                    *previous.get("fields", ()),
                    *current.get("fields", ()),
                ]
            )
        )
        merged = dict(current)
        merged["fields"] = fields
        merged["fragment"] = serialize_spec_fields(
            output,
            path,
            kind,
            fields,
            stabilize_asset_paths=False,
        )
        merged["removed"] = False
        return merged
    finally:
        output.Clear()


__all__ = [
    "SDF_LAYER_TOPOLOGY_FIELDS",
    "SDF_SPEC_KINDS",
    "SDF_SPEC_KIND_ATTRIBUTE",
    "SDF_SPEC_KIND_LAYER",
    "SDF_SPEC_KIND_PRIM",
    "SDF_SPEC_KIND_PROPERTY",
    "SDF_SPEC_KIND_RELATIONSHIP",
    "SDF_SPEC_KIND_VARIANT",
    "SDF_SPEC_KIND_VARIANT_SET",
    "apply_spec_delta_to_layer",
    "apply_spec_delta",
    "apply_layer_content_replacement",
    "event_prim_path",
    "fragment_authored_fields",
    "merge_spec_events",
    "serialize_spec_fields",
    "serialize_layer_content",
    "spec_kind_for_object",
    "validate_spec_delta",
    "validate_layer_content_replacement",
]
