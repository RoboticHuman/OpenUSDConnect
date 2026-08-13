"""Projection of layered USD composition into native adapter events."""

from __future__ import annotations

import pytest
from pxr import Sdf, Usd, UsdShade

from openusdconnect.adapters import MockAdapter
from openusdconnect.codec import encode_message
from openusdconnect.composed_projection import (
    ComposedChangeProjection,
    ComposedProjectionState,
)
from openusdconnect.dispatcher import EventDispatcher
from openusdconnect.event_apply import apply_events
from openusdconnect.sdf_spec_delta import serialize_spec_fields

from .layered_replay_test_support import (
    _BASE_LAYER,
    _STRONG_LAYER,
    _WEAK_LAYER,
    _clear_prim_field_event,
    _clear_property_event,
    _clear_translate_event,
    _event,
    _LayeredQueue,
    _state,
    _xform_events,
)


def _exact_spec_event(
    layer: Sdf.Layer,
    spec_path: str,
    spec_kind: str,
    fields: list[str],
) -> dict:
    return {
        "k": "set_sdf_spec_fields",
        "prim": "/" if spec_kind == "layer" else spec_path,
        "spec_path": spec_path,
        "spec_kind": spec_kind,
        "fields": fields,
        "fragment": serialize_spec_fields(
            layer,
            spec_path,
            spec_kind,
            fields,
            stabilize_asset_paths=False,
        ),
        "removed": False,
    }


@pytest.mark.parametrize("field", ["inheritPaths", "specializes"])
def test_class_arc_field_projects_new_composed_subtree(field):
    stage = Usd.Stage.CreateInMemory()
    stage.CreateClassPrim("/Class")
    child = stage.DefinePrim("/Class/Child", "Sphere")
    child.GetAttribute("radius").Set(4.0)
    stage.DefinePrim("/World", "Xform")

    incoming = Sdf.Layer.CreateAnonymous("class-arc.usda")
    incoming.TransferContent(stage.GetRootLayer())
    incoming_stage = Usd.Stage.Open(incoming)
    world = incoming_stage.GetPrimAtPath("/World")
    if field == "inheritPaths":
        world.GetInherits().AddInherit("/Class")
    else:
        world.GetSpecializes().AddSpecialize("/Class")
    event = _exact_spec_event(incoming, "/World", "prim", [field])

    projection = ComposedChangeProjection(stage, [event])
    apply_events(stage, [event])
    projected = projection.build_events()

    assert stage.GetPrimAtPath("/World/Child")
    assert any(item["k"] == "ensure_prim" and item["prim"] == "/World/Child" for item in projected)
    assert any(
        item["k"] == "set_gprim_attrs"
        and item["prim"] == "/World/Child"
        and item["attrs"]["radius"] == pytest.approx(4.0)
        for item in projected
    )
    adapter = MockAdapter()
    adapter.apply_events(projected)
    assert adapter.get_prim("/World/Child")["gprim_attrs"]["radius"] == pytest.approx(4.0)

    cleared = Sdf.Layer.CreateAnonymous("class-arc-cleared.usda")
    cleared.TransferContent(stage.GetRootLayer())
    cleared_stage = Usd.Stage.Open(cleared)
    cleared_world = cleared_stage.GetPrimAtPath("/World")
    if field == "inheritPaths":
        cleared_world.GetInherits().ClearInherits()
    else:
        cleared_world.GetSpecializes().ClearSpecializes()
    clear_event = _exact_spec_event(cleared, "/World", "prim", [field])

    clear_projection = ComposedChangeProjection(stage, [clear_event])
    apply_events(stage, [clear_event])
    clear_projected = clear_projection.build_events()

    assert not stage.GetPrimAtPath("/World/Child")
    assert any(
        item["k"] == "delete_prim" and item["prim"] == "/World/Child" for item in clear_projected
    )
    adapter.apply_events(clear_projected)
    assert adapter.get_prim("/World/Child") == {}


def test_layer_relocates_projects_old_and_new_composed_subtrees():
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/Ref", "Xform")
    child = stage.DefinePrim("/Ref/Child", "Sphere")
    child.GetAttribute("radius").Set(5.0)
    stage.DefinePrim("/World", "Xform").GetReferences().AddInternalReference("/Ref")
    assert stage.GetPrimAtPath("/World/Child")

    incoming = Sdf.Layer.CreateAnonymous("relocates.usda")
    incoming.TransferContent(stage.GetRootLayer())
    incoming.relocates = [
        (Sdf.Path("/World/Child"), Sdf.Path("/World/Moved")),
    ]
    event = _exact_spec_event(incoming, "/", "layer", ["layerRelocates"])

    projection = ComposedChangeProjection(stage, [event])
    apply_events(stage, [event])
    projected = projection.build_events()

    assert not stage.GetPrimAtPath("/World/Child")
    assert stage.GetPrimAtPath("/World/Moved")
    assert any(item["k"] == "delete_prim" and item["prim"] == "/World/Child" for item in projected)
    assert any(item["k"] == "ensure_prim" and item["prim"] == "/World/Moved" for item in projected)
    assert any(
        item["k"] == "set_gprim_attrs"
        and item["prim"] == "/World/Moved"
        and item["attrs"]["radius"] == pytest.approx(5.0)
        for item in projected
    )
    adapter = MockAdapter()
    adapter.ensure_prim("/World/Child", "Sphere")
    adapter.set_gprim_attrs("/World/Child", {"radius": 5.0})
    adapter.apply_events(projected)
    assert adapter.get_prim("/World/Child") == {}
    assert adapter.get_prim("/World/Moved")["gprim_attrs"]["radius"] == pytest.approx(5.0)

    cleared = Sdf.Layer.CreateAnonymous("relocates-cleared.usda")
    cleared.TransferContent(stage.GetRootLayer())
    cleared.relocates = []
    clear_event = _exact_spec_event(cleared, "/", "layer", ["layerRelocates"])

    clear_projection = ComposedChangeProjection(stage, [clear_event])
    apply_events(stage, [clear_event])
    clear_projected = clear_projection.build_events()

    assert stage.GetPrimAtPath("/World/Child")
    assert not stage.GetPrimAtPath("/World/Moved")
    assert any(
        item["k"] == "ensure_prim" and item["prim"] == "/World/Child" for item in clear_projected
    )
    assert any(
        item["k"] == "delete_prim" and item["prim"] == "/World/Moved" for item in clear_projected
    )
    adapter.apply_events(clear_projected)
    assert adapter.get_prim("/World/Moved") == {}
    assert adapter.get_prim("/World/Child")["gprim_attrs"]["radius"] == pytest.approx(5.0)


@pytest.mark.parametrize("field", ["inheritPaths", "specializes"])
def test_class_arc_notice_projects_implied_consumer_without_class_artifacts(field):
    stage = Usd.Stage.CreateInMemory()
    stage.CreateClassPrim("/Class")
    child = stage.DefinePrim("/Class/Child", "Sphere")
    child.GetAttribute("radius").Set(6.0)
    intermediate = stage.CreateClassPrim("/Intermediate")
    world = stage.DefinePrim("/World", "Xform")
    if field == "inheritPaths":
        world.GetInherits().AddInherit(intermediate.GetPath())
    else:
        world.GetSpecializes().AddSpecialize(intermediate.GetPath())
    assert not stage.GetPrimAtPath("/World/Child")

    incoming = Sdf.Layer.CreateAnonymous("implied-class-arc.usda")
    incoming.TransferContent(stage.GetRootLayer())
    incoming_stage = Usd.Stage.Open(incoming)
    incoming_intermediate = incoming_stage.GetPrimAtPath("/Intermediate")
    if field == "inheritPaths":
        incoming_intermediate.GetInherits().AddInherit("/Class")
    else:
        incoming_intermediate.GetSpecializes().AddSpecialize("/Class")
    event = _exact_spec_event(incoming, "/Intermediate", "prim", [field])

    projection = ComposedChangeProjection(stage, [event])
    apply_events(stage, [event])
    projected = projection.build_events()

    assert stage.GetPrimAtPath("/World/Child")
    assert any(item["k"] == "ensure_prim" and item["prim"] == "/World/Child" for item in projected)
    assert any(
        item["k"] == "set_gprim_attrs"
        and item["prim"] == "/World/Child"
        and item["attrs"]["radius"] == pytest.approx(6.0)
        for item in projected
    )
    assert not any(
        item.get("prim", "").startswith(("/Class", "/Intermediate")) for item in projected
    )


def test_whole_spec_removal_projects_delete_when_changed_fields_are_empty():
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/World/Thing", "Sphere")
    event = {
        "k": "set_sdf_spec_fields",
        "prim": "/World/Thing",
        "spec_path": "/World/Thing",
        "spec_kind": "prim",
        "fields": [],
        "fragment": "",
        "removed": True,
    }

    projection = ComposedChangeProjection(stage, [event])
    apply_events(stage, [event])
    projected = projection.build_events()

    assert not stage.GetPrimAtPath("/World/Thing")
    assert {item["prim"] for item in projected if item["k"] == "delete_prim"} == {"/World/Thing"}


def test_persistent_projection_state_reconciles_after_adapter_failure():
    stage = Usd.Stage.CreateInMemory()
    sphere = stage.DefinePrim("/World/Thing", "Sphere")
    sphere.GetAttribute("radius").Set(1.0)
    state = ComposedProjectionState(stage)

    with pytest.raises(RuntimeError, match="adapter failed"):
        with ComposedChangeProjection(stage, [], state=state) as projection:
            sphere.GetAttribute("radius").Set(2.0)
            assert any(item["k"] == "set_gprim_attrs" for item in projection.build_events())
            raise RuntimeError("adapter failed")

    assert state.needs_full_reconcile
    with ComposedChangeProjection(stage, [], state=state) as recovery:
        projected = recovery.build_events()
        assert any(
            item["k"] == "set_gprim_attrs"
            and item["prim"] == "/World/Thing"
            and item["attrs"]["radius"] == pytest.approx(2.0)
            for item in projected
        )
        recovery.commit()

    assert not state.needs_full_reconcile
    with ComposedChangeProjection(stage, [], state=state) as settled:
        assert settled.build_events() == []
        settled.commit()


def test_persistent_projection_noop_commit_keeps_baseline_object():
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/World/Thing", "Sphere")
    state = ComposedProjectionState(stage)
    baseline = state.baseline

    with ComposedChangeProjection(stage, [], state=state) as projection:
        assert projection.build_events() == []
        projection.commit()

    assert state.baseline is baseline


def test_persistent_projection_sparse_commit_replaces_only_affected_prim():
    stage = Usd.Stage.CreateInMemory()
    changed = stage.DefinePrim("/World/Changed", "Sphere")
    untouched = stage.DefinePrim("/World/Untouched", "Sphere")
    changed.GetAttribute("radius").Set(1.0)
    untouched.GetAttribute("radius").Set(3.0)
    state = ComposedProjectionState(stage)
    changed_before = state.baseline.prims["/World/Changed"]
    untouched_before = state.baseline.prims["/World/Untouched"]

    with ComposedChangeProjection(stage, [], state=state) as projection:
        changed.GetAttribute("radius").Set(2.0)
        assert any(item["k"] == "set_gprim_attrs" for item in projection.build_events())
        projection.commit()

    assert state.baseline.prims["/World/Changed"] is not changed_before
    assert state.baseline.prims["/World/Untouched"] is untouched_before


def test_unknown_event_kind_cannot_bypass_native_projection():
    with pytest.raises(ValueError, match="unknown event kinds"):
        ComposedChangeProjection(
            Usd.Stage.CreateInMemory(),
            [{"k": "future_specialized_edit", "prim": "/World/Thing"}],
        )


def test_layered_dispatcher_projects_composed_transforms_to_native_adapter():
    stage = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(
                _state(
                    1,
                    [
                        (_STRONG_LAYER, "Strong", False),
                        (_WEAK_LAYER, "Review Overrides", False),
                    ],
                )
            ),
            *_xform_events(1, _STRONG_LAYER, 2.0),
            *_xform_events(4, _WEAK_LAYER, 1.0),
        ]
    )
    adapter = MockAdapter()
    dispatcher = EventDispatcher(
        receiver=receiver,
        adapter=adapter,
        mirror_stage=stage,
    )

    assert dispatcher.drain_and_apply() == 3
    assert adapter.get_trs("/World/Thing")["t"] == pytest.approx([2.0, 0.0, 0.0])

    receiver.messages = [_clear_translate_event(7, _STRONG_LAYER)]
    assert dispatcher.drain_and_apply() == 2
    assert adapter.get_trs("/World/Thing")["t"] == pytest.approx([1.0, 0.0, 0.0])


def test_layered_dispatcher_does_not_apply_masked_native_edit():
    stage = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(
                _state(
                    1,
                    [
                        (_STRONG_LAYER, "Strong", False),
                        (_WEAK_LAYER, "Review Overrides", False),
                    ],
                )
            ),
            *_xform_events(1, _STRONG_LAYER, 2.0),
            *_xform_events(4, _WEAK_LAYER, 1.0),
        ]
    )
    adapter = MockAdapter()
    dispatcher = EventDispatcher(receiver=receiver, adapter=adapter, mirror_stage=stage)
    dispatcher.drain_and_apply()

    receiver.messages = [
        _event(
            7,
            _WEAK_LAYER,
            {
                "k": "set_xform_trs",
                "prim": "/World/Thing",
                "fields": ["t"],
                "t": [3.0, 0.0, 0.0],
            },
        )
    ]

    assert dispatcher.drain_and_apply() == 0
    assert adapter.get_trs("/World/Thing")["t"] == pytest.approx([2.0, 0.0, 0.0])
    weak = dispatcher.layer_router.layer_for(_WEAK_LAYER)
    assert weak.GetAttributeAtPath("/World/Thing.xformOp:translate").default == (
        3.0,
        0.0,
        0.0,
    )


def test_layered_dispatcher_corrects_masked_local_native_edit():
    stage = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(
                _state(
                    1,
                    [
                        (_STRONG_LAYER, "Strong", False),
                        (_WEAK_LAYER, "Review Overrides", False),
                    ],
                )
            ),
            *_xform_events(1, _STRONG_LAYER, 2.0),
            *_xform_events(4, _WEAK_LAYER, 1.0),
        ]
    )
    receiver.origin = "local-origin"
    adapter = MockAdapter()
    dispatcher = EventDispatcher(receiver=receiver, adapter=adapter, mirror_stage=stage)
    dispatcher.drain_and_apply()

    adapter.set_xform_trs("/World/Thing", t=[3.0, 0.0, 0.0])
    receiver.messages = [
        encode_message(
            {
                "type": "event",
                "seq": 7,
                "origin": "local-origin",
                "layer_key": _WEAK_LAYER,
                "event": {
                    "k": "set_xform_trs",
                    "prim": "/World/Thing",
                    "fields": ["t"],
                    "t": [3.0, 0.0, 0.0],
                },
            }
        )
    ]

    assert dispatcher.drain_and_apply() == 2
    assert adapter.get_trs("/World/Thing")["t"] == pytest.approx([2.0, 0.0, 0.0])
    weak = dispatcher.layer_router.layer_for(_WEAK_LAYER)
    assert weak.GetAttributeAtPath("/World/Thing.xformOp:translate").default == (
        3.0,
        0.0,
        0.0,
    )


def test_layered_dispatcher_does_not_rebuild_native_prim_for_local_api_edit():
    stage = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(
                _state(
                    1,
                    [
                        (_STRONG_LAYER, "Strong", False),
                        (_WEAK_LAYER, "Review Overrides", False),
                    ],
                )
            ),
            *_xform_events(1, _STRONG_LAYER, 2.0),
        ]
    )
    receiver.origin = "local-origin"
    adapter = MockAdapter()
    dispatcher = EventDispatcher(receiver=receiver, adapter=adapter, mirror_stage=stage)
    dispatcher.drain_and_apply()
    adapter.get_prim("/World/Thing")["sentinel"] = True

    receiver.messages = [
        encode_message(
            {
                "type": "event",
                "seq": 4,
                "origin": "local-origin",
                "layer_key": _WEAK_LAYER,
                "event": {
                    "k": "ensure_prim",
                    "prim": "/World/Thing",
                    "typeName": "",
                    "api_schemas": ["MaterialBindingAPI"],
                },
            }
        )
    ]

    dispatcher.drain_and_apply()

    native = adapter.get_prim("/World/Thing")
    assert native["sentinel"] is True
    assert "MaterialBindingAPI" in native["api_schemas"]


def test_local_reapplication_does_not_rebuild_unrelated_remote_lifecycle():
    stage = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(
                _state(
                    1,
                    [
                        (_STRONG_LAYER, "Strong", False),
                        (_WEAK_LAYER, "Review Overrides", False),
                    ],
                )
            ),
            *_xform_events(1, _STRONG_LAYER, 2.0, "/World/A"),
            *_xform_events(4, _STRONG_LAYER, 4.0, "/World/B"),
        ]
    )
    receiver.origin = "local-origin"
    adapter = MockAdapter()
    dispatcher = EventDispatcher(receiver=receiver, adapter=adapter, mirror_stage=stage)
    dispatcher.drain_and_apply()
    adapter.get_prim("/World/B")["sentinel"] = True

    receiver.messages = [
        encode_message(
            {
                "type": "event",
                "seq": 7,
                "origin": "local-origin",
                "layer_key": _WEAK_LAYER,
                "event": {
                    "k": "set_xform_trs",
                    "prim": "/World/A",
                    "fields": ["t"],
                    "t": [3.0, 0.0, 0.0],
                },
            }
        ),
        encode_message(
            {
                "type": "event",
                "seq": 8,
                "origin": "remote-origin",
                "layer_key": _STRONG_LAYER,
                "event": {
                    "k": "ensure_prim",
                    "prim": "/World/B",
                    "typeName": "Xform",
                },
            }
        ),
    ]

    dispatcher.drain_and_apply()

    assert adapter.get_trs("/World/A")["t"] == pytest.approx([2.0, 0.0, 0.0])
    assert adapter.get_prim("/World/B")["sentinel"] is True


def test_layered_dispatcher_removes_native_state_on_empty_resync():
    stage = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(_state(1, [(_STRONG_LAYER, "Strong", False)])),
            *_xform_events(1, _STRONG_LAYER, 2.0),
        ]
    )
    adapter = MockAdapter()
    dispatcher = EventDispatcher(receiver=receiver, adapter=adapter, mirror_stage=stage)
    dispatcher.drain_and_apply()
    assert adapter.get_prim("/World/Thing")

    receiver.messages = [
        encode_message({"type": "resync"}),
        encode_message(_state(1, [(_STRONG_LAYER, "Strong", False)])),
    ]
    dispatcher.drain_and_apply()

    assert adapter.get_prim("/World/Thing") == {}


def test_layered_dispatcher_reprojects_weaker_prim_after_definition_removal():
    stage = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(
                _state(
                    1,
                    [
                        (_STRONG_LAYER, "Strong", False),
                        (_WEAK_LAYER, "Review Overrides", False),
                    ],
                )
            ),
            *_xform_events(1, _STRONG_LAYER, 2.0),
            *_xform_events(4, _WEAK_LAYER, 1.0),
        ]
    )
    adapter = MockAdapter()
    dispatcher = EventDispatcher(receiver=receiver, adapter=adapter, mirror_stage=stage)
    dispatcher.drain_and_apply()

    receiver.messages = [
        _event(
            7,
            _STRONG_LAYER,
            {"k": "delete_prim", "prim": "/World/Thing"},
        )
    ]
    dispatcher.drain_and_apply()

    assert stage.GetPrimAtPath("/World/Thing")
    assert adapter.get_trs("/World/Thing")["t"] == pytest.approx([1.0, 0.0, 0.0])


@pytest.mark.parametrize(
    ("renamed_layer", "old_value", "new_value"),
    [
        (_STRONG_LAYER, 1.0, 2.0),
        (_WEAK_LAYER, 2.0, 1.0),
    ],
)
def test_layered_dispatcher_projects_composed_rename(
    renamed_layer,
    old_value,
    new_value,
):
    stage = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(
                _state(
                    1,
                    [
                        (_STRONG_LAYER, "Strong", False),
                        (_WEAK_LAYER, "Review Overrides", False),
                    ],
                )
            ),
            *_xform_events(1, _STRONG_LAYER, 2.0),
            *_xform_events(4, _WEAK_LAYER, 1.0),
        ]
    )
    adapter = MockAdapter()
    dispatcher = EventDispatcher(receiver=receiver, adapter=adapter, mirror_stage=stage)
    dispatcher.drain_and_apply()

    receiver.messages = [
        _event(
            7,
            renamed_layer,
            {
                "k": "rename_prim",
                "prim": "/World/Thing",
                "new_name": "Moved",
            },
        )
    ]
    dispatcher.drain_and_apply()

    assert adapter.get_trs("/World/Thing")["t"] == pytest.approx([old_value, 0.0, 0.0])
    assert adapter.get_trs("/World/Moved")["t"] == pytest.approx([new_value, 0.0, 0.0])


def test_layered_dispatcher_preserves_native_rename_when_composed_prim_moves():
    stage = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(_state(1, [(_STRONG_LAYER, "Strong", False)])),
            *_xform_events(1, _STRONG_LAYER, 2.0),
        ]
    )
    adapter = MockAdapter()
    dispatcher = EventDispatcher(receiver=receiver, adapter=adapter, mirror_stage=stage)
    dispatcher.drain_and_apply()

    receiver.messages = [
        _event(
            4,
            _STRONG_LAYER,
            {
                "k": "rename_prim",
                "prim": "/World/Thing",
                "new_name": "Moved",
            },
        )
    ]
    dispatcher.drain_and_apply()

    assert adapter.get_prim("/World/Thing") == {}
    assert adapter.get_trs("/World/Moved")["t"] == pytest.approx([2.0, 0.0, 0.0])


def test_layered_dispatcher_rebuilds_native_prim_after_composed_type_change():
    stage = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(
                _state(
                    1,
                    [
                        (_STRONG_LAYER, "Strong", False),
                        (_WEAK_LAYER, "Review Overrides", False),
                    ],
                )
            ),
            _event(
                1,
                _STRONG_LAYER,
                {"k": "ensure_prim", "prim": "/World/Thing", "typeName": "Cube"},
            ),
            _event(
                2,
                _WEAK_LAYER,
                {"k": "ensure_prim", "prim": "/World/Thing", "typeName": "Sphere"},
            ),
        ]
    )
    adapter = MockAdapter()
    dispatcher = EventDispatcher(receiver=receiver, adapter=adapter, mirror_stage=stage)
    dispatcher.drain_and_apply()
    assert adapter.get_prim("/World/Thing")["typeName"] == "Cube"

    receiver.messages = [
        encode_message(
            _state(
                2,
                [
                    (_WEAK_LAYER, "Review Overrides", False),
                    (_STRONG_LAYER, "Strong", False),
                ],
            )
        )
    ]
    dispatcher.drain_and_apply()

    assert adapter.get_prim("/World/Thing")["typeName"] == "Sphere"


def test_layered_dispatcher_projects_reorder_and_mute_without_event_replay():
    stage = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(
                _state(
                    1,
                    [
                        (_STRONG_LAYER, "Strong", False),
                        (_WEAK_LAYER, "Review Overrides", False),
                    ],
                )
            ),
            *_xform_events(1, _STRONG_LAYER, 2.0),
            *_xform_events(4, _WEAK_LAYER, 1.0),
        ]
    )
    adapter = MockAdapter()
    dispatcher = EventDispatcher(
        receiver=receiver,
        adapter=adapter,
        mirror_stage=stage,
    )
    dispatcher.drain_and_apply()

    receiver.messages = [
        encode_message(
            _state(
                2,
                [
                    (_WEAK_LAYER, "Review Overrides", False),
                    (_STRONG_LAYER, "Strong", False),
                ],
            )
        )
    ]
    assert dispatcher.drain_and_apply() > 0
    assert adapter.get_trs("/World/Thing")["t"] == pytest.approx([1.0, 0.0, 0.0])

    receiver.messages = [
        encode_message(
            _state(
                3,
                [
                    (_WEAK_LAYER, "Review Overrides", True),
                    (_STRONG_LAYER, "Strong", False),
                ],
            )
        )
    ]
    assert dispatcher.drain_and_apply() > 0
    assert adapter.get_trs("/World/Thing")["t"] == pytest.approx([2.0, 0.0, 0.0])


def test_layered_dispatcher_reprojects_transform_samples_after_stack_changes():
    stage = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(
                _state(
                    1,
                    [
                        (_STRONG_LAYER, "Strong", False),
                        (_WEAK_LAYER, "Review Overrides", False),
                    ],
                )
            ),
            _event(
                1,
                _STRONG_LAYER,
                {"k": "ensure_prim", "prim": "/World/Thing", "typeName": "Xform"},
            ),
            _event(
                2,
                _STRONG_LAYER,
                {"k": "ensure_xform_ops", "prim": "/World/Thing"},
            ),
            _event(
                3,
                _STRONG_LAYER,
                {
                    "k": "set_xform_trs",
                    "prim": "/World/Thing",
                    "fields": ["t"],
                    "t": [2.0, 0.0, 0.0],
                    "time": 1.0,
                },
            ),
            _event(
                4,
                _WEAK_LAYER,
                {"k": "ensure_xform_ops", "prim": "/World/Thing"},
            ),
            _event(
                5,
                _WEAK_LAYER,
                {
                    "k": "set_xform_trs",
                    "prim": "/World/Thing",
                    "fields": ["t"],
                    "t": [1.0, 0.0, 0.0],
                    "time": 1.0,
                },
            ),
        ]
    )
    adapter = MockAdapter()
    dispatcher = EventDispatcher(receiver=receiver, adapter=adapter, mirror_stage=stage)
    dispatcher.drain_and_apply()
    assert adapter.get_prim("/World/Thing")["trs_samples"][1.0]["t"] == pytest.approx(
        [2.0, 0.0, 0.0]
    )

    receiver.messages = [
        encode_message(
            _state(
                2,
                [
                    (_WEAK_LAYER, "Review Overrides", False),
                    (_STRONG_LAYER, "Strong", False),
                ],
            )
        )
    ]
    dispatcher.drain_and_apply()
    assert adapter.get_prim("/World/Thing")["trs_samples"][1.0]["t"] == pytest.approx(
        [1.0, 0.0, 0.0]
    )


def test_layered_dispatcher_projects_composed_gprim_values_to_native_adapter():
    stage = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(
                _state(
                    1,
                    [
                        (_STRONG_LAYER, "Strong", False),
                        (_WEAK_LAYER, "Review Overrides", False),
                    ],
                )
            ),
            _event(
                1,
                _STRONG_LAYER,
                {"k": "ensure_prim", "prim": "/World/Thing", "typeName": "Sphere"},
            ),
            _event(
                2,
                _STRONG_LAYER,
                {
                    "k": "set_gprim_attrs",
                    "prim": "/World/Thing",
                    "attrs": {"radius": 2.0},
                },
            ),
            _event(
                3,
                _WEAK_LAYER,
                {
                    "k": "set_gprim_attrs",
                    "prim": "/World/Thing",
                    "attrs": {"radius": 1.0},
                },
            ),
        ]
    )
    adapter = MockAdapter()
    dispatcher = EventDispatcher(
        receiver=receiver,
        adapter=adapter,
        mirror_stage=stage,
    )

    dispatcher.drain_and_apply()
    assert adapter.get_prim("/World/Thing")["gprim_attrs"]["radius"] == 2.0

    receiver.messages = [_clear_property_event(4, _STRONG_LAYER, "radius")]
    dispatcher.drain_and_apply()
    assert adapter.get_prim("/World/Thing")["gprim_attrs"]["radius"] == 1.0


def test_layered_dispatcher_reprojects_gprim_values_after_stack_changes():
    stage = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(
                _state(
                    1,
                    [
                        (_STRONG_LAYER, "Strong", False),
                        (_WEAK_LAYER, "Review Overrides", False),
                    ],
                )
            ),
            _event(
                1,
                _STRONG_LAYER,
                {"k": "ensure_prim", "prim": "/World/Thing", "typeName": "Sphere"},
            ),
            _event(
                2,
                _STRONG_LAYER,
                {
                    "k": "set_gprim_attrs",
                    "prim": "/World/Thing",
                    "attrs": {"radius": 2.0},
                },
            ),
            _event(
                3,
                _WEAK_LAYER,
                {
                    "k": "set_gprim_attrs",
                    "prim": "/World/Thing",
                    "attrs": {"radius": 1.0},
                },
            ),
        ]
    )
    adapter = MockAdapter()
    dispatcher = EventDispatcher(receiver=receiver, adapter=adapter, mirror_stage=stage)
    dispatcher.drain_and_apply()

    receiver.messages = [
        encode_message(
            _state(
                2,
                [
                    (_WEAK_LAYER, "Review Overrides", False),
                    (_STRONG_LAYER, "Strong", False),
                ],
            )
        )
    ]
    assert dispatcher.drain_and_apply() > 0
    assert adapter.get_prim("/World/Thing")["gprim_attrs"]["radius"] == 1.0

    receiver.messages = [
        encode_message(
            _state(
                3,
                [
                    (_WEAK_LAYER, "Review Overrides", True),
                    (_STRONG_LAYER, "Strong", False),
                ],
            )
        )
    ]
    assert dispatcher.drain_and_apply() > 0
    assert adapter.get_prim("/World/Thing")["gprim_attrs"]["radius"] == 2.0


def test_layered_dispatcher_reprojects_composed_references_after_stack_changes():
    asset_a = Sdf.Layer.CreateAnonymous("asset-a")
    asset_b = Sdf.Layer.CreateAnonymous("asset-b")
    for asset in (asset_a, asset_b):
        asset_stage = Usd.Stage.Open(asset)
        asset_stage.DefinePrim("/Asset", "Xform")

    stage = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(
                _state(
                    1,
                    [
                        (_STRONG_LAYER, "Strong", False),
                        (_WEAK_LAYER, "Review Overrides", False),
                    ],
                )
            ),
            _event(
                1,
                _STRONG_LAYER,
                {"k": "ensure_prim", "prim": "/World/Thing", "typeName": "Xform"},
            ),
            _event(
                2,
                _STRONG_LAYER,
                {
                    "k": "set_reference",
                    "prim": "/World/Thing",
                    "refs": [{"asset_path": asset_a.identifier, "prim_path": "/Asset"}],
                    "list_op_authored": True,
                    "list_op_explicit": True,
                },
            ),
            _event(
                3,
                _WEAK_LAYER,
                {"k": "ensure_prim", "prim": "/World/Thing", "typeName": "Xform"},
            ),
            _event(
                4,
                _WEAK_LAYER,
                {
                    "k": "set_reference",
                    "prim": "/World/Thing",
                    "refs": [{"asset_path": asset_b.identifier, "prim_path": "/Asset"}],
                    "list_op_authored": True,
                    "list_op_explicit": True,
                },
            ),
        ]
    )
    adapter = MockAdapter()
    dispatcher = EventDispatcher(receiver=receiver, adapter=adapter, mirror_stage=stage)
    dispatcher.drain_and_apply()
    assert adapter.get_prim("/World/Thing")["references"][0]["asset_path"] == (asset_a.identifier)

    receiver.messages = [
        encode_message(
            _state(
                2,
                [
                    (_WEAK_LAYER, "Review Overrides", False),
                    (_STRONG_LAYER, "Strong", False),
                ],
            )
        )
    ]
    dispatcher.drain_and_apply()

    assert adapter.get_prim("/World/Thing")["references"][0]["asset_path"] == (asset_b.identifier)


def test_layered_dispatcher_projects_composed_point_instancer_values():
    stage = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(
                _state(
                    1,
                    [
                        (_STRONG_LAYER, "Strong", False),
                        (_WEAK_LAYER, "Review Overrides", False),
                    ],
                )
            ),
            _event(
                1,
                _STRONG_LAYER,
                {
                    "k": "ensure_prim",
                    "prim": "/World/Thing",
                    "typeName": "PointInstancer",
                },
            ),
            _event(
                2,
                _STRONG_LAYER,
                {
                    "k": "set_point_instancer",
                    "prim": "/World/Thing",
                    "fields": ["positions"],
                    "positions": [[2.0, 0.0, 0.0]],
                },
            ),
            _event(
                3,
                _WEAK_LAYER,
                {
                    "k": "set_point_instancer",
                    "prim": "/World/Thing",
                    "fields": ["positions"],
                    "positions": [[1.0, 0.0, 0.0]],
                },
            ),
        ]
    )
    adapter = MockAdapter()
    dispatcher = EventDispatcher(receiver=receiver, adapter=adapter, mirror_stage=stage)

    dispatcher.drain_and_apply()
    assert adapter.get_prim("/World/Thing")["point_instancer"]["positions"].tolist() == [
        [2.0, 0.0, 0.0]
    ]

    receiver.messages = [_clear_property_event(4, _STRONG_LAYER, "positions")]
    dispatcher.drain_and_apply()
    assert adapter.get_prim("/World/Thing")["point_instancer"]["positions"].tolist() == [
        [1.0, 0.0, 0.0]
    ]


def test_layered_dispatcher_projects_composed_prim_and_imageable_state():
    stage = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(
                _state(
                    1,
                    [
                        (_STRONG_LAYER, "Strong", False),
                        (_WEAK_LAYER, "Review Overrides", False),
                    ],
                )
            ),
            _event(
                1,
                _STRONG_LAYER,
                {"k": "ensure_prim", "prim": "/World/Thing", "typeName": "Sphere"},
            ),
            _event(
                2,
                _STRONG_LAYER,
                {"k": "set_visibility", "prim": "/World/Thing", "visible": False},
            ),
            _event(
                3,
                _WEAK_LAYER,
                {"k": "set_visibility", "prim": "/World/Thing", "visible": True},
            ),
            _event(
                4,
                _STRONG_LAYER,
                {"k": "set_instanceable", "prim": "/World/Thing", "instanceable": True},
            ),
            _event(
                5,
                _WEAK_LAYER,
                {"k": "set_instanceable", "prim": "/World/Thing", "instanceable": False},
            ),
        ]
    )
    adapter = MockAdapter()
    dispatcher = EventDispatcher(receiver=receiver, adapter=adapter, mirror_stage=stage)

    dispatcher.drain_and_apply()
    assert adapter.get_prim("/World/Thing")["visible"] is False
    assert adapter.get_prim("/World/Thing")["instanceable"] is True

    receiver.messages = [
        _clear_property_event(6, _STRONG_LAYER, "visibility"),
        _clear_prim_field_event(7, _STRONG_LAYER, "instanceable"),
    ]
    dispatcher.drain_and_apply()
    assert adapter.get_prim("/World/Thing")["visible"] is True
    assert adapter.get_prim("/World/Thing")["instanceable"] is False

    receiver.messages = [
        _event(
            8,
            _STRONG_LAYER,
            {"k": "deactivate_prim", "prim": "/World/Thing", "active": False},
        ),
        _event(
            9,
            _WEAK_LAYER,
            {"k": "deactivate_prim", "prim": "/World/Thing", "active": True},
        ),
    ]
    dispatcher.drain_and_apply()
    assert adapter.get_prim("/World/Thing")["active"] is False

    receiver.messages = [_clear_prim_field_event(10, _STRONG_LAYER, "active")]
    dispatcher.drain_and_apply()
    assert adapter.get_prim("/World/Thing")["active"] is True


def test_layered_dispatcher_projects_composed_variant_selection_to_native_adapter():
    stage = Usd.Stage.CreateInMemory()
    prim = stage.DefinePrim("/World/Thing", "Xform")
    variants = prim.GetVariantSets().AddVariantSet("model")
    variants.AddVariant("A")
    variants.AddVariant("B")
    variants.SetVariantSelection("A")

    receiver = _LayeredQueue(
        [
            encode_message(
                _state(
                    1,
                    [
                        (_STRONG_LAYER, "Strong", False),
                        (_WEAK_LAYER, "Review Overrides", False),
                    ],
                )
            ),
            _event(
                1,
                _STRONG_LAYER,
                {
                    "k": "set_variant_selections",
                    "prim": "/World/Thing",
                    "selections": {"model": "B"},
                },
            ),
            _event(
                2,
                _WEAK_LAYER,
                {
                    "k": "set_variant_selections",
                    "prim": "/World/Thing",
                    "selections": {"model": "A"},
                },
            ),
        ]
    )
    adapter = MockAdapter()
    dispatcher = EventDispatcher(
        receiver=receiver,
        adapter=adapter,
        mirror_stage=stage,
    )

    dispatcher.drain_and_apply()
    assert adapter.get_prim("/World/Thing")["variant_selections"] == {"model": "B"}

    receiver.messages = [
        _event(
            3,
            _STRONG_LAYER,
            {
                "k": "set_variant_selections",
                "prim": "/World/Thing",
                "selections": {"model": ""},
            },
        )
    ]
    dispatcher.drain_and_apply()
    assert adapter.get_prim("/World/Thing")["variant_selections"] == {"model": "A"}


def test_layered_dispatcher_projects_composed_material_binding_to_native_adapter():
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/World/Thing", "Sphere")
    stage.DefinePrim("/World/Looks/Strong", "Material")
    stage.DefinePrim("/World/Looks/Weak", "Material")
    receiver = _LayeredQueue(
        [
            encode_message(
                _state(
                    1,
                    [
                        (_STRONG_LAYER, "Strong", False),
                        (_WEAK_LAYER, "Review Overrides", False),
                    ],
                )
            ),
            _event(
                1,
                _STRONG_LAYER,
                {
                    "k": "set_material_binding",
                    "prim": "/World/Thing",
                    "material_path": "/World/Looks/Strong",
                },
            ),
            _event(
                2,
                _WEAK_LAYER,
                {
                    "k": "set_material_binding",
                    "prim": "/World/Thing",
                    "material_path": "/World/Looks/Weak",
                },
            ),
        ]
    )
    adapter = MockAdapter()
    dispatcher = EventDispatcher(
        receiver=receiver,
        adapter=adapter,
        mirror_stage=stage,
    )

    dispatcher.drain_and_apply()
    assert adapter.get_prim("/World/Thing")["material_bindings"][""] == ("/World/Looks/Strong")

    receiver.messages = [
        encode_message(
            _state(
                2,
                [
                    (_WEAK_LAYER, "Review Overrides", False),
                    (_STRONG_LAYER, "Strong", False),
                ],
            )
        )
    ]
    dispatcher.drain_and_apply()
    assert adapter.get_prim("/World/Thing")["material_bindings"][""] == ("/World/Looks/Weak")


def test_layered_dispatcher_projects_arcs_before_dependent_native_edits(tmp_path):
    asset_path = tmp_path / "asset.usda"
    asset_stage = Usd.Stage.CreateNew(str(asset_path))
    asset_stage.DefinePrim("/Asset", "Xform")
    asset_stage.GetRootLayer().defaultPrim = "Asset"
    asset_stage.GetRootLayer().Save()

    class RecordingAdapter(MockAdapter):
        def set_reference(self, prim_path, refs, **kwargs):
            self.calls.append(("set_reference", prim_path))
            return super().set_reference(prim_path, refs, **kwargs)

        def set_material_binding(self, prim_path, material_path, material_purpose=""):
            self.calls.append(("set_material_binding", prim_path))
            return super().set_material_binding(
                prim_path,
                material_path,
                material_purpose,
            )

    stage = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(_state(1, [(_BASE_LAYER, "Base", False)])),
            _event(
                1,
                _BASE_LAYER,
                {"k": "ensure_prim", "prim": "/World/Asset", "typeName": "Xform"},
            ),
            _event(
                2,
                _BASE_LAYER,
                {
                    "k": "set_reference",
                    "prim": "/World/Asset",
                    "refs": [{"asset_path": str(asset_path), "prim_path": "/Asset"}],
                },
            ),
            _event(
                3,
                _BASE_LAYER,
                {
                    "k": "set_material_binding",
                    "prim": "/World/Asset",
                    "material_path": "/World/Looks/Material",
                },
            ),
        ]
    )
    adapter = RecordingAdapter()
    dispatcher = EventDispatcher(receiver=receiver, adapter=adapter, mirror_stage=stage)

    dispatcher.drain_and_apply()

    assert adapter.calls == [
        ("set_reference", "/World/Asset"),
        ("set_material_binding", "/World/Asset"),
    ]


def test_layered_dispatcher_projects_composed_shader_input_to_native_adapter():
    stage = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(
                _state(
                    1,
                    [
                        (_STRONG_LAYER, "Strong", False),
                        (_WEAK_LAYER, "Review Overrides", False),
                    ],
                )
            ),
            _event(
                1,
                _STRONG_LAYER,
                {"k": "ensure_prim", "prim": "/World/Shader", "typeName": "Shader"},
            ),
            _event(
                2,
                _STRONG_LAYER,
                {
                    "k": "set_connectable_input",
                    "prim": "/World/Shader",
                    "info_id": "UsdPreviewSurface",
                    "inputs": {"roughness": 0.2},
                    "input_types": {"roughness": "float"},
                },
            ),
            _event(
                3,
                _WEAK_LAYER,
                {
                    "k": "set_connectable_input",
                    "prim": "/World/Shader",
                    "info_id": "UsdPreviewSurface",
                    "inputs": {"roughness": 0.8},
                    "input_types": {"roughness": "float"},
                },
            ),
        ]
    )
    adapter = MockAdapter()
    dispatcher = EventDispatcher(
        receiver=receiver,
        adapter=adapter,
        mirror_stage=stage,
    )

    dispatcher.drain_and_apply()
    assert adapter.get_prim("/World/Shader")["connectable_inputs"]["roughness"] == pytest.approx(
        0.2
    )

    receiver.messages = [
        _event(
            4,
            _STRONG_LAYER,
            {
                "k": "set_sdf_spec_fields",
                "prim": "/World/Shader",
                "spec_path": "/World/Shader.inputs:roughness",
                "spec_kind": "attribute",
                "fields": ["default"],
                "fragment": "",
                "removed": True,
            },
        )
    ]
    dispatcher.drain_and_apply()
    assert adapter.get_prim("/World/Shader")["connectable_inputs"]["roughness"] == pytest.approx(
        0.8
    )


def test_layered_dispatcher_preserves_connected_input_fallback_in_native_adapter():
    stage = Usd.Stage.CreateInMemory()
    shader = UsdShade.Shader.Define(stage, "/World/Shader")
    shader.CreateIdAttr("UsdPreviewSurface")
    roughness = shader.CreateInput("roughness", Sdf.ValueTypeNames.Float)
    roughness.Set(0.5)
    texture = UsdShade.Shader.Define(stage, "/World/Texture")
    texture.CreateIdAttr("UsdUVTexture")
    roughness.ConnectToSource(texture.CreateOutput("g", Sdf.ValueTypeNames.Float))

    receiver = _LayeredQueue(
        [
            encode_message(_state(1, [(_BASE_LAYER, "Base", False)])),
            _event(
                1,
                _BASE_LAYER,
                {
                    "k": "set_connectable_input",
                    "prim": "/World/Shader",
                    "info_id": "UsdPreviewSurface",
                    "inputs": {"roughness": 0.3},
                    "input_types": {"roughness": "float"},
                },
            ),
        ]
    )
    adapter = MockAdapter()
    dispatcher = EventDispatcher(receiver=receiver, adapter=adapter, mirror_stage=stage)

    dispatcher.drain_and_apply()

    assert adapter.get_prim("/World/Shader")["connectable_inputs"]["roughness"] == pytest.approx(
        0.3
    )


def test_layered_dispatcher_projects_connectable_values_before_connections():
    class RecordingAdapter(MockAdapter):
        def __init__(self):
            super().__init__()
            self.shader_calls = []

        def set_connectable_input(
            self,
            prim_path,
            info_id,
            inputs,
            input_types,
            time=None,
        ):
            self.shader_calls.append(("input", prim_path))
            return super().set_connectable_input(
                prim_path,
                info_id,
                inputs,
                input_types,
                time,
            )

        def set_connectable_connection(
            self,
            prim_path,
            connections,
            disconnections=None,
        ):
            self.shader_calls.append(("connection", prim_path))
            return super().set_connectable_connection(
                prim_path,
                connections,
                disconnections,
            )

    stage = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(_state(1, [(_BASE_LAYER, "Base", False)])),
            _event(
                1,
                _BASE_LAYER,
                {"k": "ensure_prim", "prim": "/World/Consumer", "typeName": "Shader"},
            ),
            _event(
                2,
                _BASE_LAYER,
                {"k": "ensure_prim", "prim": "/World/Source", "typeName": "Shader"},
            ),
            _event(
                3,
                _BASE_LAYER,
                {
                    "k": "set_connectable_connection",
                    "prim": "/World/Consumer",
                    "connections": {
                        "inputs:roughness": {
                            "source_prim": "/World/Source",
                            "source_attr": "outputs:r",
                        }
                    },
                    "disconnections": [],
                },
            ),
            _event(
                4,
                _BASE_LAYER,
                {
                    "k": "set_connectable_input",
                    "prim": "/World/Source",
                    "info_id": "UsdUVTexture",
                    "inputs": {"file": "texture.exr"},
                    "input_types": {"file": "asset"},
                },
            ),
        ]
    )
    adapter = RecordingAdapter()
    dispatcher = EventDispatcher(receiver=receiver, adapter=adapter, mirror_stage=stage)

    dispatcher.drain_and_apply()

    assert adapter.shader_calls == [
        ("input", "/World/Source"),
        ("connection", "/World/Consumer"),
    ]
