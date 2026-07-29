"""Sdf property deltas for registered fields outside specialized events."""

from __future__ import annotations

import threading

from pxr import Sdf, Usd

_DECLARATION_FIELDS = frozenset({"custom", "typeName", "variability"})
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


def _property_path(value: str | Sdf.Path) -> Sdf.Path:
    path = value if isinstance(value, Sdf.Path) else Sdf.Path(value)
    if not path.IsAbsolutePath() or not path.IsPropertyPath():
        raise ValueError(f"expected an absolute property path, got {path}")
    return path


def _remove_property(layer: Sdf.Layer, path: Sdf.Path) -> None:
    if not layer.GetPropertyAtPath(path):
        return
    edits = Sdf.BatchNamespaceEdit()
    edits.Add(Sdf.NamespaceEdit.Remove(path))
    if not layer.Apply(edits):
        raise RuntimeError(f"failed to remove property spec {path}")


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


def _copy_field(target_spec, source_spec, field: str) -> None:
    if field == "timeSamples" and isinstance(target_spec, Sdf.AttributeSpec):
        for time in tuple(target_spec.ListTimeSamples()):
            target_spec.EraseTimeSample(time)
        if source_spec.HasInfo(field):
            for time in source_spec.ListTimeSamples():
                target_spec.SetTimeSample(time, source_spec.QueryTimeSample(time))
        return
    if field == "connectionPaths" and isinstance(target_spec, Sdf.AttributeSpec):
        if source_spec.HasInfo(field):
            _copy_path_list(target_spec.connectionPathList, source_spec.GetInfo(field))
        else:
            target_spec.connectionPathList.ClearEdits()
        return
    if field == "targetPaths" and isinstance(target_spec, Sdf.RelationshipSpec):
        if source_spec.HasInfo(field):
            _copy_path_list(target_spec.targetPathList, source_spec.GetInfo(field))
        else:
            target_spec.targetPathList.ClearEdits()
        return
    if source_spec.HasInfo(field):
        target_spec.SetInfo(field, source_spec.GetInfo(field))
    else:
        _clear_field(target_spec, field)


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


def _composed_declaration_value(prop: Usd.Property, field: str):
    if field == "custom":
        return prop.IsCustom()
    if isinstance(prop, Usd.Attribute):
        if field == "typeName":
            return prop.GetTypeName()
        if field == "variability":
            return prop.GetVariability()
    return prop.GetMetadata(field)


def _retain_fields(spec, fields) -> None:
    keep = _DECLARATION_FIELDS | set(fields)
    for key in tuple(spec.ListInfoKeys()):
        if str(key) not in keep:
            _clear_field(spec, str(key))


def serialize_property_spec_fields(
    source_layer: Sdf.Layer,
    source_path: str | Sdf.Path,
    event_path: str | Sdf.Path,
    fields: set[str] | frozenset[str] | list[str] | tuple[str, ...],
) -> str:
    """Serialize selected property fields and declaration data to USDA."""
    source_path = _property_path(source_path)
    event_path = _property_path(event_path)
    source_spec = source_layer.GetPropertyAtPath(source_path)
    if not source_spec:
        raise ValueError(f"source layer has no property spec at {source_path}")

    scratch = _scratch_layer("serialize")
    try:
        Sdf.CreatePrimInLayer(scratch, event_path.GetPrimPath())
        if not Sdf.CopySpec(source_layer, source_path, scratch, event_path):
            raise RuntimeError(f"failed to copy property spec {source_path}")

        spec = scratch.GetPropertyAtPath(event_path)
        _retain_fields(spec, fields)
        return scratch.ExportToString()
    finally:
        scratch.Clear()


def serialize_composed_property_spec_fields(
    stage: Usd.Stage,
    event_path: str | Sdf.Path,
    fields: set[str] | frozenset[str] | list[str] | tuple[str, ...],
) -> str | None:
    """Serialize the composed values of selected fields on one property.

    Department-layer receivers currently maintain a flat projection of the
    server stage.  When an authored opinion loses strength or is cleared, that
    projection needs the resulting composed fields rather than the losing local
    fragment. USD property APIs perform the required declaration, dictionary,
    and path-list field composition.

    Returns ``None`` when no authored property spec contributes at
    ``event_path``; callers represent that state with a removed-property event.
    """
    event_path = _property_path(event_path)
    prop = stage.GetPropertyAtPath(event_path)
    if not prop or not prop.IsValid():
        return None
    stack = prop.GetPropertyStack()
    if not stack:
        return None

    selected = set(fields)
    scratch = _scratch_layer("compose")
    try:
        source_spec = stack[0]
        Sdf.CreatePrimInLayer(scratch, event_path.GetPrimPath())
        if not Sdf.CopySpec(
            source_spec.layer,
            source_spec.path,
            scratch,
            event_path,
        ):
            raise RuntimeError(f"failed to copy composed property spec {event_path}")

        spec = scratch.GetPropertyAtPath(event_path)
        _retain_fields(spec, selected)
        declaration_fields = _DECLARATION_FIELDS & {str(key) for key in spec.ListInfoKeys()}
        for field in declaration_fields:
            value = _composed_declaration_value(prop, field)
            if spec.GetInfo(field) != value:
                _set_composed_field(spec, field, value)
        for field in selected - declaration_fields:
            if prop.HasAuthoredMetadata(field):
                _set_composed_field(spec, field, prop.GetMetadata(field))
            else:
                _clear_field(spec, field)
        return scratch.ExportToString()
    finally:
        scratch.Clear()


def composed_property_spec_event(stage: Usd.Stage, event: dict) -> dict:
    """Build a flat-projection event for the property's composed state."""
    path = _property_path(event["spec_path"])
    fields = set(str(field) for field in event.get("fields", ()))
    prop = stage.GetPropertyAtPath(path)
    if prop and prop.IsValid() and event.get("removed", False):
        for spec in prop.GetPropertyStack():
            fields.update(str(key) for key in spec.ListInfoKeys())

    fragment = serialize_composed_property_spec_fields(stage, path, fields)
    return {
        "k": event["k"],
        "prim": event["prim"],
        "spec_path": str(path),
        "fields": sorted(fields),
        "fragment": fragment or "",
        "removed": fragment is None,
    }


def fragment_authored_fields(fragment: str, spec_path: str | Sdf.Path) -> set[str]:
    """Return fields authored on the fragment's property spec."""
    path = _property_path(spec_path)
    incoming = _scratch_layer("inspect")
    try:
        if not incoming.ImportFromString(fragment):
            raise ValueError("invalid Sdf property fragment")
        spec = incoming.GetPropertyAtPath(path)
        if not spec:
            raise ValueError(f"fragment has no property spec at {path}")
        return {str(key) for key in spec.ListInfoKeys()}
    finally:
        incoming.Clear()


def apply_property_spec_delta(stage: Usd.Stage, event: dict) -> None:
    """Apply one ``set_sdf_property_fields`` event to the stage's edit target."""
    event_path = _property_path(event["spec_path"])
    edit_target = stage.GetEditTarget()
    target_layer = edit_target.GetLayer()
    target_path = edit_target.MapToSpecPath(event_path)
    if target_path.isEmpty:
        raise ValueError(f"edit target cannot map property path {event_path}")

    if event.get("removed", False):
        _remove_property(target_layer, target_path)
        return

    fields = tuple(dict.fromkeys(str(field) for field in event.get("fields", ())))
    if not fields:
        return
    fragment = event.get("fragment", "")
    incoming = _scratch_layer("apply")
    try:
        if not fragment or not incoming.ImportFromString(fragment):
            raise ValueError("set_sdf_property_fields requires a valid Sdf property fragment")
        source_spec = incoming.GetPropertyAtPath(event_path)
        if not source_spec:
            raise ValueError(f"fragment has no property spec at {event_path}")

        target_spec = target_layer.GetPropertyAtPath(target_path)
        if target_spec and type(target_spec) is not type(source_spec):
            _remove_property(target_layer, target_path)
            target_spec = None
        if not target_spec:
            _retain_fields(source_spec, fields)
            Sdf.CreatePrimInLayer(target_layer, target_path.GetPrimPath())
            if not Sdf.CopySpec(incoming, event_path, target_layer, target_path):
                raise RuntimeError(f"failed to create property spec {target_path}")
            return

        for field in fields:
            _copy_field(target_spec, source_spec, field)
    finally:
        incoming.Clear()


def merge_property_spec_events(previous: dict, current: dict) -> dict:
    """Merge two deltas for one property into one replay-complete event."""
    if previous["spec_path"] != current["spec_path"]:
        raise ValueError("cannot merge deltas for different property specs")
    if current.get("removed", False) or previous.get("removed", False):
        return dict(current)

    path = _property_path(current["spec_path"])
    old_layer = _scratch_layer("merge-old")
    new_layer = _scratch_layer("merge-new")
    output = _scratch_layer("merge-output")
    try:
        if not old_layer.ImportFromString(previous.get("fragment", "")):
            raise ValueError("invalid previous Sdf property fragment")
        if not new_layer.ImportFromString(current.get("fragment", "")):
            raise ValueError("invalid current Sdf property fragment")
        old_spec = old_layer.GetPropertyAtPath(path)
        new_spec = new_layer.GetPropertyAtPath(path)
        if not old_spec or not new_spec:
            raise ValueError(f"Sdf fragment has no property spec at {path}")
        if type(old_spec) is not type(new_spec):
            return dict(current)

        Sdf.CreatePrimInLayer(output, path.GetPrimPath())
        if not Sdf.CopySpec(old_layer, path, output, path):
            raise RuntimeError(f"failed to copy property spec {path}")
        output_spec = output.GetPropertyAtPath(path)
        for field in current.get("fields", ()):
            _copy_field(output_spec, new_spec, field)

        merged = dict(current)
        merged["fields"] = list(
            dict.fromkeys(
                [
                    *previous.get("fields", ()),
                    *current.get("fields", ()),
                ]
            )
        )
        merged["fragment"] = output.ExportToString()
        merged["removed"] = False
        return merged
    finally:
        old_layer.Clear()
        new_layer.Clear()
        output.Clear()


__all__ = [
    "apply_property_spec_delta",
    "composed_property_spec_event",
    "fragment_authored_fields",
    "merge_property_spec_events",
    "serialize_composed_property_spec_fields",
    "serialize_property_spec_fields",
]
