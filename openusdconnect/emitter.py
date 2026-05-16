"""Stage change detection and event building.

NoticeEmitter watches a Usd.Stage via Usd.Notice.ObjectsChanged,
tracks dirty prims, snapshots TRS transforms, and builds partial-diff
events ready to send over the network.

DCC-agnostic — works on any Usd.Stage regardless of what's authoring to it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from pxr import Gf, Sdf, Tf, Usd, UsdGeom, UsdLux, UsdShade

from .connectable_attrs import (
    USDSHADE_INPUT_PREFIX,
    USDSHADE_OUTPUT_PREFIX,
    ConnectableAttr,
    input_attr,
    output_attr,
)
from .protocol_constants import (
    K_DEACTIVATE_PRIM,
    K_DELETE_PRIM,
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_LOAD_PAYLOAD,
    K_RENAME_PRIM,
    K_SET_CONNECTABLE_CONNECTION,
    K_SET_CONNECTABLE_INPUT,
    K_SET_GPRIM_ATTRS,
    K_SET_MATERIAL_BINDING,
    K_SET_PAYLOAD,
    K_SET_REFERENCE,
    K_SET_VARIANT_SELECTIONS,
    K_SET_VISIBILITY,
    K_SET_XFORM_MATRICES,
    K_SET_XFORM_TRS,
    K_UNLOAD_PAYLOAD,
    PRIMVAR_PREFIX,
    REL_MATERIAL_BINDING,
)

LOG = logging.getLogger(__name__)

# Per-prim cache keys — use these instead of raw strings to catch typos.
_C_TRS = "trs"
_C_MATS = "mats"
_C_VISIBILITY = "visibility"
_C_REFERENCES = "references"
_C_PAYLOADS = "payloads"
_C_PAYLOAD_LOADED = "payload_loaded"
_C_VARIANT_SELECTIONS = "variant_selections"
_C_GPRIM_ATTRS = "gprim_attrs"
_C_MATERIAL_BINDING = "material_binding"
_C_CONNECTABLE = "connectable"
_C_API_SCHEMAS = "api_schemas"
_C_CAMERA_ATTRS = "camera_attrs"

# Per-prim cache slots reserved by inline blocks in _build_dirty_prim_events
# (TRS uses a shared snap; gprim attrs use a dirty-driven scan; matrices is
# diagnostic). Extra channels must not collide with these — see the cache-
# key uniqueness check in NoticeEmitter.__init__.
_INLINE_CACHE_KEYS = frozenset({_C_TRS, _C_MATS, _C_GPRIM_ATTRS, _C_API_SCHEMAS})

# Attribute names and prefixes the inline blocks own — TRS uses the
# pre-computed snap; ``extent`` is computed by USD; ``proxyPrim`` is a
# rendering hint relationship. No PrimChannel watches these, so the
# default gprim attr filter starts with them and unions in every
# channel's watched_attrs/watched_prefixes at NoticeEmitter init time.
_INLINE_BLOCK_SKIP_ATTRS = frozenset({"xformOpOrder", "extent", "proxyPrim"})
_INLINE_BLOCK_SKIP_PREFIXES = ("xformOp:",)

# Snapshot of names/prefixes the built-in channels watch — used by the
# module-level ``_schema_attrs`` helper, which runs at import time (before
# any NoticeEmitter exists) to derive typed-schema attr name sets. Keep
# in sync if a new built-in channel watches a new attr/prefix that should
# be excluded from typed-schema aggregations.
_BUILTIN_CHANNEL_WATCHED_ATTRS = frozenset({"visibility", "info:id"})
_BUILTIN_CHANNEL_WATCHED_PREFIXES = (USDSHADE_INPUT_PREFIX, USDSHADE_OUTPUT_PREFIX)


def _schema_attrs(schema_cls) -> frozenset[str]:
    """Return attribute names defined directly by a typed USD schema,
    minus names already handled by inline blocks or built-in channels."""
    skip_attrs = _INLINE_BLOCK_SKIP_ATTRS | _BUILTIN_CHANNEL_WATCHED_ATTRS
    skip_prefixes = _INLINE_BLOCK_SKIP_PREFIXES + _BUILTIN_CHANNEL_WATCHED_PREFIXES
    return frozenset(
        n for n in schema_cls.GetSchemaAttributeNames(False)
        if n not in skip_attrs
        and not any(n.startswith(p) for p in skip_prefixes)
    )


# UsdGeomCamera typed-schema attribute names. Derived from USD's schema
# registry so future schema additions (new exposure sub-attrs etc.) are
# picked up automatically without a code change here.
_CAMERA_ATTR_NAMES = _schema_attrs(UsdGeom.Camera)


# ---------------------------------------------------------------------------
# Replicated API schemas — emit-side filter
# ---------------------------------------------------------------------------
#
# Decides which applied API schemas show up in the api_schemas field of
# ensure_prim events. Default ships with UsdLux schemas that any DCC needs
# in the viewport. DCC integrations register their own at import time;
# tests pass an explicit set via the NoticeEmitter constructor.

DEFAULT_REPLICATED_API_SCHEMAS = frozenset({
    "ShapingAPI", "ShadowAPI",          # UsdLux user-applied
    "MeshLightAPI", "VolumeLightAPI",   # UsdLux user-applied (light on Mesh/Volume)
    # NOTE: LightAPI is built-in for typed UsdLux lights — replicating it
    # would add a redundant authored opinion. Excluded by design.
    # NOTE: MaterialBindingAPI is handled via K_SET_MATERIAL_BINDING.
    # NOTE: MotionAPI (motion-blur sampling) is render-time, not viewport —
    # users who need it call register_replicated_api_schema("MotionAPI").
})

_REPLICATED_API_SCHEMAS: set[str] = set(DEFAULT_REPLICATED_API_SCHEMAS)


def _validate_replicated_schema_name(name: str) -> None:
    """Hard-reject ':instance' wire form (whitelisting the bare name matches
    all instances automatically). Soft-reject unregistered names so
    plugin-loaded-later schemas still work; typos get a clear warning.
    """
    if ":" in name:
        raise ValueError(
            f"register_replicated_api_schema expects a bare schema name; "
            f"got {name!r}. Whitelist 'CollectionAPI' to replicate all instances."
        )
    if not Usd.SchemaRegistry.IsAppliedAPISchema(name):
        LOG.warning(
            "Schema %r is not currently a registered applied API schema. "
            "Adding to whitelist anyway (plugin may load later); if it never "
            "resolves, the whitelist entry has no effect.",
            name,
        )


def register_replicated_api_schema(name: str) -> None:
    """Add an API schema to the global replicate-list for new NoticeEmitters.

    DCC integrations call at import time to add their own schemas. Existing
    NoticeEmitters that snapshotted the global already are unaffected — only
    NoticeEmitters constructed AFTER the call pick up the addition.

    Validated against Usd.SchemaRegistry.IsAppliedAPISchema(name).
    """
    _validate_replicated_schema_name(name)
    _REPLICATED_API_SCHEMAS.add(name)


def unregister_replicated_api_schema(name: str) -> None:
    """Remove an API schema from the global replicate-list."""
    _REPLICATED_API_SCHEMAS.discard(name)


def _make_attr_filter(channels):
    """Build a gprim-attr filter from a channel set.

    The returned callable returns True for attrs the gprim scan should
    track. Attrs owned by inline blocks or by any of the channels' watched_*
    declarations are filtered out — so adding a new channel automatically
    excludes its attrs from gprim scan without further edits.
    """
    skip_attrs = set(_INLINE_BLOCK_SKIP_ATTRS)
    skip_prefixes = list(_INLINE_BLOCK_SKIP_PREFIXES)
    for ch in channels:
        skip_attrs.update(ch.watched_attrs)
        skip_prefixes.extend(ch.watched_prefixes)
    skip_attrs_fs = frozenset(skip_attrs)
    skip_prefixes_t = tuple(skip_prefixes)

    def _filter(attr_name: str) -> bool:
        if attr_name in skip_attrs_fs:
            return False
        return not attr_name.startswith(skip_prefixes_t)

    return _filter


def _values_equal(a, b) -> bool:
    """Compare two attribute values, handling numpy arrays."""
    import numpy as np

    if isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
        try:
            return np.array_equal(a, b)
        except (TypeError, ValueError):
            return False
    return a == b


def _usd_value_to_python(val):
    """Convert a USD attribute value to a codec-friendly Python type.

    Handles scalars, GfVec types, and VtArrays (including arrays of vectors).
    VtArrays are converted to numpy arrays (zero-copy when possible) so the
    codec can use CreateNumpyVector for bulk encoding.
    Returns None for unsupported types so the caller can skip them.
    """
    import numpy as np

    if val is None:
        return None
    # Simple scalars
    if isinstance(val, (int, float, bool, str)):
        return val
    # Sdf.AssetPath → prefer the resolved absolute path so the receiver
    # doesn't have to recover the source layer's anchor to make sense of
    # a bare relative string.  Falls back to the authored form when the
    # source layer's resolver couldn't resolve (e.g. in-memory stages).
    if isinstance(val, Sdf.AssetPath):
        return val.resolvedPath or val.path
    # GfVec types → list of floats (small, not worth numpy overhead)
    for vec_type in (Gf.Vec2d, Gf.Vec2f, Gf.Vec3d, Gf.Vec3f, Gf.Vec4d, Gf.Vec4f):
        if isinstance(val, vec_type):
            return [float(v) for v in val]
    # VtArray types (Vec3fArray, IntArray, FloatArray, etc.)
    # Detected by type name ending in "Array" — no shared base class in pxr.
    # Convert to numpy directly — pxr VtArrays support the buffer protocol.
    type_name = type(val).__name__
    if type_name.endswith("Array"):
        try:
            return np.array(val)
        except (TypeError, ValueError):
            # Fallback for exotic array types — iterate element-by-element
            result = []
            for elem in val:
                converted = _usd_value_to_python(elem)
                if converted is None:
                    return None
                result.append(converted)
            return result
    # Pxr value types that have a Python numeric equivalent
    if type_name in ("Half",):
        return float(val)
    # Numeric coercion only — never fall through to str() which would produce
    # unrecoverable representations like "Vt.Vec3fArray(...)"
    for coerce in (float, int):
        try:
            return coerce(val)
        except (TypeError, ValueError):
            continue
    return None


# PrimResyncType enum for classifying resync notices.
# Not available in all USD builds (e.g. Blender's bundled pxr).
try:
    _PrimResyncType = Usd.Notice.ObjectsChanged.PrimResyncType
except AttributeError:
    _PrimResyncType = None


def mat_to_16(m: Gf.Matrix4d) -> list[float]:
    """Convert a Gf.Matrix4d to a flat 16-element row-major list."""
    out = []
    for r in range(4):
        row = m.GetRow(r)
        out.extend([float(row[0]), float(row[1]), float(row[2]), float(row[3])])
    return out


def as_matrix(ret):
    """Handle USD binding variants: matrix or (matrix, resets...) tuple."""
    return ret[0] if isinstance(ret, tuple) else ret


def decompose_trs_from_matrix(m: Gf.Matrix4d):
    """Decompose a 4x4 matrix into translation, quaternion rotation, and scale.

    Returns:
        (t, r, s) where:
        - t = [x, y, z] translation
        - r = [w, x, y, z] quaternion rotation
        - s = [x, y, z] scale
    """
    tr = Gf.Transform()
    tr.SetMatrix(m)
    t = Gf.Vec3d(tr.GetTranslation())
    rot = tr.GetRotation()  # Gf.Rotation (axis-angle)
    s = Gf.Vec3d(tr.GetScale())

    # Convert rotation to quaternion
    qd = rot.GetQuat()
    w = float(qd.GetReal())
    iv = qd.GetImaginary()
    x, y, z = float(iv[0]), float(iv[1]), float(iv[2])

    return (
        [float(t[0]), float(t[1]), float(t[2])],
        [w, x, y, z],
        [float(s[0]), float(s[1]), float(s[2])],
    )


def near_list(a: list[float] | None, b: list[float] | None, eps: float) -> bool:
    """Check if two float lists are element-wise within epsilon."""
    if a is None or b is None or len(a) != len(b):
        return False
    return all(abs(float(x) - float(y)) <= eps for x, y in zip(a, b, strict=True))


def _prim_path_from_notice_path(path_str: str) -> str | None:
    """Convert a USD notice path to a prim path.

    Property paths like '/World/Sphere.xformOp:translate' become '/World/Sphere'.
    Prim paths pass through unchanged.
    """
    if not path_str.startswith("/"):
        return None
    if "." in path_str:
        return path_str.split(".", 1)[0]
    return path_str


def _read_composition_arcs(stage, prim_path, arc_attr):
    """Read composition arcs authored on this stage's own layers.

    Returns a list of (asset_path, prim_path_str) tuples, or empty list.
    Only considers the root and session layers — ignores arcs that come
    from composed-in layers (e.g. internal refs inside referenced assets).

    Args:
        arc_attr: Spec attribute name — "referenceList" or "payloadList".
    """
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return []
    own_layers = {stage.GetRootLayer().identifier, stage.GetSessionLayer().identifier}
    result = []
    for spec in prim.GetPrimStack():
        if spec.layer.identifier not in own_layers:
            continue
        arc_list = getattr(spec, arc_attr)
        for item in arc_list.prependedItems:
            result.append((item.assetPath, str(item.primPath)))
        for item in arc_list.explicitItems:
            result.append((item.assetPath, str(item.primPath)))
        for item in arc_list.appendedItems:
            result.append((item.assetPath, str(item.primPath)))
    return result


def read_references(stage, prim_path):
    """Read reference arcs authored on this stage's own layers."""
    return _read_composition_arcs(stage, prim_path, "referenceList")


def read_payloads(stage, prim_path):
    """Read payload arcs authored on this stage's own layers."""
    return _read_composition_arcs(stage, prim_path, "payloadList")


def read_variant_selections(stage, prim_path):
    """Read variant selections on a prim.

    Returns a dict mapping variant set name -> selected variant name,
    or empty dict if no variant sets or no selections.
    """
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return {}
    vsets = prim.GetVariantSets()
    result = {}
    for name in vsets.GetNames():
        sel = vsets.GetVariantSelection(name)
        if sel:
            result[name] = sel
    return result


def read_material_binding(stage, prim_path):
    """Read the material:binding relationship target from the composed stage.

    Returns the target material prim path string, or empty string if unbound.
    Reads from the full composed view so bindings from referenced files are
    visible — the emitter's per-prim cache handles deduplication.
    """
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return ""
    binding_rel = prim.GetRelationship(REL_MATERIAL_BINDING)
    if not binding_rel or not binding_rel.IsValid():
        return ""
    targets = binding_rel.GetTargets()
    return str(targets[0]) if targets else ""


def _connected_source_attr(src) -> ConnectableAttr:
    """Return the protocol attribute reference for a UsdShade connection source.

    Uses `sourceType` to choose `inputs:` vs `outputs:` so we cover the
    NodeGraph interface-forwarding case where the source is itself an
    input.
    """
    if src.sourceType == UsdShade.AttributeType.Output:
        return output_attr(src.sourceName)
    return input_attr(src.sourceName)


def read_usdshade_connectable(stage, prim_path):
    """Read interface data from a UsdShade.ConnectableAPI-bearing prim.

    Polymorphic over Shader, NodeGraph, Material (Material inherits
    NodeGraph in UsdShade), and UsdLux lights (LightAPI is a UsdShade
    connectable container).  Reads authored input values, their USD
    types, and any authored connections — on inputs AND outputs.

    Returns (container_kind, info_id, inputs, input_types, connections):
      - container_kind: "" if the prim doesn't bear an interface, otherwise
        "shader", "nodegraph" (covers Material), or "light".
      - info_id: info:id for Shader prims, "" for NodeGraph/Material/Light
        which carry no info:id by design.
      - inputs/input_types: keyed by the input's base name (no namespace
        prefix), since these are direct values, not connection edges.
      - connections: keyed by namespace-qualified local attribute name
        ("inputs:foo" or "outputs:bar") and valued by
        {"source_prim", "source_attr"} where source_attr is similarly
        qualified.  Mirrors USD's .connect authoring shape.  Inputs that
        have a connection are excluded from `inputs` since their value
        comes from the source, not direct authoring.

    Callers gate on `container_kind` to distinguish "no interface here"
    from "interface present but nothing authored yet".
    """
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return "", "", {}, {}, {}

    if prim.IsA(UsdShade.Shader):
        container_kind = "shader"
        shader = UsdShade.Shader(prim)
        info_id = shader.GetIdAttr().Get() or ""
        if not info_id:
            return "", "", {}, {}, {}
        connectable = shader
    elif prim.IsA(UsdShade.NodeGraph):
        # NodeGraph covers Material — Material inherits from NodeGraph.
        container_kind = "nodegraph"
        info_id = ""
        connectable = UsdShade.NodeGraph(prim)
    elif prim.HasAPI(UsdLux.LightAPI):
        # LightAPI is built-in on typed UsdLux lights (SphereLight, etc.) and
        # user-applied via MeshLightAPI/VolumeLightAPI on Mesh/Volume prims.
        container_kind = "light"
        info_id = ""
        connectable = UsdShade.ConnectableAPI(prim)
    else:
        return "", "", {}, {}, {}

    inputs = {}
    input_types = {}
    connections = {}

    for inp in connectable.GetInputs():
        if not inp.GetAttr().IsAuthored():
            continue
        name = inp.GetBaseName()
        sources, _ = inp.GetConnectedSources()
        if sources:
            connections[input_attr(name).qualified_name] = {
                "source_prim": str(sources[0].source.GetPath()),
                "source_attr": _connected_source_attr(sources[0]).qualified_name,
            }
            continue
        val = _usd_value_to_python(inp.Get())
        if val is not None:
            inputs[name] = val
            input_types[name] = str(inp.GetAttr().GetTypeName())

    # Output-side authored connections: NodeGraph/Material output ports
    # that bubble internal shader values up to consumers outside.  Shaders
    # generally don't author connections on their outputs, but the check
    # is uniform so we read them either way.
    for outp in connectable.GetOutputs():
        if not outp.GetAttr().HasAuthoredConnections():
            continue
        sources, _ = outp.GetConnectedSources()
        if not sources:
            continue
        connections[output_attr(outp.GetBaseName()).qualified_name] = {
            "source_prim": str(sources[0].source.GetPath()),
            "source_attr": _connected_source_attr(sources[0]).qualified_name,
        }

    return container_kind, info_id, inputs, input_types, connections


def read_camera_attrs(stage, prim_path):
    """Read authored UsdGeomCamera attributes from a prim.

    Returns a ``{name: python_value}`` dict for every authored camera attr,
    or ``None`` if the prim isn't a ``UsdGeom.Camera``. Mirrors the full-read
    pattern of ``read_usdshade_connectable`` so the diff loop emits the
    complete authored set on first encounter rather than just the attr that
    triggered the notice.
    """
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid() or not prim.IsA(UsdGeom.Camera):
        return None
    attrs = {}
    for name in _CAMERA_ATTR_NAMES:
        attr = prim.GetAttribute(name)
        if attr and attr.IsValid() and attr.IsAuthored():
            val = _usd_value_to_python(attr.Get())
            if val is not None:
                attrs[name] = val
    return attrs


# ---------------------------------------------------------------------------
# PrimChannel — uniform snapshot/diff/emit pipeline per per-prim state slice
# ---------------------------------------------------------------------------
#
# Each PrimChannel watches one slice of a prim's state (its camera attrs,
# its connectable interface, its references, etc.). On every diff cycle the
# channel reads the current state, diffs against the cached snapshot, and
# emits an event when the diff is non-empty.
#
# Adding support for a new typed schema is: subclass PrimChannel, hand the
# instance to NoticeEmitter via the ``extra_channels=`` constructor kwarg.
# The built-in set always runs; extras append after. Both the diff loop
# and ``seed_prim_cache`` iterate the same channel set, so no other code
# needs to know about the new channel.
#
# Channels with richer behavior live inline in _build_dirty_prim_events:
# TRS (uses a pre-computed snapshot shared with matrices), gprim attrs
# (dirty-driven selective scan with primvar metadata), matrices
# (diagnostic, opt-in via include_matrices), and the ensure_prim /
# api_schemas first-encounter handshake.


class PrimChannel:
    """One slice of per-prim state with a uniform read/diff/emit lifecycle.

    A channel watches its slice (camera attrs, connectable interface,
    references, …), reads the current state per cycle, diffs it against
    the per-prim cache, and emits wire events when something changed.

    Subclasses must set ``cache_key`` and implement ``read`` + ``to_event``.
    They typically also declare ``watched_attrs`` / ``watched_prefixes``
    (so the base ``needs_read`` knows when to skip a cycle), plus optionally
    override ``cache_default`` and ``diff`` for non-equality comparisons.
    ``to_event`` may return a single dict, a list of dicts (for channels
    that fan out into multiple wire events), or ``None`` to suppress.
    """

    cache_key: str = ""

    # Baseline ``diff`` compares against when the per-prim cache has no
    # entry yet. Set to the type-appropriate empty value (``{}`` / ``[]``
    # / ``""``) so a first-encounter prim with no authored state produces
    # no event. Leave as None when first-encounter should emit on any
    # non-None read (channels whose read returns a bool, for instance).
    cache_default = None

    # Opt into attr-level gating: ``needs_read`` looks for these names in
    # the dirty_attrs set and reads iff any match. For channels that
    # watch named USD properties — attributes (visibility, focalLength,
    # ...) or relationships (material:binding, ...).
    watched_attrs: tuple[str, ...] = ()

    # Same as ``watched_attrs`` but matched as ``str.startswith`` prefixes,
    # for channels watching a namespace (``inputs:*``, ``outputs:*``, ...)
    # rather than enumerating concrete names.
    watched_prefixes: tuple[str, ...] = ()

    # Opt into resync-only gating: skip the read whenever dirty_attrs has
    # any attr name (those can't be ours). For channels watching
    # composition arcs or stage-level state, where USD signals every
    # change as a resync notice rather than info-only. Mutually exclusive
    # with ``watched_attrs`` / ``watched_prefixes`` — NoticeEmitter
    # rejects channels that set both.
    reads_on_resync_only: bool = False

    def applies_to(self, prim) -> bool:
        """Does this channel apply to this prim at all? Cheap predicate."""
        return bool(prim and prim.IsValid())

    def needs_read(self, dirty_attrs: set[str] | None) -> bool:
        """Should this channel actually read on this cycle?

        ``dirty_attrs`` is the set of attr names the USD notice handler
        recorded as changed on this prim, or ``None`` when no per-attr
        info is available (resync notice / first encounter). Channels
        opt into one of two gating styles via class attributes:

        - ``reads_on_resync_only = True``: skip when dirty_attrs has any
          attr name (those can't be ours; our state changes via resync).
        - ``watched_attrs`` / ``watched_prefixes`` declared: read iff at
          least one name in dirty_attrs matches.

        Channels that declare neither default to always-read — the safe
        and slowest option.

        Reads are the expensive part of the diff loop (~hundreds of µs
        for connectables), so opting into a gate when correct is a big
        win at idle.
        """
        if not dirty_attrs:
            return True
        if self.reads_on_resync_only:
            return False
        if not self.watched_attrs and not self.watched_prefixes:
            return True
        for a in dirty_attrs:
            if a in self.watched_attrs:
                return True
            if self.watched_prefixes and a.startswith(self.watched_prefixes):
                return True
        return False

    def read(self, stage, prim_path):
        """Return current state, or ``None`` to skip the cache write."""
        raise NotImplementedError

    def diff(self, current, cached):
        """Return the diff to emit (any truthy value), or ``None`` if unchanged.

        Default uses ``cache_default`` as the cache-miss baseline so a
        first-encounter prim with no authored state doesn't produce a
        spurious empty event.
        """
        if cached is None:
            cached = self.cache_default
        return current if current != cached else None

    def to_event(self, prim_path, diff):
        """Build the wire event(s) for the diff.

        Return a ``dict`` for one event, ``list[dict]`` for multiple
        (channels that produce more than one event kind from a single
        read), or ``None`` to suppress.
        """
        raise NotImplementedError


class VariantSelectionsChannel(PrimChannel):
    cache_key = _C_VARIANT_SELECTIONS
    cache_default = {}
    reads_on_resync_only = True

    def read(self, stage, prim_path):
        return dict(read_variant_selections(stage, prim_path))

    def to_event(self, prim_path, diff):
        return {"k": K_SET_VARIANT_SELECTIONS, "prim": prim_path, "selections": diff}


class ReferencesChannel(PrimChannel):
    cache_key = _C_REFERENCES
    cache_default = []
    reads_on_resync_only = True

    def read(self, stage, prim_path):
        return read_references(stage, prim_path)

    def to_event(self, prim_path, diff):
        refs = []
        for asset_path, ref_prim_path in diff:
            entry: dict = {"asset_path": asset_path}
            if ref_prim_path:
                entry["prim_path"] = ref_prim_path
            refs.append(entry)
        return {"k": K_SET_REFERENCE, "prim": prim_path, "refs": refs}


class PayloadsChannel(PrimChannel):
    cache_key = _C_PAYLOADS
    cache_default = []
    reads_on_resync_only = True

    def read(self, stage, prim_path):
        return read_payloads(stage, prim_path)

    def to_event(self, prim_path, diff):
        payloads = []
        for asset_path, pay_prim_path in diff:
            entry: dict = {"asset_path": asset_path}
            if pay_prim_path:
                entry["prim_path"] = pay_prim_path
            payloads.append(entry)
        return {"k": K_SET_PAYLOAD, "prim": prim_path, "payloads": payloads}


class MaterialBindingChannel(PrimChannel):
    """``material:binding`` is a USD relationship — a named property on
    the prim, not a composition arc. Subsequent rebinds and clears fire
    info-only notices on the relationship name (resyncs only for the
    initial Apply), so this channel watches the property name like the
    other attribute/relationship channels.
    """

    cache_key = _C_MATERIAL_BINDING
    cache_default = ""
    watched_attrs = ("material:binding",)

    def read(self, stage, prim_path):
        return read_material_binding(stage, prim_path)

    def to_event(self, prim_path, diff):
        return {"k": K_SET_MATERIAL_BINDING, "prim": prim_path, "material_path": diff}


class ConnectableChannel(PrimChannel):
    """UsdShade.ConnectableAPI inputs + connections in one channel.

    Reads the connectable interface once per cycle and fans out into
    both wire events (``set_connectable_input``, ``set_connectable_connection``)
    so we don't pay the ~800 µs ``read_usdshade_connectable`` cost twice.
    """

    cache_key = _C_CONNECTABLE
    watched_attrs = ("info:id",)
    watched_prefixes = (USDSHADE_INPUT_PREFIX, USDSHADE_OUTPUT_PREFIX)

    def read(self, stage, prim_path):
        kind, info_id, inputs, types, conns = read_usdshade_connectable(stage, prim_path)
        if not kind:
            return None
        return {
            "info_id": info_id,
            "inputs": inputs,
            "types": types,
            "connections": conns,
        }

    def diff(self, current, cached):
        cached = cached or {}
        last_inputs = cached.get("inputs", {})
        last_conns = cached.get("connections", {})

        changed_inputs = {
            n: v for n, v in current["inputs"].items()
            if not _values_equal(v, last_inputs.get(n))
        }
        info_id = current["info_id"]
        # Bool guard: info_id transitions only matter for Shaders (non-empty
        # info:id). Lights and node graphs always carry empty info_id.
        info_id_changed = bool(info_id) and info_id != cached.get("info_id")

        new_conns: dict = {}
        removed_conns: list = []
        if current["connections"] != last_conns:
            new_conns = {
                k: v for k, v in current["connections"].items()
                if v != last_conns.get(k)
            }
            removed_conns = [k for k in last_conns if k not in current["connections"]]

        inputs_emit = changed_inputs or info_id_changed
        conns_emit = new_conns or removed_conns
        if not inputs_emit and not conns_emit:
            return None
        return {
            "info_id": info_id,
            "inputs": changed_inputs if inputs_emit else None,
            "input_types": (
                {n: current["types"][n] for n in changed_inputs}
                if inputs_emit else None
            ),
            "new_conns": new_conns if conns_emit else None,
            "removed_conns": removed_conns if conns_emit else None,
        }

    def to_event(self, prim_path, diff):
        events: list[dict] = []
        if diff["inputs"] is not None:
            events.append({
                "k": K_SET_CONNECTABLE_INPUT,
                "prim": prim_path,
                "info_id": diff["info_id"],
                "inputs": diff["inputs"],
                "input_types": diff["input_types"],
            })
        if diff["new_conns"] is not None or diff["removed_conns"]:
            ev: dict = {
                "k": K_SET_CONNECTABLE_CONNECTION,
                "prim": prim_path,
                "connections": diff["new_conns"] or {},
            }
            if diff["removed_conns"]:
                ev["disconnections"] = diff["removed_conns"]
            events.append(ev)
        return events


class CameraAttrsChannel(PrimChannel):
    """UsdGeomCamera typed-schema attribute replication.

    Reads every authored camera attr per cycle and emits them via the
    generic ``set_gprim_attrs`` wire event so the same applier path
    handles cameras alongside other typed-schema attrs.
    """

    cache_key = _C_CAMERA_ATTRS
    watched_attrs = tuple(_CAMERA_ATTR_NAMES)

    def applies_to(self, prim):
        return bool(prim and prim.IsValid() and prim.IsA(UsdGeom.Camera))

    def read(self, stage, prim_path):
        return read_camera_attrs(stage, prim_path) or {}

    def diff(self, current, cached):
        cached = cached or {}
        changed = {
            n: v for n, v in current.items()
            if not _values_equal(v, cached.get(n))
        }
        return changed if changed else None

    def to_event(self, prim_path, diff):
        return {"k": K_SET_GPRIM_ATTRS, "prim": prim_path, "attrs": diff}


class VisibilityChannel(PrimChannel):
    cache_key = _C_VISIBILITY
    watched_attrs = ("visibility",)

    def read(self, stage, prim_path):
        prim = stage.GetPrimAtPath(prim_path)
        if not prim or not prim.IsValid():
            return None
        vis_attr = UsdGeom.Imageable(prim).GetVisibilityAttr()
        if not vis_attr or not vis_attr.IsValid() or not vis_attr.IsAuthored():
            return None
        return vis_attr.Get() or "inherited"

    def to_event(self, prim_path, diff):
        return {"k": K_SET_VISIBILITY, "prim": prim_path, "visible": diff != "invisible"}


class PayloadLoadStateChannel(PrimChannel):
    """Payload load/unload toggle. Emits ``load_payload`` or ``unload_payload``
    depending on ``IsLoaded()``.

    Load state changes via ``stage.Load()`` / ``stage.Unload()`` and
    payload-arc edits arrive as USD resync notices on the prim, not as
    info-only edits.
    """

    cache_key = _C_PAYLOAD_LOADED
    reads_on_resync_only = True

    def read(self, stage, prim_path):
        prim = stage.GetPrimAtPath(prim_path)
        if not prim or not prim.IsValid() or not prim.HasAuthoredPayloads():
            return None
        return prim.IsLoaded()

    def to_event(self, prim_path, diff):
        return {
            "k": K_LOAD_PAYLOAD if diff else K_UNLOAD_PAYLOAD,
            "prim": prim_path,
        }


# Framework-owned channels — always active. Order is emit order: V before
# R per LIVERPS, then composition arcs, material/connectable, transform-
# adjacent state. Receive-side event_kind_order re-sorts, but consistent
# emit order keeps logs readable. Users append via ``extra_channels=``;
# this set is not replaceable so core USD types always replicate.
_BUILTIN_PRIM_CHANNELS: tuple[PrimChannel, ...] = (
    VariantSelectionsChannel(),
    ReferencesChannel(),
    PayloadsChannel(),
    PayloadLoadStateChannel(),
    MaterialBindingChannel(),
    ConnectableChannel(),
    VisibilityChannel(),
    CameraAttrsChannel(),
)


def _emit_channel_events(channel, prim_path, current, pc, events_out):
    """Run the channel's diff, append any events, refresh the cache."""
    cached = pc.get(channel.cache_key)
    d = channel.diff(current, cached)
    if d is not None:
        ev = channel.to_event(prim_path, d)
        if ev is not None:
            if isinstance(ev, list):
                events_out.extend(ev)
            else:
                events_out.append(ev)
    pc[channel.cache_key] = current


# ---------------------------------------------------------------------------
# Cache invalidation — for receivers applying remote events
# ---------------------------------------------------------------------------
#
# After a remote event is applied to the stage, the per-prim diff cache
# reflects pre-mutation state.  Without invalidation, the next emit cycle
# would compare current stage state to the stale cache and re-emit the
# change the server already knows about (a feedback loop).
#
# Each entry maps an event kind to a callable that re-syncs the affected
# channel from the emitter's stage.  Only stage-affecting kinds need
# entries; kinds that mutate DCC objects directly (TRS, visibility,
# gprim attrs) are absorbed by the depsgraph's normal write-back path,
# and ``suppressed()`` keeps them from echoing.


def _invalidate_ensure_prim(emitter, prim_path, _ev):
    emitter._known_prims.add(prim_path)


def _invalidate_delete_prim(emitter, prim_path, _ev):
    emitter._purge_caches(prim_path)
    prefix = prim_path + "/"
    for p in [k for k in list(emitter._known_prims) if k.startswith(prefix)]:
        emitter._purge_caches(p)


def _invalidate_deactivate_prim(emitter, prim_path, ev):
    # Active=False removes the subtree from the composed view; on the next
    # reactivation the child set re-composes, so child caches become stale.
    if not ev.get("active", True):
        emitter._purge_caches(prim_path)
        prefix = prim_path + "/"
        for p in [k for k in list(emitter._known_prims) if k.startswith(prefix)]:
            emitter._purge_caches(p)


def _invalidate_rename_prim(emitter, prim_path, ev):
    new_name = ev.get("new_name", "")
    if not new_name:
        return
    parent = prim_path.rsplit("/", 1)[0]
    new_path = f"{parent}/{new_name}" if parent else f"/{new_name}"
    emitter._migrate_caches(prim_path, new_path)


def _invalidate_set_reference(emitter, prim_path, _ev):
    pc = emitter._prim_cache.setdefault(prim_path, {})
    pc[_C_REFERENCES] = read_references(emitter.stage, prim_path)
    pc[_C_VARIANT_SELECTIONS] = read_variant_selections(emitter.stage, prim_path)
    # Composed children may carry their own variant selections — capture
    # them so subsequent diffs don't fire on imported state.
    prim = emitter.stage.GetPrimAtPath(prim_path)
    if prim and prim.IsValid():
        for child in Usd.PrimRange(prim):
            cp = str(child.GetPath())
            if cp == prim_path:
                continue
            cvs = read_variant_selections(emitter.stage, cp)
            if cvs:
                emitter._prim_cache.setdefault(cp, {})[_C_VARIANT_SELECTIONS] = cvs


def _invalidate_set_payload(emitter, prim_path, _ev):
    emitter._prim_cache.setdefault(prim_path, {})[_C_PAYLOADS] = read_payloads(
        emitter.stage, prim_path,
    )


def _invalidate_load_payload(emitter, prim_path, _ev):
    prim = emitter.stage.GetPrimAtPath(prim_path)
    if prim and prim.IsValid():
        emitter._prim_cache.setdefault(prim_path, {})[_C_PAYLOAD_LOADED] = prim.IsLoaded()


def _invalidate_unload_payload(emitter, prim_path, _ev):
    # Children have left the composed stage — drop their caches so they're
    # rediscovered on the next load_payload.
    prefix = prim_path + "/"
    for p in [k for k in list(emitter._known_prims) if k.startswith(prefix)]:
        emitter._purge_caches(p)
    prim = emitter.stage.GetPrimAtPath(prim_path)
    if prim and prim.IsValid():
        emitter._prim_cache.setdefault(prim_path, {})[_C_PAYLOAD_LOADED] = prim.IsLoaded()


def _invalidate_set_variant_selections(emitter, prim_path, _ev):
    emitter._prim_cache.setdefault(prim_path, {})[_C_VARIANT_SELECTIONS] = (
        read_variant_selections(emitter.stage, prim_path)
    )
    # Variant change rewrites the child set — purge caches under this prim
    # so they're rebuilt from the new composition.
    prefix = prim_path + "/"
    for p in [k for k in list(emitter._known_prims) if k.startswith(prefix)]:
        emitter._purge_caches(p)


def _invalidate_set_material_binding(emitter, prim_path, _ev):
    emitter._prim_cache.setdefault(prim_path, {})[_C_MATERIAL_BINDING] = read_material_binding(
        emitter.stage, prim_path,
    )


def _resync_connectable_cache(emitter, prim_path):
    """Re-read the full connectable interface into the combined cache slot.
    Used by both connectable input and connection invalidators since they
    share one read and one cache entry now.
    """
    kind, info_id, inputs, types, connections = read_usdshade_connectable(
        emitter.stage, prim_path,
    )
    if kind:
        emitter._prim_cache.setdefault(prim_path, {})[_C_CONNECTABLE] = {
            "info_id": info_id,
            "inputs": inputs,
            "types": types,
            "connections": connections,
        }


def _invalidate_set_connectable_input(emitter, prim_path, _ev):
    _resync_connectable_cache(emitter, prim_path)


def _invalidate_set_gprim_attrs(emitter, prim_path, _ev):
    cam_attrs = read_camera_attrs(emitter.stage, prim_path)
    if cam_attrs is not None:
        emitter._prim_cache.setdefault(prim_path, {})[_C_CAMERA_ATTRS] = cam_attrs


def _invalidate_set_connectable_connection(emitter, prim_path, _ev):
    _resync_connectable_cache(emitter, prim_path)


_INVALIDATE_DISPATCH = {
    K_ENSURE_PRIM: _invalidate_ensure_prim,
    K_DELETE_PRIM: _invalidate_delete_prim,
    K_DEACTIVATE_PRIM: _invalidate_deactivate_prim,
    K_RENAME_PRIM: _invalidate_rename_prim,
    K_SET_REFERENCE: _invalidate_set_reference,
    K_SET_PAYLOAD: _invalidate_set_payload,
    K_LOAD_PAYLOAD: _invalidate_load_payload,
    K_UNLOAD_PAYLOAD: _invalidate_unload_payload,
    K_SET_VARIANT_SELECTIONS: _invalidate_set_variant_selections,
    K_SET_MATERIAL_BINDING: _invalidate_set_material_binding,
    K_SET_CONNECTABLE_INPUT: _invalidate_set_connectable_input,
    K_SET_CONNECTABLE_CONNECTION: _invalidate_set_connectable_connection,
    K_SET_GPRIM_ATTRS: _invalidate_set_gprim_attrs,
}


class _SuppressScope:
    """Context manager for NoticeEmitter.suppressed().

    Calls suppress() on enter and unsuppress() on exit.
    __exit__ returns False -- exceptions propagate, never swallowed.
    """

    __slots__ = ("_emitter",)

    def __init__(self, emitter: NoticeEmitter):
        self._emitter = emitter

    def __enter__(self):
        self._emitter.suppress()
        return self._emitter

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._emitter.unsuppress()
        return False


class NoticeEmitter:
    """Watches a Usd.Stage for changes and builds idempotent transform events.

    Detects creation, deletion, deactivation, and renames via
    ``notice.GetPrimResyncType()`` on resync paths. Supports a reentrant
    suppress counter for feedback-loop prevention.

    Usage:
        emitter = NoticeEmitter(stage)
        # ... something authors to stage ...
        events = emitter.build_events_for_dirty()
        # events is a list of event dicts ready to wrap in a txn
    """

    def __init__(
        self,
        stage: Usd.Stage,
        attr_filter=None,
        *,
        replicated_api_schemas: set[str] | None = None,
        extra_channels: Sequence[PrimChannel] | None = None,
    ):
        """
        Args:
            stage: The Usd.Stage to watch.
            attr_filter: Optional callable(attr_name: str) -> bool.
                Controls which attributes are tracked for gprim attr diffing.
                Return True to track, False to skip. Default is derived from
                the channel set so any name a channel watches plus the
                inline-block-owned names are skipped. Primvars and other
                typed-schema attrs ARE tracked.
            replicated_api_schemas: Optional explicit override of the API
                schema names to replicate via the ensure_prim ``api_schemas``
                field. Each name must be a bare schema name (no
                ``":instance"``). If None, snapshots the module-level
                ``_REPLICATED_API_SCHEMAS`` at construction (default behavior
                — DCC integrations register their schemas at import time,
                then any later-constructed emitter picks them up).
            extra_channels: Optional additional ``PrimChannel`` instances to
                run alongside the built-in set. The framework-owned channels
                (variants, refs, payloads, material binding, connectable
                inputs/connections, visibility, camera attrs) are always
                active and run first; ``extra_channels`` are appended in
                order. Use this to replicate custom typed schemas without
                losing core USD coverage. Each channel's ``cache_key`` must
                be unique across the full set.
        """
        self.stage = stage
        if replicated_api_schemas is not None:
            for n in replicated_api_schemas:
                _validate_replicated_schema_name(n)
            self._replicated_apis: frozenset[str] = frozenset(replicated_api_schemas)
        else:
            self._replicated_apis = frozenset(_REPLICATED_API_SCHEMAS)
        self.dirty: set[str] = set()
        self._known_prims: set[str] = set()
        self._deleted_prims: set[str] = set()
        self._deactivated_prims: set[str] = set()
        self._renamed_prims: list[tuple[str, str]] = []  # (old_path, new_path)
        self._suppress_depth: int = 0
        self.listener = Tf.Notice.Register(Usd.Notice.ObjectsChanged, self._on_changed, stage)
        self.cache = UsdGeom.XformCache(Usd.TimeCode.Default())
        self._prim_cache: dict[str, dict] = {}
        # Per-prim names of attributes the USD info-only notice handler
        # saw change. Populated unfiltered so channels can gate reads on
        # specific names; the gprim attr scan re-applies _attr_filter
        # at iteration time to avoid leaking channel-owned attrs into
        # set_gprim_attrs.
        self._dirty_attrs: dict[str, set[str]] = {}
        self._notice_resynced_prims: set[str] = set()
        extras = tuple(extra_channels) if extra_channels else ()
        for ch in extras:
            if not isinstance(ch, PrimChannel):
                raise TypeError(
                    f"extra_channels must contain PrimChannel instances; "
                    f"got {type(ch).__name__}"
                )
        # Cache-key uniqueness: silent collisions would have one channel
        # overwrite another's cache, or stomp on an inline-block slot
        # (TRS / matrices / gprim attrs / api_schemas). Surface either
        # collision loudly at construction.
        seen_keys: set[str] = set(_INLINE_CACHE_KEYS)
        for ch in (*_BUILTIN_PRIM_CHANNELS, *extras):
            if ch.cache_key in seen_keys:
                raise ValueError(
                    f"Duplicate PrimChannel cache_key {ch.cache_key!r}; "
                    f"each channel must own a unique per-prim cache slot "
                    f"(reserved inline slots: {sorted(_INLINE_CACHE_KEYS)})."
                )
            seen_keys.add(ch.cache_key)
            # The two gating modes are mutually exclusive — they describe
            # different USD notice signaling patterns. Picking both is a
            # contradiction, not a stronger gate.
            if ch.reads_on_resync_only and (ch.watched_attrs or ch.watched_prefixes):
                raise ValueError(
                    f"{type(ch).__name__} declares both reads_on_resync_only "
                    f"and watched_attrs/watched_prefixes; pick one — resync-only "
                    f"for composition arcs, watched names for attributes/rels."
                )
        self._channels: tuple[PrimChannel, ...] = _BUILTIN_PRIM_CHANNELS + extras
        # Build the default gprim attr filter from the channel set so any
        # name a channel watches is automatically excluded from gprim
        # scan emit. User-provided ``attr_filter`` overrides this.
        self._attr_filter = attr_filter or _make_attr_filter(self._channels)

    def _filtered_api_schemas(self, prim: Usd.Prim) -> set[str]:
        """Return prim's applied schemas filtered through the whitelist.

        Schema names match USD's GetAppliedSchemas() format: bare ``"Name"``
        for single-apply, ``"Name:instance"`` for multi-apply. Returned as a
        set — api_schemas is logically unordered (composition reorderings
        shouldn't trigger spurious re-emits). Callers materialize a list at
        the wire boundary.
        """
        if not prim or not prim.IsValid():
            return set()
        apis = self._replicated_apis
        return {n for n in prim.GetAppliedSchemas() if n.split(":", 1)[0] in apis}

    def cleanup(self):
        """Deregister notice listener and clear all caches.

        Call this before discarding the emitter (e.g., on DCC addon
        unregister/reload) to prevent stale callbacks from firing.
        """
        if self.listener:
            self.listener.Revoke()
            self.listener = None
        self._prim_cache.clear()
        self._known_prims.clear()
        self._dirty_attrs.clear()
        self._notice_resynced_prims.clear()
        self.dirty.clear()
        self._suppress_depth = 0

    def seed_prim_cache(self, stage: Usd.Stage, prim_path: str):
        """Seed the per-prim diff cache for a prim and its composed children.

        Snapshots the current state of every applicable channel (plus the
        inline gprim-attrs and api_schemas slots) into the cache, so the
        next emit cycle diffs against authored state instead of treating
        everything as a first-encounter delta.

        Does NOT add to ``_known_prims`` — the emitter should still send
        structural events (ensure_prim, ensure_xform_ops) on first
        encounter so the server can create xform ops on payload prims.
        """
        prim = stage.GetPrimAtPath(prim_path)
        if not prim or not prim.IsValid():
            return
        for child in Usd.PrimRange(prim):
            cp = str(child.GetPath())
            pc = self._prim_cache.setdefault(cp, {})
            # Snapshot every applicable channel's current state into its
            # cache slot. Same channel set the diff loop iterates. A
            # channel whose read() returns None contributes nothing —
            # either applies_to was False or there's no authored state.
            for channel in self._channels:
                if not channel.applies_to(child):
                    continue
                current = channel.read(stage, cp)
                if current is None:
                    continue
                pc[channel.cache_key] = current
            # Seed the gprim-attr cache too (not a PrimChannel — uses the
            # dirty-driven selective scan in the diff loop). _attr_filter
            # excludes anything a channel already handles, so prims whose
            # state is fully channel-owned produce an empty snapshot here.
            gprim_snapshot = {}
            for attr in child.GetAttributes():
                name = attr.GetName()
                if attr.IsAuthored() and self._attr_filter(name):
                    val = _usd_value_to_python(attr.Get())
                    if val is not None:
                        gprim_snapshot[name] = val
            if gprim_snapshot:
                pc[_C_GPRIM_ATTRS] = gprim_snapshot
            # Seed the api_schemas snapshot so a later diff cycle doesn't
            # spuriously re-emit ensure_prim on first encounter.
            pc[_C_API_SCHEMAS] = self._filtered_api_schemas(child)

    def suppress(self):
        """Suppress notice collection (feedback guard).

        Reentrant: each call increments the suppress depth.
        Must be paired with a matching unsuppress() call.
        """
        self._suppress_depth += 1

    def unsuppress(self):
        """Resume notice collection.

        Decrements the suppress depth. Notices are only collected
        again when depth reaches zero.
        """
        assert self._suppress_depth > 0, "unsuppress() called without matching suppress()"
        self._suppress_depth -= 1

    def suppressed(self):
        """Return a context manager that suppresses notices for the block.

        Usage::

            with emitter.suppressed():
                apply_events(stage, events)
            # notices automatically resume here

        Reentrant -- nests correctly with other suppress/unsuppress calls.
        Exceptions are NOT swallowed: __exit__ returns False.
        """
        return _SuppressScope(self)

    def clear_all(self):
        """Flush all dirty/deleted/renamed sets without building events."""
        self.dirty.clear()
        self._deleted_prims.clear()
        self._deactivated_prims.clear()
        self._renamed_prims.clear()
        self._dirty_attrs.clear()
        self._notice_resynced_prims.clear()

    def _classify_resync(self, notice, prim_path: str) -> str | None:
        """Classify a resync path into an action.

        Returns "rename", "delete", "deactivate", "dirty", or None (skip).
        For renames, also appends to self._renamed_prims as a side effect.
        """
        if _PrimResyncType is not None:
            sdf_path = Sdf.Path(prim_path)
            resync_info = notice.GetPrimResyncType(sdf_path)
            resync_type = resync_info[0]
            associated_path = str(resync_info[1]) if len(resync_info) > 1 else ""

            if resync_type == _PrimResyncType.Delete:
                return "delete"
            if resync_type == _PrimResyncType.RenameSource:
                if associated_path and associated_path != ".":
                    self._renamed_prims.append((prim_path, associated_path))
                return "rename"
            if resync_type == _PrimResyncType.RenameDestination:
                return None

        # Fallback (or "Other" resync type with PrimResyncType available)
        prim = self.stage.GetPrimAtPath(prim_path)
        if prim and prim.IsValid():
            if not prim.IsActive() and prim_path in self._known_prims:
                return "deactivate"
            return "dirty"
        if prim_path in self._known_prims:
            return "delete"
        return None

    def _on_changed(self, notice, stage):
        if self._suppress_depth > 0:
            return

        for p in notice.GetResyncedPaths():
            prim_path = _prim_path_from_notice_path(str(p))
            if not prim_path:
                continue
            action = self._classify_resync(notice, prim_path)
            if action == "delete":
                self._deleted_prims.add(prim_path)
            elif action == "deactivate":
                self._deactivated_prims.add(prim_path)
            elif action == "dirty":
                self.dirty.add(prim_path)
                self._notice_resynced_prims.add(prim_path)

        for p in notice.GetChangedInfoOnlyPaths():
            path_str = str(p)
            prim_path = _prim_path_from_notice_path(path_str)
            if prim_path:
                self.dirty.add(prim_path)
                # Track every changed attr name — channels consult this set
                # to gate their reads on whether anything they care about
                # changed (e.g. ConnectableChannel skips when no inputs:*
                # appears). The gprim attr scan applies its own filter at
                # iteration time, so this set staying unfiltered doesn't
                # leak xformOp/inputs/visibility into gprim emit.
                if "." in path_str:
                    attr_name = path_str.split(".", 1)[1]
                    self._dirty_attrs.setdefault(prim_path, set()).add(attr_name)

    def mark_dirty(self, prim_path: str):
        """Manually mark a prim as dirty (useful for DCC integrations)."""
        self.dirty.add(prim_path)

    def invalidate_for_event(self, ev: dict) -> None:
        """Sync internal diff caches with a remotely-applied event.

        After a receiver applies a network event to the stage, the diff
        cache reflects pre-mutation state.  Pass each applied event
        through here so the next ``build_events_for_dirty()`` doesn't
        re-emit a change the server already knows about.

        Idempotent.  Safe inside or outside a ``suppressed()`` block.
        Unknown event kinds are no-ops.
        """
        k = ev.get("k")
        prim_path = ev.get("prim", "")
        if not k or not prim_path:
            return
        fn = _INVALIDATE_DISPATCH.get(k)
        if fn is not None:
            fn(self, prim_path, ev)

    def invalidate_for_events(self, events: list[dict]) -> None:
        """Batch version of :meth:`invalidate_for_event`."""
        for ev in events:
            self.invalidate_for_event(ev)

    def snapshot_events(
        self, eps_trs: float = 1e-9, eps_mat: float = 1e-12, include_matrices: bool = False
    ) -> list[dict]:
        """Build events for every prim on the stage as if newly authored.

        Marks every prim under the pseudo-root dirty and runs the normal
        build-events pipeline. Equivalent to walking ``Usd.PrimRange`` and
        calling ``mark_dirty`` on each path, then ``build_events_for_dirty``.

        Useful for initial-sync scenarios — a DCC plugin coming online with
        a populated stage, a replay harness reproducing a captured scene,
        or tests that need a full event stream for an authored stage.
        """
        for prim in Usd.PrimRange(self.stage.GetPseudoRoot()):
            path = str(prim.GetPath())
            if path != "/":
                self.mark_dirty(path)
        return self.build_events_for_dirty(
            eps_trs=eps_trs, eps_mat=eps_mat, include_matrices=include_matrices
        )

    def snapshot_prim(self, prim_path: str) -> dict | None:
        """Snapshot the current local transform of a prim as TRS + matrices."""
        prim = self.stage.GetPrimAtPath(prim_path)
        if not prim or not prim.IsValid():
            return None

        xf = UsdGeom.Xformable(prim)
        local_ret = xf.GetLocalTransformation(Usd.TimeCode.Default())
        local_m = as_matrix(local_ret)

        self.cache.SetTime(Usd.TimeCode.Default())
        world_m = self.cache.GetLocalToWorldTransform(prim)

        t, r, s = decompose_trs_from_matrix(local_m)

        return {
            "local_m16": mat_to_16(local_m),
            "world_m16": mat_to_16(world_m),
            "t": t,
            "r": r,
            "s": s,
        }

    def _migrate_caches(self, old_path: str, new_path: str):
        """Migrate all per-prim caches from old_path to new_path."""
        if old_path in self._known_prims:
            self._known_prims.discard(old_path)
            self._known_prims.add(new_path)
        if old_path in self._prim_cache:
            self._prim_cache[new_path] = self._prim_cache.pop(old_path)

    def _purge_caches(self, prim_path: str):
        """Remove all per-prim caches for a deactivated/deleted prim."""
        self._known_prims.discard(prim_path)
        self._prim_cache.pop(prim_path, None)
        self._dirty_attrs.pop(prim_path, None)
        self.dirty.discard(prim_path)

    def _build_rename_events(self) -> list[dict]:
        """Build rename events and migrate caches."""
        events: list[dict] = []
        renamed_now = list(self._renamed_prims)
        self._renamed_prims.clear()
        for old_path, new_path in renamed_now:
            new_name = new_path.rsplit("/", 1)[-1]
            events.append({"k": K_RENAME_PRIM, "prim": old_path, "new_name": new_name})
            self._migrate_caches(old_path, new_path)
            if old_path in self.dirty:
                self.dirty.discard(old_path)
                self.dirty.add(new_path)
        return events

    def _build_deactivation_events(self) -> list[dict]:
        """Build deactivation events for deactivated and deleted prims."""
        events: list[dict] = []
        deactivated_now = self._deactivated_prims | self._deleted_prims
        self._deactivated_prims.clear()
        self._deleted_prims.clear()
        for prim_path in deactivated_now:
            events.append({"k": K_DEACTIVATE_PRIM, "prim": prim_path, "active": False})
            self._purge_caches(prim_path)
        return events

    def _build_dirty_prim_events(
        self,
        prim_path: str,
        snap: dict,
        eps_trs: float,
        eps_mat: float,
        include_matrices: bool,
    ) -> list[dict]:
        """Build events for a single dirty prim: structural, ref, TRS, visibility, matrices."""
        events: list[dict] = []
        pc = self._prim_cache.setdefault(prim_path, {})
        prim = self.stage.GetPrimAtPath(prim_path)

        # Structural events on first encounter
        if prim_path not in self._known_prims:
            # Preserve untyped prims (empty typeName) — Materials/Shaders
            # scopes are commonly authored as `def "Materials"` with no type.
            # Fall back to Xform only when the prim itself is invalid.
            type_name = ""
            if prim and prim.IsValid():
                type_name = str(prim.GetTypeName())
            api_schemas = self._filtered_api_schemas(prim)
            events.append({
                "k": K_ENSURE_PRIM,
                "prim": prim_path,
                "typeName": type_name,
                "api_schemas": list(api_schemas),
            })
            pc[_C_API_SCHEMAS] = api_schemas
            # Only emit xform ops for prims that have transforms —
            # Materials, Shaders, NodeGraphs, Scopes don't.
            xf = UsdGeom.Xformable(prim) if prim else None
            if xf and xf.GetXformOpOrderAttr().IsAuthored():
                events.append({"k": K_ENSURE_XFORM_OPS, "prim": prim_path})
            self._known_prims.add(prim_path)
        else:
            # Re-emit ensure_prim when the applied api_schemas change (e.g.
            # ShapingAPI applied to an existing SphereLight to make it a spot).
            last_apis = pc.get(_C_API_SCHEMAS)
            current_apis = self._filtered_api_schemas(prim)
            if last_apis is not None and current_apis != last_apis:
                type_name = str(prim.GetTypeName()) if prim and prim.IsValid() else ""
                events.append({
                    "k": K_ENSURE_PRIM,
                    "prim": prim_path,
                    "typeName": type_name,
                    "api_schemas": list(current_apis),
                })
                pc[_C_API_SCHEMAS] = current_apis

        # Pass dirty_attrs=None when a resync notice fired, so channels treat
        # the cycle as first-encounter and read unconditionally — resyncs
        # carry no per-attr info but signal "anything could have changed."
        # The gprim attr block downstream consumes the resync entry; we
        # only peek here.
        dirty_attrs = self._dirty_attrs.get(prim_path)
        if prim_path in self._notice_resynced_prims:
            dirty_attrs = None
        for channel in self._channels:
            if not channel.applies_to(prim):
                continue
            if not channel.needs_read(dirty_attrs):
                continue
            current = channel.read(self.stage, prim_path)
            if current is None:
                continue
            _emit_channel_events(channel, prim_path, current, pc, events)

        # TRS partial diff — uses the pre-computed snap from the outer loop,
        # so it stays inline rather than going through a channel.
        xf = UsdGeom.Xformable(prim) if prim else None
        has_xform = xf and xf.GetXformOpOrderAttr().IsAuthored()

        if has_xform:
            last_trs = pc.get(_C_TRS, {})
            fields = []
            payload = {"k": K_SET_XFORM_TRS, "prim": prim_path, "fields": fields}

            if not near_list(snap["t"], last_trs.get("t"), eps_trs):
                fields.append("t")
                payload["t"] = snap["t"]
            if not near_list(snap["r"], last_trs.get("r"), eps_trs):
                fields.append("r")
                payload["r"] = snap["r"]
            if not near_list(snap["s"], last_trs.get("s"), eps_trs):
                fields.append("s")
                payload["s"] = snap["s"]

            if fields:
                events.append(payload)
                pc[_C_TRS] = {"t": snap["t"], "r": snap["r"], "s": snap["s"]}

        # Gprim attribute diff
        if prim and prim.IsValid():
            dirty_attr_names = self._dirty_attrs.pop(prim_path, set())
            last_attrs = pc.get(_C_GPRIM_ATTRS, {})

            # Full attr scan: needed on first encounter (cache empty) or
            # after a resync notice (variant switch, structural change).
            # Skipped for plain info-only changes (e.g., only xformOp values
            # changed) where the cache is already populated — avoids reading
            # thousands of mesh vertices every frame.
            is_resync = prim_path in self._notice_resynced_prims
            self._notice_resynced_prims.discard(prim_path)
            if not dirty_attr_names and (not last_attrs or is_resync):
                for attr in prim.GetAttributes():
                    name = attr.GetName()
                    if attr.IsAuthored() and self._attr_filter(name):
                        dirty_attr_names.add(name)

            changed_attrs = {}
            primvar_meta = {}
            attr_interp = {}
            pvapi = None  # lazy — only created if a primvar actually changed
            for attr_name in dirty_attr_names:
                # _dirty_attrs is unfiltered (channels need every changed name
                # for gating); the gprim-specific filter runs here so dedicated-
                # channel attrs (xformOps, visibility, inputs:*, info:id, ...)
                # don't leak into set_gprim_attrs.
                if not self._attr_filter(attr_name):
                    continue
                attr = prim.GetAttribute(attr_name)
                if not attr or not attr.IsValid():
                    continue
                val = _usd_value_to_python(attr.Get())
                if val is None:
                    continue
                if not _values_equal(val, last_attrs.get(attr_name)):
                    changed_attrs[attr_name] = val
                    if attr_name.startswith(PRIMVAR_PREFIX):
                        # Primvar: include USD type name and interpolation so
                        # the receiver can create non-schema primvars with the
                        # exact type.
                        if pvapi is None:
                            pvapi = UsdGeom.PrimvarsAPI(prim)
                        pv = pvapi.GetPrimvar(attr_name[len(PRIMVAR_PREFIX) :])
                        if pv:
                            meta = {"typeName": str(attr.GetTypeName())}
                            if pv.HasAuthoredInterpolation():
                                meta["interpolation"] = str(pv.GetInterpolation())
                            primvar_meta[attr_name] = meta
                    else:
                        # Non-primvar: capture authored interpolation metadata
                        # (e.g. normals has per-attr interpolation).
                        interp = attr.GetMetadata("interpolation")
                        if interp:
                            attr_interp[attr_name] = str(interp)

            if changed_attrs:
                ev = {
                    "k": K_SET_GPRIM_ATTRS,
                    "prim": prim_path,
                    "attrs": changed_attrs,
                }
                if primvar_meta:
                    ev["primvar_meta"] = primvar_meta
                if attr_interp:
                    ev["attr_interp"] = attr_interp
                events.append(ev)
                pc.setdefault(_C_GPRIM_ATTRS, {}).update(changed_attrs)

        # Optional matrices event (diagnostic)
        if include_matrices:
            last_mats = pc.get(_C_MATS, {})
            if not near_list(snap["local_m16"], last_mats.get("local"), eps_mat) or not near_list(
                snap["world_m16"], last_mats.get("world"), eps_mat
            ):
                events.append(
                    {
                        "k": K_SET_XFORM_MATRICES,
                        "prim": prim_path,
                        "local_m": snap["local_m16"],
                        "world_m": snap["world_m16"],
                    }
                )
                pc[_C_MATS] = {
                    "local": snap["local_m16"],
                    "world": snap["world_m16"],
                }

        return events

    def build_events_for_dirty(
        self, eps_trs: float = 1e-9, eps_mat: float = 1e-12, include_matrices: bool = False
    ) -> list[dict]:
        """Build events for all dirty prims, diffing against last-sent state.

        Returns a list of event dicts (ensure_prim, ensure_xform_ops, set_xform_trs,
        rename_prim, deactivate_prim, optionally set_xform_matrices) ready to wrap
        in a transaction.

        Processing order: renames first, then deactivations/deletions, then TRS.
        """
        events: list[dict] = []

        events.extend(self._build_rename_events())
        events.extend(self._build_deactivation_events())

        # Dirty prims (creation + TRS changes)
        # Sort by path depth so parents are emitted before children.
        dirty_now = sorted(self.dirty, key=lambda p: p.count("/"))
        self.dirty.clear()

        for prim_path in dirty_now:
            snap = self.snapshot_prim(prim_path)
            if snap is None:
                continue
            events.extend(
                self._build_dirty_prim_events(prim_path, snap, eps_trs, eps_mat, include_matrices)
            )

        return events
