"""Semantic validation for MCP-authored events, layered on protocol_validation.

Two stages run before anything is sent: (1) dict-shape via the core
``protocol_validation.validate_event``; (2) pxr-backed semantic checks (path
validity, schema typeName/api_schemas, Sdf type names, connection-source
existence) that turn the core's silent skips into actionable errors. Missing
ancestors of created prims are auto-prepended as ``Xform`` ensure_prim events
when a mirror stage is available.
"""

from __future__ import annotations

from collections.abc import Callable

from pxr import Sdf, Usd

from openusdconnect.protocol_constants import (
    EVENT_KEYS,
    K_ENSURE_PRIM,
    K_SET_CONNECTABLE_CONNECTION,
    K_SET_CONNECTABLE_INPUT,
    K_SET_GPRIM_ATTRS,
    K_SET_MATERIAL_BINDING,
    K_SET_PAYLOAD,
    K_SET_REFERENCE,
    K_SET_STAGE_METADATA,
    PRIMVAR_PREFIX,
    STAGE_METADATA_KEYS,
)
from openusdconnect.protocol_validation import validate_event

from .errors import ToolError

_REG = Usd.SchemaRegistry


def _ancestor_paths(prim_path: str) -> list[str]:
    """Return ancestor prim paths of ``prim_path``, top-down (excludes self)."""
    parts = [p for p in prim_path.split("/") if p]
    return ["/" + "/".join(parts[:i]) for i in range(1, len(parts))]


def _require_prim_path(value, *, idx: int, kind: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or not Sdf.Path.IsValidPathString(value)
    ):
        raise ToolError(
            f"{kind}: {field}={value!r} is not a valid absolute prim path",
            code="invalid_path",
            event_index=idx,
            field=field,
        )


def _check_type_name(type_name: str, *, idx: int) -> None:
    if type_name == "":
        return  # untyped scope is allowed
    t = _REG.GetTypeFromSchemaTypeName(type_name)
    if not t:
        raise ToolError(
            f"ensure_prim: unknown typeName {type_name!r}",
            code="unknown_type",
            event_index=idx,
            field="typeName",
            hint="Use a concrete USD schema type (Xform, Mesh, Material, Shader, "
            "Scope, SphereLight, Camera, PointInstancer, ...).",
        )
    if not _REG.IsConcrete(t):
        raise ToolError(
            f"ensure_prim: typeName {type_name!r} is not a concrete prim type",
            code="abstract_type",
            event_index=idx,
            field="typeName",
        )


def _check_api_schemas(schemas: list, *, idx: int) -> None:
    for s in schemas:
        base, _, inst = s.partition(":")
        t = _REG.GetTypeFromSchemaTypeName(base)
        if not t or not _REG.IsAppliedAPISchema(t):
            raise ToolError(
                f"ensure_prim: {s!r} is not an applied API schema",
                code="unknown_api_schema",
                event_index=idx,
                field="api_schemas",
            )
        multi = _REG.IsMultipleApplyAPISchema(t)
        if multi and not inst:
            raise ToolError(
                f"ensure_prim: {base!r} is multiple-apply, use '{base}:<instance>'",
                code="api_schema_instance",
                event_index=idx,
                field="api_schemas",
            )
        if inst and not multi:
            raise ToolError(
                f"ensure_prim: {base!r} is single-apply and takes no ':{inst}' instance",
                code="api_schema_instance",
                event_index=idx,
                field="api_schemas",
            )


def _check_sdf_type(type_name, *, idx: int, field: str) -> None:
    if not isinstance(type_name, str) or not Sdf.ValueTypeNames.Find(type_name):
        raise ToolError(
            f"unknown Sdf value type {type_name!r}",
            code="unknown_sdf_type",
            event_index=idx,
            field=field,
            hint="Use lowercase Sdf type names: scalars like 'float', 'color3f', "
            "'float2', 'normal3f', 'asset', 'token'; arrays like 'point3f[]', "
            "'texCoord2f[]', 'normal3f[]', 'int[]'.",
        )


def _validate_stage_metadata(ev: dict, idx: int) -> None:
    extra = set(ev) - {"k", *STAGE_METADATA_KEYS}
    if extra:
        raise ToolError(
            f"set_stage_metadata: unexpected fields {sorted(extra)}",
            code="invalid_request",
            event_index=idx,
        )
    present = [key for key in STAGE_METADATA_KEYS if key in ev]
    if not present:
        raise ToolError(
            "set_stage_metadata: provide at least one metadata field",
            code="invalid_request",
            event_index=idx,
        )
    if "upAxis" in ev and ev["upAxis"] not in ("Y", "Z"):
        raise ToolError(
            f"set_stage_metadata: upAxis must be 'Y' or 'Z', got {ev['upAxis']!r}",
            code="invalid_request",
            event_index=idx,
            field="upAxis",
        )


def _shape_check(ev: dict, idx: int) -> None:
    """Run the core dict-shape validator with a clear error on failure."""
    if not validate_event(ev):
        raise ToolError(
            f"{ev.get('k')}: malformed event (missing/invalid required fields)",
            code="invalid_event",
            event_index=idx,
        )


def validate_and_prepare(
    events: list,
    *,
    stage: Usd.Stage | None = None,
    auto_create_ancestors: bool = True,
    node_exists: Callable[[str], bool] | None = None,
) -> tuple[list[dict], list[str]]:
    """Validate ``events`` and return ``(prepared_events, warnings)``.

    Raises :class:`ToolError` on the first invalid event; nothing should be sent
    when this raises. ``prepared_events`` may include auto-prepended ancestor
    ``ensure_prim`` events. ``stage`` (the mirror) enables existence checks;
    ``node_exists`` enables an info_id warning.
    """
    if not isinstance(events, list) or not events:
        raise ToolError("events must be a non-empty list", field="events")

    batch_created: set[str] = set()
    for ev in events:
        if (
            isinstance(ev, dict)
            and ev.get("k") == K_ENSURE_PRIM
            and isinstance(ev.get("prim"), str)
        ):
            batch_created.add(ev["prim"])

    prepared: list[dict] = []
    warnings: list[str] = []
    prepended: set[str] = set()

    def _exists(path: str) -> bool:
        return (
            path in batch_created
            or path in prepended
            or (stage is not None and stage.GetPrimAtPath(path).IsValid())
        )

    for idx, ev in enumerate(events):
        if not isinstance(ev, dict):
            raise ToolError(f"event #{idx} is not an object", event_index=idx)
        kind = ev.get("k")
        if kind not in EVENT_KEYS:
            raise ToolError(
                f"unknown event kind {kind!r}", code="unknown_kind", event_index=idx, field="k"
            )

        if kind == K_SET_STAGE_METADATA:
            _validate_stage_metadata(ev, idx)
            prepared.append(ev)
            continue

        _require_prim_path(ev.get("prim"), idx=idx, kind=kind, field="prim")
        _shape_check(ev, idx)

        if kind == K_ENSURE_PRIM:
            _check_type_name(ev["typeName"], idx=idx)
            _check_api_schemas(ev.get("api_schemas", []), idx=idx)
            if auto_create_ancestors and stage is not None:
                for anc in _ancestor_paths(ev["prim"]):
                    if not _exists(anc):
                        prepared.append({"k": K_ENSURE_PRIM, "prim": anc, "typeName": "Xform"})
                        prepended.add(anc)

        if kind == K_SET_CONNECTABLE_INPUT:
            for name, tn in ev.get("input_types", {}).items():
                _check_sdf_type(tn, idx=idx, field=f"input_types[{name}]")
            info_id = ev.get("info_id", "")
            if info_id and node_exists is not None and not node_exists(info_id):
                warnings.append(
                    f"event #{idx} set_connectable_input: info_id {info_id!r} is not a "
                    "known shader node, call usd_describe_shader_node to verify."
                )

        if kind == K_SET_GPRIM_ATTRS:
            for name, meta in ev.get("primvar_meta", {}).items():
                if not name.startswith(PRIMVAR_PREFIX):
                    raise ToolError(
                        f"set_gprim_attrs: primvar_meta key {name!r} must start with 'primvars:'",
                        event_index=idx,
                        field="primvar_meta",
                    )
                _check_sdf_type(
                    meta.get("typeName"), idx=idx, field=f"primvar_meta[{name}].typeName"
                )

        if kind == K_SET_MATERIAL_BINDING:
            _require_prim_path(ev["material_path"], idx=idx, kind=kind, field="material_path")

        if kind in (K_SET_REFERENCE, K_SET_PAYLOAD):
            arcs = ev.get("refs" if kind == K_SET_REFERENCE else "payloads", [])
            for arc in arcs:
                pp = arc.get("prim_path")
                if pp is not None and not Sdf.Path.IsValidPathString(pp):
                    raise ToolError(
                        f"{kind}: prim_path {pp!r} is not a valid path",
                        code="invalid_path",
                        event_index=idx,
                        field="prim_path",
                    )

        if kind == K_SET_CONNECTABLE_CONNECTION and stage is not None:
            for local_attr, conn in ev.get("connections", {}).items():
                src = conn.get("source_prim", "")
                if not _exists(src):
                    raise ToolError(
                        f"set_connectable_connection: source_prim {src!r} (for {local_attr}) "
                        "does not exist and is not created in this batch",
                        code="missing_source",
                        event_index=idx,
                        field="connections",
                        hint="Create the source shader with usd_ensure_prim first, or "
                        "include it in the same usd_send_events batch.",
                    )

        prepared.append(ev)

    return prepared, warnings
