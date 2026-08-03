"""Receiver-local logical collaboration layer reconstruction."""

from __future__ import annotations

import pytest
from pxr import Sdf, Usd, UsdGeom

from openusdconnect.adapters import UsdStageAdapter
from openusdconnect.codec import encode_message
from openusdconnect.dispatcher import EventDispatcher
from openusdconnect.logical_layers import LogicalLayerRouter
from openusdconnect.protocol_constants import (
    K_LOAD_PAYLOAD,
    K_SET_STAGE_METADATA,
    K_UNLOAD_PAYLOAD,
)

from .layered_replay_test_support import (
    _BASE_LAYER,
    _STRONG_LAYER,
    _WEAK_LAYER,
    _LayeredQueue,
    _property_event,
    _state,
)


def _set_int(
    stage: Usd.Stage,
    router: LogicalLayerRouter,
    layer_key: str,
    value: int,
):
    with Usd.EditContext(stage, router.edit_target_for(layer_key)):
        prim = stage.OverridePrim("/World/Thing")
        prim.CreateAttribute(
            "userProperties:value",
            Sdf.ValueTypeNames.Int,
        ).Set(value)


def test_router_preserves_unrelated_session_layers_and_composes_strength():
    stage = Usd.Stage.CreateInMemory()
    unrelated = Sdf.Layer.CreateAnonymous("unrelated")
    session = stage.GetSessionLayer()
    session.subLayerPaths.append(unrelated.identifier)
    session.subLayerOffsets[0] = Sdf.LayerOffset(7, 2)
    router = LogicalLayerRouter(stage)

    router.apply_state(
        _state(
            1,
            [
                (_STRONG_LAYER, "Strong", False),
                (_WEAK_LAYER, "Review Overrides", False),
                (_BASE_LAYER, "Base", False),
            ],
        )
    )
    _set_int(stage, router, _STRONG_LAYER, 2)
    _set_int(stage, router, _WEAK_LAYER, 1)
    _set_int(stage, router, _BASE_LAYER, 0)

    managed = [Sdf.Layer.Find(identifier) for identifier in session.subLayerPaths[:3]]
    assert managed == [
        router.layer_for(_STRONG_LAYER),
        router.layer_for(_WEAK_LAYER),
        router.layer_for(_BASE_LAYER),
    ]
    assert session.subLayerPaths[3] == unrelated.identifier
    assert session.subLayerOffsets[3] == Sdf.LayerOffset(7, 2)
    assert stage.GetAttributeAtPath("/World/Thing.userProperties:value").Get() == 2

    router.apply_state(
        _state(
            2,
            [
                (_WEAK_LAYER, "Review Overrides", False),
                (_STRONG_LAYER, "Strong", False),
                (_BASE_LAYER, "Base", False),
            ],
        )
    )
    assert stage.GetAttributeAtPath("/World/Thing.userProperties:value").Get() == 1

    router.apply_state(
        _state(
            3,
            [
                (_WEAK_LAYER, "Review Overrides", True),
                (_STRONG_LAYER, "Strong", False),
                (_BASE_LAYER, "Base", False),
            ],
        )
    )
    assert stage.GetAttributeAtPath("/World/Thing.userProperties:value").Get() == 2

    with router.writable([router.layer_for(_WEAK_LAYER)]):
        _set_int(stage, router, _WEAK_LAYER, 3)
    assert (
        router.layer_for(_WEAK_LAYER)
        .GetAttributeAtPath("/World/Thing.userProperties:value")
        .default
        == 3
    )
    assert stage.GetAttributeAtPath("/World/Thing.userProperties:value").Get() == 2

    router.apply_state(
        _state(
            4,
            [
                (_WEAK_LAYER, "Review Overrides", False),
                (_STRONG_LAYER, "Strong", False),
                (_BASE_LAYER, "Base", False),
            ],
        )
    )
    assert stage.GetAttributeAtPath("/World/Thing.userProperties:value").Get() == 3

    router.apply_state(
        _state(
            5,
            [
                (_WEAK_LAYER, "Review Overrides", False),
                (_STRONG_LAYER, "Strong", False),
                (_BASE_LAYER, "Base", True),
            ],
        )
    )
    assert stage.IsLayerMuted(router.layer_for(_BASE_LAYER).identifier)

    router.close()
    assert list(session.subLayerPaths) == [unrelated.identifier]
    assert list(session.subLayerOffsets) == [Sdf.LayerOffset(7, 2)]


def test_router_revisions_are_scoped_to_server_generation():
    router = LogicalLayerRouter(Usd.Stage.CreateInMemory())
    assert router.apply_state(_state(4, [(_STRONG_LAYER, "Strong", False)]))
    assert not router.apply_state(_state(3, [(_WEAK_LAYER, "Review Overrides", False)]))
    assert router.layer_keys == (_STRONG_LAYER,)

    assert router.apply_state(
        _state(
            1,
            [(_WEAK_LAYER, "Review Overrides", False)],
            generation="server-b",
        )
    )
    assert router.layer_keys == (_WEAK_LAYER,)


def test_router_rebinds_owned_layers_without_leaking_into_old_stage():
    first = Usd.Stage.CreateInMemory()
    second = Usd.Stage.CreateInMemory()
    router = LogicalLayerRouter(first)
    router.apply_state(_state(1, [(_STRONG_LAYER, "Strong", False)]))
    _set_int(first, router, _STRONG_LAYER, 7)

    router.bind(second)

    managed_id = router.layer_for(_STRONG_LAYER).identifier
    assert managed_id not in first.GetSessionLayer().subLayerPaths
    assert managed_id in second.GetSessionLayer().subLayerPaths
    assert not first.GetAttributeAtPath("/World/Thing.userProperties:value")
    assert second.GetAttributeAtPath("/World/Thing.userProperties:value").Get() == 7


def test_removed_layer_key_does_not_restore_stale_opinions_when_reused():
    stage = Usd.Stage.CreateInMemory()
    router = LogicalLayerRouter(stage)
    router.apply_state(_state(1, [(_STRONG_LAYER, "Strong", False)]))
    original = router.layer_for(_STRONG_LAYER)
    _set_int(stage, router, _STRONG_LAYER, 7)

    router.apply_state(_state(2, [(_BASE_LAYER, "Base", False)]))
    assert original.identifier not in stage.GetSessionLayer().subLayerPaths
    assert not stage.GetAttributeAtPath("/World/Thing.userProperties:value")

    router.apply_state(
        _state(
            3,
            [
                (_STRONG_LAYER, "Strong", False),
                (_BASE_LAYER, "Base", False),
            ],
        )
    )
    replacement = router.layer_for(_STRONG_LAYER)
    assert replacement is not original
    assert not replacement.GetPropertyAtPath("/World/Thing.userProperties:value")


def test_dispatcher_routes_authored_records_and_clears_layers_on_resync():
    stage = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(
                _state(
                    1,
                    [
                        (_STRONG_LAYER, "Strong", False),
                        (_WEAK_LAYER, "Review Overrides", False),
                        (_BASE_LAYER, "Base", False),
                    ],
                )
            ),
            _property_event(1, _STRONG_LAYER, 2),
            _property_event(2, _WEAK_LAYER, 1),
        ]
    )
    dispatcher = EventDispatcher(
        receiver=receiver,
        adapter=UsdStageAdapter(stage),
    )

    assert dispatcher.drain_and_apply() == 2
    assert stage.GetAttributeAtPath("/World/Thing.userProperties:value").Get() == 2

    receiver.messages = [
        encode_message({"type": "resync"}),
        encode_message(
            _state(
                1,
                [
                    (_STRONG_LAYER, "Strong", False),
                    (_WEAK_LAYER, "Review Overrides", False),
                    (_BASE_LAYER, "Base", False),
                ],
            )
        ),
        _property_event(1, _WEAK_LAYER, 5),
    ]
    assert dispatcher.drain_and_apply() == 1
    assert dispatcher.last_seq == 1
    assert stage.GetAttributeAtPath("/World/Thing.userProperties:value").Get() == 5
    assert not dispatcher.layer_router.layer_for(_STRONG_LAYER).GetPropertyAtPath(
        "/World/Thing.userProperties:value"
    )


def test_dispatcher_rejects_layer_opinion_without_collaboration_layer_key():
    stage = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(
                _state(
                    1,
                    [(_BASE_LAYER, "Base", False)],
                )
            ),
            encode_message(
                {
                    "type": "event",
                    "seq": 1,
                    "event": {
                        "k": "ensure_prim",
                        "prim": "/World/Thing",
                        "typeName": "Xform",
                    },
                }
            ),
        ]
    )
    dispatcher = EventDispatcher(
        receiver=receiver,
        adapter=UsdStageAdapter(stage),
    )

    with pytest.raises(ValueError, match="collaboration layer key"):
        dispatcher.drain_and_apply()


def test_dispatcher_keeps_stage_runtime_events_outside_logical_layers():
    class RecordingAdapter(UsdStageAdapter):
        def __init__(self, stage):
            super().__init__(stage)
            self.targets = []

        def apply_events(self, events):
            self.targets.append(
                (
                    self.stage.GetEditTarget().GetLayer().identifier,
                    [event["k"] for event in events],
                )
            )
            return super().apply_events(events)

    stage = Usd.Stage.CreateInMemory()
    session_id = stage.GetSessionLayer().identifier
    receiver = _LayeredQueue(
        [
            encode_message(
                _state(
                    1,
                    [
                        (_STRONG_LAYER, "Strong", False),
                        (_BASE_LAYER, "Base", False),
                    ],
                )
            ),
            _property_event(1, _STRONG_LAYER, 2),
            encode_message(
                {
                    "type": "event",
                    "seq": 2,
                    "event": {
                        "k": K_LOAD_PAYLOAD,
                        "prim": "/World/Thing",
                    },
                }
            ),
            encode_message(
                {
                    "type": "event",
                    "seq": 3,
                    "event": {
                        "k": K_SET_STAGE_METADATA,
                        "upAxis": "Z",
                    },
                }
            ),
        ]
    )
    adapter = RecordingAdapter(stage)
    dispatcher = EventDispatcher(receiver=receiver, adapter=adapter)

    assert dispatcher.drain_and_apply() == 3
    logical_layer_identifier = dispatcher.layer_router.layer_for(_STRONG_LAYER).identifier
    assert adapter.targets == [
        (logical_layer_identifier, ["set_sdf_spec_fields"]),
        (session_id, [K_LOAD_PAYLOAD, K_SET_STAGE_METADATA]),
    ]
    assert stage.GetSessionLayer().pseudoRoot.GetInfo("upAxis") == "Z"


def test_dispatcher_moves_shared_stage_metadata_on_rebind_and_close():
    first = Usd.Stage.CreateInMemory()
    second = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(_state(1, [(_BASE_LAYER, "Base", False)])),
            encode_message(
                {
                    "type": "event",
                    "seq": 1,
                    "event": {
                        "k": K_SET_STAGE_METADATA,
                        "upAxis": "Z",
                    },
                }
            ),
        ]
    )
    dispatcher = EventDispatcher(
        receiver=receiver,
        adapter=UsdStageAdapter(first),
    )

    assert dispatcher.drain_and_apply() == 1
    assert UsdGeom.GetStageUpAxis(first) == UsdGeom.Tokens.z

    dispatcher.adapter = UsdStageAdapter(second)
    dispatcher.bind_layered_stage(second)

    assert UsdGeom.GetStageUpAxis(first) == UsdGeom.Tokens.y
    assert not first.GetSessionLayer().pseudoRoot.HasInfo("upAxis")
    assert UsdGeom.GetStageUpAxis(second) == UsdGeom.Tokens.z

    dispatcher.close()
    assert UsdGeom.GetStageUpAxis(second) == UsdGeom.Tokens.y
    assert not second.GetSessionLayer().pseudoRoot.HasInfo("upAxis")


def test_dispatcher_resync_clears_shared_stage_state():
    stage = Usd.Stage.CreateInMemory()
    receiver = _LayeredQueue(
        [
            encode_message(_state(1, [(_BASE_LAYER, "Base", False)])),
            encode_message(
                {
                    "type": "event",
                    "seq": 1,
                    "event": {
                        "k": K_SET_STAGE_METADATA,
                        "upAxis": "Z",
                    },
                }
            ),
        ]
    )
    dispatcher = EventDispatcher(receiver=receiver, adapter=UsdStageAdapter(stage))
    assert dispatcher.drain_and_apply() == 1
    assert UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.z

    receiver.messages = [
        encode_message({"type": "resync"}),
        encode_message(_state(1, [(_BASE_LAYER, "Base", False)])),
    ]
    assert dispatcher.drain_and_apply() == 0
    assert UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y


def test_dispatcher_moves_payload_load_state_on_rebind_and_close(tmp_path):
    payload_path = tmp_path / "payload.usda"
    payload_layer = Sdf.Layer.CreateNew(str(payload_path))
    payload_spec = Sdf.CreatePrimInLayer(payload_layer, "/Payload")
    payload_spec.specifier = Sdf.SpecifierDef
    payload_spec.typeName = "Xform"
    payload_layer.defaultPrim = "Payload"
    payload_layer.Save()

    def _stage_with_payload():
        stage = Usd.Stage.CreateInMemory()
        stage.DefinePrim("/World/Thing", "Xform").GetPayloads().AddPayload(
            str(payload_path),
            "/Payload",
        )
        assert stage.GetPrimAtPath("/World/Thing").IsLoaded()
        return stage

    first = _stage_with_payload()
    second = _stage_with_payload()
    receiver = _LayeredQueue(
        [
            encode_message(_state(1, [(_BASE_LAYER, "Base", False)])),
            encode_message(
                {
                    "type": "event",
                    "seq": 1,
                    "event": {
                        "k": K_UNLOAD_PAYLOAD,
                        "prim": "/World/Thing",
                    },
                }
            ),
        ]
    )
    dispatcher = EventDispatcher(
        receiver=receiver,
        adapter=UsdStageAdapter(first),
    )

    assert dispatcher.drain_and_apply() == 1
    assert not first.GetPrimAtPath("/World/Thing").IsLoaded()

    dispatcher.adapter = UsdStageAdapter(second)
    dispatcher.bind_layered_stage(second)

    assert first.GetPrimAtPath("/World/Thing").IsLoaded()
    assert not second.GetPrimAtPath("/World/Thing").IsLoaded()

    dispatcher.close()
    assert second.GetPrimAtPath("/World/Thing").IsLoaded()
