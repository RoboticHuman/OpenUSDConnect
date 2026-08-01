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
    elif kind in (SDF_SPEC_KIND_ATTRIBUTE, SDF_SPEC_KIND_RELATIONSHIP):
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
    return spec if spec_kind_for_object(spec) == kind else None


def _remove_namespace_spec(layer: Sdf.Layer, path: Sdf.Path) -> None:
    if not layer.GetObjectAtPath(path):
        return
    edits = Sdf.BatchNamespaceEdit()
    edits.Add(Sdf.NamespaceEdit.Remove(path))
    if not layer.Apply(edits):
        raise RuntimeError(f"failed to remove Sdf spec {path}")


def _remove_spec(layer: Sdf.Layer, path: Sdf.Path, kind: str) -> None:
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


def _copy_path_list(proxy, value) -> None:
    proxy.ClearEdits()
    if value.isExplicit:
        proxy.explicitItems = list(value.explicitItems)
        return
    proxy.addedItems = list(value.addedItems)
    proxy.prependedItems = list(value.prependedItems)
    proxy.appendedItems = list(value.appendedItems)
    proxy.deletedItems = list(value.deletedItems)
    proxy.orderedItems = list(value.orderedItems)


def _clear_field(spec, field: str) -> None:
    if field == "timeSamples" and isinstance(spec, Sdf.AttributeSpec):
        for time in tuple(spec.ListTimeSamples()):
            spec.EraseTimeSample(time)
        return
    if field == "connectionPaths" and isinstance(spec, Sdf.AttributeSpec):
        spec.connectionPathList.ClearEdits()
        return
    if field == "targetPaths" and isinstance(spec, Sdf.RelationshipSpec):
        spec.targetPathList.ClearEdits()
        return
    spec.ClearInfo(field)


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


def _set_composed_field(spec, field: str, value) -> None:
    if field == "timeSamples" and isinstance(spec, Sdf.AttributeSpec):
        _clear_field(spec, field)
        for time, sample in value.items():
            spec.SetTimeSample(float(time), sample)
        return
    if field == "connectionPaths" and isinstance(spec, Sdf.AttributeSpec):
        _copy_path_list(spec.connectionPathList, value)
        return
    if field == "targetPaths" and isinstance(spec, Sdf.RelationshipSpec):
        _copy_path_list(spec.targetPathList, value)
        return
    spec.SetInfo(field, value)


def _composed_property_field_value(prop: Usd.Property, field: str):
    if field == "custom":
        return prop.IsCustom()
    if isinstance(prop, Usd.Attribute):
        if field == "typeName":
            return prop.GetTypeName()
        if field == "variability":
            return prop.GetVariability()
        if field == "connectionPaths":
            value = Sdf.PathListOp()
            value.explicitItems = prop.GetConnections()
            return value
    if isinstance(prop, Usd.Relationship) and field == "targetPaths":
        value = Sdf.PathListOp()
        value.explicitItems = prop.GetTargets()
        return value
    return prop.GetMetadata(field)


def _composed_asset_source_layer(
    prop: Usd.Property,
    stack,
    fields: Iterable[str],
) -> tuple[bool, Sdf.Layer | None]:
    source_layer = None
    found = False
    for field in fields:
        if field in _ASSET_FREE_FIELDS or not prop.HasAuthoredMetadata(field):
            continue
        value = _composed_property_field_value(prop, field)
        if not value_contains_asset_path(value):
            continue
        found = True
        sources = [spec for spec in stack if spec.HasInfo(field)]
        if not sources or (isinstance(value, dict) and len(sources) > 1):
            return True, None
        field_layer = sources[0].layer
        if source_layer is not None and source_layer != field_layer:
            return True, None
        source_layer = field_layer
    return found, source_layer


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
) -> str:
    """Serialize selected fields and required declaration data to USDA."""
    spec_path = _spec_path(spec_path, spec_kind)
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
        if _fields_contain_asset_paths(source_spec, selected):
            stabilize_layer_asset_paths(
                scratch,
                source_layer,
                expression_variables=expression_variables,
                resolver_context=resolver_context,
            )
        return scratch.ExportToString()
    finally:
        scratch.Clear()


def serialize_composed_property_spec_fields(
    stage: Usd.Stage,
    event_path: str | Sdf.Path,
    spec_kind: str,
    fields: Iterable[str],
) -> str | None:
    """Serialize selected fields from one property's composed state."""
    if spec_kind not in (SDF_SPEC_KIND_ATTRIBUTE, SDF_SPEC_KIND_RELATIONSHIP):
        raise ValueError("composed projection is only defined for property specs")
    event_path = _spec_path(event_path, spec_kind)
    if event_path.ContainsPrimVariantSelection():
        return None
    prop = stage.GetPropertyAtPath(event_path)
    if not prop or not prop.IsValid():
        return None
    stack = prop.GetPropertyStack()
    if not stack:
        return None
    if spec_kind_for_object(stack[0]) != spec_kind:
        return None

    selected = set(str(field) for field in fields)
    scratch = _scratch_layer("compose")
    try:
        source_spec = stack[0]
        spec = _copy_spec_fields(
            source_spec.layer,
            source_spec.path,
            scratch,
            event_path,
            spec_kind,
            selected,
            include_required=True,
        )
        declaration_fields = _PROPERTY_DECLARATION_FIELDS & {
            str(key) for key in spec.ListInfoKeys()
        }
        for field in declaration_fields:
            value = _composed_property_field_value(prop, field)
            if spec.GetInfo(field) != value:
                _set_composed_field(spec, field, value)
        for field in selected - declaration_fields:
            if prop.HasAuthoredMetadata(field):
                _set_composed_field(
                    spec,
                    field,
                    _composed_property_field_value(prop, field),
                )
            else:
                _clear_field(spec, field)
        asset_fields = [
            str(field)
            for field in spec.ListInfoKeys()
            if value_contains_asset_path(_field_value(spec, str(field)))
        ]
        if asset_fields:
            _found, source_layer = _composed_asset_source_layer(
                prop,
                stack,
                asset_fields,
            )
            if source_layer is None:
                flattened = stage.Flatten()
                return serialize_spec_fields(
                    flattened,
                    event_path,
                    spec_kind,
                    selected,
                )
            stabilize_layer_asset_paths(
                scratch,
                source_layer,
                resolver_context=stage.GetPathResolverContext(),
                use_evaluated_paths=True,
            )
        return scratch.ExportToString()
    finally:
        scratch.Clear()


def composed_property_spec_requires_flattening(stage: Usd.Stage, event: dict) -> bool:
    """Return whether a flat property correction needs USD layer flattening."""
    requested_kind = event["spec_kind"]
    if requested_kind not in (SDF_SPEC_KIND_ATTRIBUTE, SDF_SPEC_KIND_RELATIONSHIP):
        return False
    path = _spec_path(event["spec_path"], requested_kind)
    if path.ContainsPrimVariantSelection():
        return True

    prop = stage.GetPropertyAtPath(path)
    if not prop or not prop.IsValid():
        return False
    stack = prop.GetPropertyStack()
    if not stack:
        return False

    composed_kind = spec_kind_for_object(stack[0])
    fields = set(str(field) for field in event.get("fields", ()))
    if composed_kind != requested_kind or event.get("removed", False):
        fields.update(
            str(key)
            for spec in stack
            if spec_kind_for_object(spec) == composed_kind
            for key in spec.ListInfoKeys()
        )

    found, source_layer = _composed_asset_source_layer(prop, stack, fields)
    return found and source_layer is None


def composed_property_spec_event(stage: Usd.Stage, event: dict) -> dict | None:
    """Build a flat-projection event for a property's composed state."""
    if composed_property_spec_requires_flattening(stage, event):
        return composed_layer_spec_event(
            stage.Flatten(),
            event,
        )

    requested_kind = event["spec_kind"]
    if requested_kind not in (SDF_SPEC_KIND_ATTRIBUTE, SDF_SPEC_KIND_RELATIONSHIP):
        return None
    path = _spec_path(event["spec_path"], requested_kind)
    fields = set(str(field) for field in event.get("fields", ()))
    prop = stage.GetPropertyAtPath(path)
    composed_kind = requested_kind
    if prop and prop.IsValid():
        stack = prop.GetPropertyStack()
        if stack:
            composed_kind = spec_kind_for_object(stack[0])
        if composed_kind != requested_kind:
            fields.clear()
            for spec in stack:
                if spec_kind_for_object(spec) == composed_kind:
                    fields.update(str(key) for key in spec.ListInfoKeys())
        elif event.get("removed", False):
            for spec in stack:
                if spec_kind_for_object(spec) == composed_kind:
                    fields.update(str(key) for key in spec.ListInfoKeys())

    fragment = serialize_composed_property_spec_fields(
        stage,
        path,
        composed_kind,
        fields,
    )
    return {
        "k": event["k"],
        "prim": event_prim_path(path, composed_kind),
        "spec_path": str(path),
        "spec_kind": composed_kind,
        "fields": sorted(fields),
        "fragment": fragment or "",
        "removed": fragment is None,
    }


def composed_layer_spec_event(composed_layer: Sdf.Layer, event: dict) -> dict:
    """Build a flat-projection event from a flattened USD layer stack."""
    requested_kind = event["spec_kind"]
    path = _spec_path(event["spec_path"], requested_kind)
    source_spec = (
        composed_layer.pseudoRoot
        if requested_kind == SDF_SPEC_KIND_LAYER
        else composed_layer.GetObjectAtPath(path)
    )
    if not source_spec:
        return {
            "k": event["k"],
            "prim": event["prim"],
            "spec_path": str(path),
            "spec_kind": requested_kind,
            "fields": sorted(set(str(field) for field in event.get("fields", ()))),
            "fragment": "",
            "removed": True,
        }

    composed_kind = spec_kind_for_object(source_spec)
    fields = set(str(field) for field in event.get("fields", ()))
    if composed_kind != requested_kind:
        fields.clear()
        fields.update(str(key) for key in source_spec.ListInfoKeys())
    elif event.get("removed", False):
        fields.update(str(key) for key in source_spec.ListInfoKeys())
    fields.difference_update(SDF_LAYER_TOPOLOGY_FIELDS)
    return {
        "k": event["k"],
        "prim": event_prim_path(path, composed_kind),
        "spec_path": str(path),
        "spec_kind": composed_kind,
        "fields": sorted(fields),
        "fragment": serialize_spec_fields(
            composed_layer,
            path,
            composed_kind,
            fields,
        ),
        "removed": False,
    }


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
    kind, event_path, fields = _spec_delta_info(event)
    target_layer = stage.GetEditTarget().GetLayer()

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
        stage = Usd.Stage.Open(output)
        apply_spec_delta(stage, previous)
        apply_spec_delta(stage, current)
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
    "SDF_SPEC_KIND_RELATIONSHIP",
    "SDF_SPEC_KIND_VARIANT",
    "SDF_SPEC_KIND_VARIANT_SET",
    "apply_spec_delta",
    "composed_layer_spec_event",
    "composed_property_spec_event",
    "composed_property_spec_requires_flattening",
    "event_prim_path",
    "fragment_authored_fields",
    "merge_spec_events",
    "serialize_composed_property_spec_fields",
    "serialize_spec_fields",
    "spec_kind_for_object",
    "validate_spec_delta",
]
