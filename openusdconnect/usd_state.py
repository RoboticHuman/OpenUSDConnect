"""Read composed USD state shared by emitters and native projections."""

from __future__ import annotations

from collections.abc import Callable

from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdShade

from .connectable_attrs import ConnectableAttr, input_attr, output_attr
from .protocol_constants import PRIMVAR_PREFIX, REL_MATERIAL_BINDING


def values_equal(a, b) -> bool:
    """Compare attribute values, including NumPy and Vt array values."""
    import numpy as np

    if isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
        try:
            return np.array_equal(a, b)
        except (TypeError, ValueError):
            return False
    return a == b


def usd_value_to_python(
    value,
    asset_path_transform: Callable[[str], str] | None = None,
):
    """Convert a USD value to a type supported by the wire codec."""
    import numpy as np

    if value is None:
        return None
    if isinstance(value, (int, float, bool, str)):
        return value
    if isinstance(value, Sdf.AssetPath):
        if asset_path_transform is not None:
            identifier = value.evaluatedPath or value.authoredPath or value.path
            return asset_path_transform(identifier)
        return value.resolvedPath or value.path
    for value_type in (Gf.Vec2d, Gf.Vec2f, Gf.Vec3d, Gf.Vec3f, Gf.Vec4d, Gf.Vec4f):
        if isinstance(value, value_type):
            return [float(component) for component in value]
    for value_type in (Gf.Quatf, Gf.Quatd, Gf.Quath):
        if isinstance(value, value_type):
            imaginary = value.GetImaginary()
            return [
                float(value.GetReal()),
                float(imaginary[0]),
                float(imaginary[1]),
                float(imaginary[2]),
            ]
    for value_type in (
        Gf.Matrix2d,
        Gf.Matrix2f,
        Gf.Matrix3d,
        Gf.Matrix3f,
        Gf.Matrix4d,
        Gf.Matrix4f,
    ):
        if isinstance(value, value_type):
            return [float(component) for row in value for component in row]

    type_name = type(value).__name__
    if isinstance(value, Sdf.AssetPathArray):
        return [
            usd_value_to_python(element, asset_path_transform=asset_path_transform)
            for element in value
        ]
    if type_name.endswith("Array"):
        try:
            return np.array(value)
        except (TypeError, ValueError):
            result = []
            for element in value:
                converted = usd_value_to_python(
                    element,
                    asset_path_transform=asset_path_transform,
                )
                if converted is None:
                    return None
                result.append(converted)
            return result
    if type_name == "Half":
        return float(value)
    for convert in (float, int):
        try:
            return convert(value)
        except (TypeError, ValueError):
            continue
    return None


def read_variant_selections(stage: Usd.Stage, prim_path: str) -> dict[str, str]:
    """Return the composed selections on a prim, keyed by variant-set name."""
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return {}
    variant_sets = prim.GetVariantSets()
    return {
        name: selection
        for name in variant_sets.GetNames()
        if (selection := variant_sets.GetVariantSelection(name))
    }


_MATERIAL_BINDING_PURPOSE_RELS = (
    ("", REL_MATERIAL_BINDING),
    ("preview", REL_MATERIAL_BINDING + ":preview"),
    ("full", REL_MATERIAL_BINDING + ":full"),
)


def read_material_binding(stage: Usd.Stage, prim_path: str) -> dict[str, str]:
    """Return resolved direct binding targets by material purpose.

    This reads the composed binding relationships on the prim. It does not
    compute an inherited bound material from an ancestor or collection.
    """
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid() or prim.IsPseudoRoot():
        return {}
    result: dict[str, str] = {}
    for purpose, relationship_name in _MATERIAL_BINDING_PURPOSE_RELS:
        relationship = prim.GetRelationship(relationship_name)
        if not relationship or not relationship.IsValid() or not relationship.IsAuthored():
            continue
        targets = relationship.GetTargets()
        result[purpose] = str(targets[0]) if targets else ""
    return result


def attribute_event_metadata(prim, attr_name: str, attr) -> tuple[dict, dict]:
    """Return the wire primvar and interpolation metadata for an attribute."""
    primvar_meta: dict = {}
    attr_interp: dict = {}
    if attr_name.startswith(PRIMVAR_PREFIX):
        primvar = UsdGeom.PrimvarsAPI(prim).GetPrimvar(attr_name[len(PRIMVAR_PREFIX) :])
        if primvar:
            metadata: dict = {"typeName": str(attr.GetTypeName())}
            if primvar.HasAuthoredInterpolation():
                metadata["interpolation"] = str(primvar.GetInterpolation())
            primvar_meta[attr_name] = metadata
    else:
        interpolation = attr.GetMetadata("interpolation")
        if interpolation:
            attr_interp[attr_name] = str(interpolation)
    return primvar_meta, attr_interp


def connectable_kind(prim) -> str:
    """Return the protocol container kind for a supported connectable prim."""
    if not prim or not prim.IsValid():
        return ""
    if prim.IsA(UsdShade.Shader):
        return "shader"
    if prim.IsA(UsdShade.NodeGraph):
        return "nodegraph"
    if prim.HasAPI(UsdLux.LightAPI):
        return "light"
    return ""


def connected_source_attr(source) -> ConnectableAttr:
    """Return the qualified protocol attribute for a USD connection source."""
    if source.sourceType == UsdShade.AttributeType.Output:
        return output_attr(source.sourceName)
    return input_attr(source.sourceName)


def read_usdshade_connectable(stage: Usd.Stage, prim_path: str):
    """Read composed inputs and connections from a supported connectable prim.

    Returns ``(container_kind, info_id, inputs, input_types, connections)``.
    Connections use namespace-qualified local and source attribute names.
    """
    prim = stage.GetPrimAtPath(prim_path)
    container_kind = connectable_kind(prim)
    if not container_kind:
        return "", "", {}, {}, {}

    if container_kind == "shader":
        shader = UsdShade.Shader(prim)
        info_id = shader.GetIdAttr().Get() or ""
        if not info_id:
            return "", "", {}, {}, {}
        connectable = shader
    else:
        info_id = ""
        connectable = UsdShade.ConnectableAPI(prim)

    inputs = {}
    input_types = {}
    connections = {}
    for input_port in connectable.GetInputs():
        if not input_port.GetAttr().IsAuthored():
            continue
        name = input_port.GetBaseName()
        sources, _invalid = input_port.GetConnectedSources()
        if sources:
            connections[input_attr(name).qualified_name] = {
                "source_prim": str(sources[0].source.GetPath()),
                "source_attr": connected_source_attr(sources[0]).qualified_name,
            }
            continue
        value = usd_value_to_python(input_port.Get())
        if value is not None:
            inputs[name] = value
            input_types[name] = str(input_port.GetAttr().GetTypeName())

    for output_port in connectable.GetOutputs():
        if not output_port.GetAttr().HasAuthoredConnections():
            continue
        sources, _invalid = output_port.GetConnectedSources()
        if not sources:
            continue
        connections[output_attr(output_port.GetBaseName()).qualified_name] = {
            "source_prim": str(sources[0].source.GetPath()),
            "source_attr": connected_source_attr(sources[0]).qualified_name,
        }
    return container_kind, info_id, inputs, input_types, connections


# UsdGeomPointInstancer property name to protocol field name. The float
# orientation property wins when both orientation representations exist.
POINT_INSTANCER_USD_TO_WIRE = {
    "protoIndices": "proto_indices",
    "positions": "positions",
    "orientations": "orientations",
    "orientationsf": "orientations",
    "scales": "scales",
    "velocities": "velocities",
    "accelerations": "accelerations",
    "angularVelocities": "angular_velocities",
    "ids": "ids",
    "invisibleIds": "invisible_ids",
}
POINT_INSTANCER_QUAT_ATTRS = frozenset({"orientations", "orientationsf"})


def point_instancer_value_to_wire(field_name: str, value):
    """Convert a composed PointInstancer value into its protocol shape."""
    if field_name == "orientations":
        import numpy as np

        return np.asarray(value)[:, [3, 0, 1, 2]].astype(np.float32, copy=False)
    if field_name in {"prototypes", "inactive_ids"}:
        return value
    return usd_value_to_python(value)


def read_point_instancer(
    stage: Usd.Stage,
    prim_path: str,
    only=None,
    time: float | None = None,
    *,
    transport: bool = True,
):
    """Read selected composed PointInstancer values.

    ``only`` contains USD property names and limits bulk array reads. ``time``
    selects default or sampled values. With ``transport=False``, Vt arrays
    remain copy-on-write values suitable for comparison.
    """
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid() or not prim.IsA(UsdGeom.PointInstancer):
        return None
    point_instancer = UsdGeom.PointInstancer(prim)
    state: dict = {}
    if only is None or "prototypes" in only:
        targets = point_instancer.GetPrototypesRel().GetTargets()
        if targets:
            state["prototypes"] = [str(target) for target in targets]
    if (only is None or "inactiveIds" in only) and prim.HasAuthoredMetadata("inactiveIds"):
        list_op = prim.GetMetadata("inactiveIds")
        state["inactive_ids"] = [int(value) for value in list_op.ApplyOperations([])]

    float_orientations = point_instancer.GetOrientationsfAttr()
    orientation_name = (
        "orientationsf"
        if float_orientations and float_orientations.IsAuthored()
        else "orientations"
    )
    if only is not None and only & POINT_INSTANCER_QUAT_ATTRS:
        only = set(only) | {orientation_name}
    time_code = Usd.TimeCode.Default() if time is None else Usd.TimeCode(time)
    for usd_name, wire_name in POINT_INSTANCER_USD_TO_WIRE.items():
        if usd_name in POINT_INSTANCER_QUAT_ATTRS and usd_name != orientation_name:
            continue
        if only is not None and usd_name not in only:
            continue
        attr = prim.GetAttribute(usd_name)
        if not attr or not attr.IsValid() or not attr.IsAuthored():
            continue
        value = attr.Get(time_code)
        if value is None:
            continue
        converted = point_instancer_value_to_wire(wire_name, value) if transport else value
        if converted is not None:
            state[wire_name] = converted
    return state


__all__ = [
    "POINT_INSTANCER_QUAT_ATTRS",
    "POINT_INSTANCER_USD_TO_WIRE",
    "attribute_event_metadata",
    "connectable_kind",
    "connected_source_attr",
    "point_instancer_value_to_wire",
    "read_material_binding",
    "read_point_instancer",
    "read_usdshade_connectable",
    "read_variant_selections",
    "usd_value_to_python",
    "values_equal",
]
