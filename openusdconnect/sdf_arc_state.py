"""Exact Sdf list-op state for reference and payload events."""

from __future__ import annotations

from pxr import Sdf

from .protocol_constants import ARC_LIST_POSITIONS

_LIST_OP_FIELDS = (
    ("addedItems", "added"),
    ("prependedItems", "prepended"),
    ("appendedItems", "appended"),
    ("deletedItems", "deleted"),
    ("orderedItems", "ordered"),
)


def serialize_reference_custom_data(custom_data: dict) -> str:
    """Serialize an Sdf reference customData dictionary without type loss."""
    if not custom_data:
        return ""
    layer = Sdf.Layer.CreateAnonymous("openusdconnect-reference-custom-data")
    layer.customLayerData = custom_data
    return layer.ExportToString()


def deserialize_reference_custom_data(fragment: str) -> dict:
    """Deserialize a typed reference customData dictionary."""
    if not fragment:
        return {}
    layer = Sdf.Layer.CreateAnonymous("openusdconnect-reference-custom-data")
    if not layer.ImportFromString(fragment):
        raise ValueError("invalid reference custom-data fragment")
    return dict(layer.customLayerData)


def _arc_field(arc_attr: str) -> str:
    if arc_attr == "referenceList":
        return "references"
    if arc_attr == "payloadList":
        return "payload"
    raise ValueError(f"unsupported composition arc field {arc_attr!r}")


def _canonical_entry(
    entry: dict,
    *,
    explicit: bool,
    references: bool,
) -> dict:
    position = str(entry.get("list_position", "explicit" if explicit else "prepended"))
    if position not in ARC_LIST_POSITIONS:
        raise ValueError(f"invalid arc list position {position!r}")
    if explicit and position != "explicit":
        raise ValueError("explicit list ops cannot contain non-explicit entries")
    if not explicit and position == "explicit":
        raise ValueError("non-explicit list ops cannot contain explicit entries")

    result: dict = {}
    asset_path = str(entry.get("asset_path", ""))
    prim_path = str(entry.get("prim_path", ""))
    if asset_path:
        result["asset_path"] = asset_path.replace("\\", "/")
    if prim_path:
        result["prim_path"] = prim_path
    if not explicit and position != "prepended":
        result["list_position"] = position

    offset = float(entry.get("layer_offset", 0.0))
    scale = float(entry.get("layer_scale", 1.0))
    if offset != 0.0:
        result["layer_offset"] = offset
    if scale != 1.0:
        result["layer_scale"] = scale

    custom_data_fragment = str(entry.get("custom_data_fragment", ""))
    if custom_data_fragment:
        if not references:
            raise ValueError("payload entries cannot carry reference custom data")
        normalized_fragment = serialize_reference_custom_data(
            deserialize_reference_custom_data(custom_data_fragment),
        )
        if normalized_fragment:
            result["custom_data_fragment"] = normalized_fragment
    return result


def canonical_arc_state(
    entries: list[dict],
    *,
    authored: bool | None = None,
    explicit: bool = False,
    references: bool,
) -> dict:
    """Return one normalized, comparison-safe arc event state."""
    if authored is None:
        authored = bool(entries) or explicit
    authored = bool(authored)
    explicit = bool(explicit)
    if not authored and (entries or explicit):
        raise ValueError("an unauthored list op cannot contain entries or be explicit")
    normalized = [
        _canonical_entry(
            entry,
            explicit=explicit,
            references=references,
        )
        for entry in entries
    ]
    if not explicit:
        normalized = [
            entry
            for _item_field, position in _LIST_OP_FIELDS
            for entry in normalized
            if entry.get("list_position", "prepended") == position
        ]
    return {
        "entries": normalized,
        "list_op_authored": authored,
        "list_op_explicit": explicit,
    }


def read_arc_state(
    layer: Sdf.Layer,
    spec_path: str | Sdf.Path,
    arc_attr: str,
    *,
    absolute_asset_paths: bool = False,
) -> dict:
    """Read an exact reference or payload list op from one PrimSpec."""
    path = spec_path if isinstance(spec_path, Sdf.Path) else Sdf.Path(spec_path)
    spec = layer.GetPrimAtPath(path)
    field = _arc_field(arc_attr)
    references = arc_attr == "referenceList"
    if spec is None or not spec.HasInfo(field):
        return {
            "entries": [],
            "list_op_authored": False,
            "list_op_explicit": False,
        }

    list_op = getattr(spec, arc_attr)
    entries: list[dict] = []
    buckets = (("explicitItems", "explicit"),) if list_op.isExplicit else _LIST_OP_FIELDS
    for item_field, position in buckets:
        for item in getattr(list_op, item_field):
            entry: dict = {}
            if item.assetPath:
                asset_path = (
                    layer.ComputeAbsolutePath(item.assetPath)
                    if absolute_asset_paths
                    else item.assetPath
                )
                entry["asset_path"] = asset_path.replace("\\", "/")
            if not item.primPath.isEmpty:
                entry["prim_path"] = str(item.primPath)
            if not list_op.isExplicit and position != "prepended":
                entry["list_position"] = position
            if item.layerOffset.offset != 0.0:
                entry["layer_offset"] = item.layerOffset.offset
            if item.layerOffset.scale != 1.0:
                entry["layer_scale"] = item.layerOffset.scale
            if references and item.customData:
                entry["custom_data_fragment"] = serialize_reference_custom_data(
                    item.customData,
                )
            entries.append(entry)

    return {
        "entries": entries,
        "list_op_authored": True,
        "list_op_explicit": list_op.isExplicit,
    }


def _sdf_arc_item(entry: dict, *, references: bool):
    asset_path = str(entry.get("asset_path", ""))
    prim_path_text = str(entry.get("prim_path", ""))
    prim_path = Sdf.Path(prim_path_text) if prim_path_text else Sdf.Path.emptyPath
    layer_offset = Sdf.LayerOffset(
        float(entry.get("layer_offset", 0.0)),
        float(entry.get("layer_scale", 1.0)),
    )
    if references:
        return Sdf.Reference(
            asset_path,
            prim_path,
            layer_offset,
            deserialize_reference_custom_data(
                str(entry.get("custom_data_fragment", "")),
            ),
        )
    return Sdf.Payload(asset_path, prim_path, layer_offset)


def apply_arc_state(
    stage,
    prim_path: str,
    entries: list[dict],
    *,
    authored: bool | None = None,
    explicit: bool = False,
    arc_attr: str,
) -> None:
    """Replace one edit-target PrimSpec's authored reference/payload list op."""
    references = arc_attr == "referenceList"
    state = canonical_arc_state(
        entries,
        authored=authored,
        explicit=explicit,
        references=references,
    )
    edit_target = stage.GetEditTarget()
    layer = edit_target.GetLayer()
    target_path = edit_target.MapToSpecPath(Sdf.Path(prim_path))
    if target_path.isEmpty:
        raise ValueError(f"edit target cannot map prim path {prim_path}")
    spec = layer.GetPrimAtPath(target_path)

    field = _arc_field(arc_attr)
    if not state["list_op_authored"]:
        if spec is not None:
            spec.ClearInfo(field)
        return
    if spec is None:
        spec = Sdf.CreatePrimInLayer(layer, target_path)

    list_op = Sdf.ReferenceListOp() if references else Sdf.PayloadListOp()
    items = [_sdf_arc_item(entry, references=references) for entry in state["entries"]]
    if state["list_op_explicit"]:
        list_op.explicitItems = items
    else:
        grouped = {position: [] for _field, position in _LIST_OP_FIELDS}
        for entry, item in zip(state["entries"], items, strict=True):
            grouped[entry.get("list_position", "prepended")].append(item)
        for item_field, position in _LIST_OP_FIELDS:
            setattr(list_op, item_field, grouped[position])
    spec.SetInfo(field, list_op)


def clear_arc_state(stage, prim_path: str, *, arc_attr: str) -> None:
    """Clear one edit-target list op without creating a PrimSpec."""
    edit_target = stage.GetEditTarget()
    layer = edit_target.GetLayer()
    target_path = edit_target.MapToSpecPath(Sdf.Path(prim_path))
    if target_path.isEmpty:
        return
    spec = layer.GetPrimAtPath(target_path)
    if spec is not None:
        spec.ClearInfo(_arc_field(arc_attr))


__all__ = [
    "apply_arc_state",
    "canonical_arc_state",
    "clear_arc_state",
    "deserialize_reference_custom_data",
    "read_arc_state",
    "serialize_reference_custom_data",
]
