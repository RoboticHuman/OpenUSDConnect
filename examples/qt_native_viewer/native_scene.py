"""Application-owned scene graph and direct ``DCCAdapter`` implementation.

This module is deliberately independent of Qt. A host integration replaces
``NativeScene`` mutations with calls into its own scene/object API while keeping
the same adapter boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from openusdconnect import DCCAdapter


def _plain_value(value):
    """Detach numpy-backed receive values from the network decode buffer."""
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, dict):
        return {key: _plain_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_value(item) for item in value]
    if isinstance(value, set):
        return sorted(value)
    return value


@dataclass(slots=True)
class NativeObject:
    """State one external application might keep for a scene object."""

    path: str
    type_name: str = "Xform"
    api_schemas: set[str] = field(default_factory=set)
    xform_ready: bool = False
    translation: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    rotation: list[float] = field(default_factory=lambda: [1.0, 0.0, 0.0, 0.0])
    scale: list[float] = field(default_factory=lambda: [1.0, 1.0, 1.0])
    transform_samples: dict[float, dict] = field(default_factory=dict)
    visible: bool = True
    visibility_samples: dict[float, bool] = field(default_factory=dict)
    active: bool = True
    attributes: dict = field(default_factory=dict)
    attribute_samples: dict[float, dict] = field(default_factory=dict)
    references: list = field(default_factory=list)
    reference_list_op_authored: bool = False
    reference_list_op_explicit: bool = False
    payloads: list = field(default_factory=list)
    payload_list_op_authored: bool = False
    payload_list_op_explicit: bool = False
    payload_loaded: bool = True
    variant_selections: dict[str, str] = field(default_factory=dict)
    material_bindings: dict[str, str] = field(default_factory=dict)
    shader_id: str = ""
    connectable_inputs: dict = field(default_factory=dict)
    connectable_input_types: dict = field(default_factory=dict)
    connectable_input_samples: dict[float, dict] = field(default_factory=dict)
    connectable_connections: dict = field(default_factory=dict)
    instanceable: bool = False
    point_instancer: dict = field(default_factory=dict)
    point_instancer_samples: dict[float, dict] = field(default_factory=dict)


class NativeScene:
    """Plain application scene with no USD stage as its backing store."""

    def __init__(self):
        self.objects: dict[str, NativeObject] = {}
        self.stage_metadata: dict = {}
        self.revision = 0

    def ensure_object(self, path: str, type_name: str = "Xform") -> NativeObject:
        obj = self.objects.get(path)
        if obj is None:
            obj = NativeObject(path=path, type_name=type_name or "Xform")
            self.objects[path] = obj
        return obj

    def require_object(self, path: str, operation: str) -> NativeObject:
        obj = self.objects.get(path)
        if obj is None:
            raise RuntimeError(f"{operation} requires an existing native object at {path}")
        return obj

    def delete_subtree(self, path: str) -> bool:
        prefix = path.rstrip("/") + "/"
        removed = [
            candidate
            for candidate in self.objects
            if candidate == path or candidate.startswith(prefix)
        ]
        for candidate in removed:
            del self.objects[candidate]
        return bool(removed)

    def rename_subtree(self, path: str, new_name: str) -> bool:
        if path not in self.objects:
            return False
        if not new_name or "/" in new_name:
            raise ValueError(f"invalid native object name {new_name!r}")
        parent = path.rsplit("/", 1)[0]
        new_root = f"{parent}/{new_name}"
        prefix = path.rstrip("/") + "/"
        moved = [
            candidate
            for candidate in self.objects
            if candidate == path or candidate.startswith(prefix)
        ]
        destinations = {new_root + candidate[len(path) :] for candidate in moved}
        collisions = destinations.intersection(self.objects).difference(moved)
        if collisions:
            raise RuntimeError(f"rename_prim would replace native objects: {sorted(collisions)!r}")
        replacements = {}
        for old_path in sorted(moved, key=lambda candidate: candidate.count("/")):
            obj = self.objects.pop(old_path)
            suffix = old_path[len(path) :]
            obj.path = new_root + suffix
            replacements[obj.path] = obj
        self.objects.update(replacements)
        return True

    def clear(self) -> None:
        self.objects.clear()
        self.stage_metadata.clear()
        self.revision += 1


class NativeSceneAdapter(DCCAdapter):
    """Reference adapter for an application with its own non-USD scene."""

    def __init__(
        self,
        scene: NativeScene,
        on_changed: Callable[[list[dict]], None] | None = None,
    ):
        self.scene = scene
        self.on_changed = on_changed

    def targets_stage(self):
        """Select composed projection by declaring a non-USD destination."""
        return None

    def apply_events(self, events: list[dict]) -> int:
        """Use public event dispatch, then refresh the host UI once per batch."""
        count = super().apply_events(events)
        self.scene.revision += 1
        if self.on_changed is not None:
            self.on_changed(events)
        return count

    def ensure_prim(
        self,
        prim_path: str,
        type_name: str = "Xform",
        api_schemas: list[str] | None = None,
    ) -> bool:
        obj = self.scene.ensure_object(prim_path, type_name)
        obj.api_schemas.update(api_schemas or ())
        return True

    def ensure_xform_ops(self, prim_path: str) -> bool:
        obj = self.scene.require_object(prim_path, "ensure_xform_ops")
        # A real host folds any pre-transform/offset into its canonical local
        # transform here. This scene already stores only canonical local TRS.
        obj.xform_ready = True
        return True

    def set_xform_trs(
        self,
        prim_path: str,
        *,
        t: list[float] | None = None,
        r: list[float] | None = None,
        s: list[float] | None = None,
        time: float | None = None,
    ) -> bool:
        obj = self.scene.require_object(prim_path, "set_xform_trs")
        values = {
            key: _plain_value(value)
            for key, value in (("t", t), ("r", r), ("s", s))
            if value is not None
        }
        if time is not None:
            obj.transform_samples.setdefault(float(time), {}).update(values)
            return True
        if t is not None:
            obj.translation = values["t"]
        if r is not None:
            obj.rotation = values["r"]
        if s is not None:
            obj.scale = values["s"]
        return True

    def delete_prim(self, prim_path: str) -> bool:
        return self.scene.delete_subtree(prim_path)

    def deactivate_prim(self, prim_path: str, active: bool = False) -> bool:
        self.scene.require_object(prim_path, "deactivate_prim").active = bool(active)
        return True

    def rename_prim(self, prim_path: str, new_name: str) -> bool:
        return self.scene.rename_subtree(prim_path, new_name)

    def set_visibility(
        self,
        prim_path: str,
        visible: bool,
        time: float | None = None,
    ) -> bool:
        obj = self.scene.require_object(prim_path, "set_visibility")
        if time is None:
            obj.visible = bool(visible)
        else:
            obj.visibility_samples[float(time)] = bool(visible)
        return True

    def set_gprim_attrs(
        self,
        prim_path: str,
        attrs: dict,
        time: float | None = None,
    ) -> bool:
        obj = self.scene.require_object(prim_path, "set_gprim_attrs")
        values = _plain_value(attrs)
        if time is None:
            obj.attributes.update(values)
        else:
            obj.attribute_samples.setdefault(float(time), {}).update(values)
        return True

    def set_reference(
        self,
        prim_path: str,
        refs: list,
        *,
        list_op_authored: bool,
        list_op_explicit: bool,
    ) -> bool:
        obj = self.scene.ensure_object(prim_path)
        obj.references = _plain_value(refs)
        obj.reference_list_op_authored = bool(list_op_authored)
        obj.reference_list_op_explicit = bool(list_op_explicit)
        return True

    def set_payload(
        self,
        prim_path: str,
        payloads: list,
        *,
        list_op_authored: bool,
        list_op_explicit: bool,
    ) -> bool:
        obj = self.scene.ensure_object(prim_path)
        obj.payloads = _plain_value(payloads)
        obj.payload_list_op_authored = bool(list_op_authored)
        obj.payload_list_op_explicit = bool(list_op_explicit)
        return True

    def load_payload(self, prim_path: str) -> bool:
        self.scene.require_object(prim_path, "load_payload").payload_loaded = True
        return True

    def unload_payload(self, prim_path: str) -> bool:
        self.scene.require_object(prim_path, "unload_payload").payload_loaded = False
        return True

    def set_variant_selections(self, prim_path: str, selections: dict[str, str]) -> bool:
        obj = self.scene.ensure_object(prim_path)
        obj.variant_selections = dict(selections)
        return True

    def set_material_binding(
        self,
        prim_path: str,
        material_path: str,
        material_purpose: str = "",
    ) -> bool:
        obj = self.scene.require_object(prim_path, "set_material_binding")
        if material_path:
            obj.material_bindings[material_purpose] = material_path
        else:
            obj.material_bindings.pop(material_purpose, None)
        return True

    def set_connectable_input(
        self,
        prim_path: str,
        info_id: str,
        inputs: dict,
        input_types: dict,
        time: float | None = None,
    ) -> bool:
        obj = self.scene.ensure_object(prim_path, "Shader" if info_id else "NodeGraph")
        obj.shader_id = info_id
        values = _plain_value(inputs)
        if time is None:
            obj.connectable_inputs.update(values)
            obj.connectable_input_types.update(input_types)
        else:
            obj.connectable_input_samples.setdefault(float(time), {}).update(values)
        return True

    def set_connectable_connection(
        self,
        prim_path: str,
        connections: dict,
        disconnections: list | None = None,
    ) -> bool:
        obj = self.scene.ensure_object(prim_path, "Shader")
        obj.connectable_connections.update(_plain_value(connections))
        for name in disconnections or ():
            obj.connectable_connections.pop(name, None)
        return True

    def set_stage_metadata(
        self,
        *,
        timeCodesPerSecond: float | None = None,
        framesPerSecond: float | None = None,
        startTimeCode: float | None = None,
        endTimeCode: float | None = None,
        metersPerUnit: float | None = None,
        upAxis: str | None = None,
    ) -> bool:
        values = {
            "timeCodesPerSecond": timeCodesPerSecond,
            "framesPerSecond": framesPerSecond,
            "startTimeCode": startTimeCode,
            "endTimeCode": endTimeCode,
            "metersPerUnit": metersPerUnit,
            "upAxis": upAxis,
        }
        self.scene.stage_metadata.update(
            {key: value for key, value in values.items() if value is not None}
        )
        return True

    def set_instanceable(self, prim_path: str, instanceable: bool) -> bool:
        self.scene.require_object(prim_path, "set_instanceable").instanceable = bool(instanceable)
        return True

    def set_point_instancer(
        self,
        prim_path: str,
        *,
        prototypes: list[str] | None = None,
        proto_indices=None,
        positions=None,
        orientations=None,
        scales=None,
        velocities=None,
        accelerations=None,
        angular_velocities=None,
        ids=None,
        invisible_ids=None,
        inactive_ids=None,
        time: float | None = None,
    ) -> bool:
        obj = self.scene.ensure_object(prim_path, "PointInstancer")
        values = {
            key: _plain_value(value)
            for key, value in (
                ("prototypes", prototypes),
                ("proto_indices", proto_indices),
                ("positions", positions),
                ("orientations", orientations),
                ("scales", scales),
                ("velocities", velocities),
                ("accelerations", accelerations),
                ("angular_velocities", angular_velocities),
                ("ids", ids),
                ("invisible_ids", invisible_ids),
                ("inactive_ids", inactive_ids),
            )
            if value is not None
        }
        if time is None:
            obj.point_instancer.update(values)
        else:
            obj.point_instancer_samples.setdefault(float(time), {}).update(values)
        return True

    def reset(self) -> None:
        """Clear application state before a complete authoritative replay."""
        self.scene.clear()
        if self.on_changed is not None:
            self.on_changed([])


__all__ = ["NativeObject", "NativeScene", "NativeSceneAdapter"]
