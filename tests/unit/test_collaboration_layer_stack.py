"""Server-owned collaboration layer stack tests."""

from __future__ import annotations

import pytest
from pxr import Sdf, Usd

from openusdconnect.server.layer_stack import CollaborationLayerStack


def _make_stack():
    stage = Usd.Stage.CreateInMemory()
    default = Sdf.Layer.CreateAnonymous("default-edits")
    return stage, CollaborationLayerStack(stage, default), default


def _set_value(stage, layer, value):
    with Usd.EditContext(stage, Usd.EditTarget(layer)):
        prim = stage.OverridePrim("/World/Thing")
        prim.CreateAttribute(
            "userProperties:value",
            Sdf.ValueTypeNames.Int,
        ).Set(value)


def test_default_layer_is_weakest_and_reverse_lookup_is_local():
    stage, stack, default = _make_stack()
    strong, added = stack.ensure_layer("shot:animation", label="Animation")

    assert added
    assert stack.layer_keys == ("shot:animation", "default")
    assert stack.ordered_layers == (strong, default)
    assert stack.key_for_layer(strong) == "shot:animation"
    assert stack.key_for_layer(Sdf.Layer.CreateAnonymous("external")) is None
    assert list(stage.GetSessionLayer().subLayerPaths) == [
        strong.identifier,
        default.identifier,
    ]


def test_reorder_and_mute_change_composition_without_moving_opinions():
    stage, stack, default = _make_stack()
    animation, _ = stack.ensure_layer("animation")
    layout, _ = stack.ensure_layer("layout")
    stack.set_order(["animation", "layout", "default"])

    _set_value(stage, default, 0)
    _set_value(stage, layout, 1)
    _set_value(stage, animation, 2)
    attribute = stage.GetAttributeAtPath(
        "/World/Thing.userProperties:value"
    )
    assert attribute.Get() == 2

    assert stack.set_order(["layout", "animation", "default"])
    assert attribute.Get() == 1
    assert stack.set_muted("layout", True)
    assert attribute.Get() == 2
    assert stack.set_muted("layout", False)
    assert attribute.Get() == 1

    assert layout.GetAttributeAtPath(
        "/World/Thing.userProperties:value"
    ).default == 1
    assert animation.GetAttributeAtPath(
        "/World/Thing.userProperties:value"
    ).default == 2


def test_stack_changes_preserve_unmanaged_sublayers_and_offsets():
    stage = Usd.Stage.CreateInMemory()
    unrelated = Sdf.Layer.CreateAnonymous("unrelated")
    session = stage.GetSessionLayer()
    session.subLayerPaths.append(unrelated.identifier)
    session.subLayerOffsets[0] = Sdf.LayerOffset(7, 2)
    default = Sdf.Layer.CreateAnonymous("default-edits")

    stack = CollaborationLayerStack(stage, default)
    review, _ = stack.ensure_layer("review")
    stack.set_order(["review", "default"])

    assert list(session.subLayerPaths) == [
        review.identifier,
        default.identifier,
        unrelated.identifier,
    ]
    assert session.subLayerOffsets[2] == Sdf.LayerOffset(7, 2)


def test_remove_detaches_muted_layer_and_keeps_default():
    stage, stack, default = _make_stack()
    transient, _ = stack.ensure_layer("transient")
    stack.set_muted("transient", True)

    removed = stack.remove_layer("transient")

    assert removed is transient
    assert stack.layer_keys == ("default",)
    assert list(stage.GetSessionLayer().subLayerPaths) == [default.identifier]
    assert not stage.IsLayerMuted(transient.identifier)
    assert stack.key_for_layer(transient) is None

    with pytest.raises(ValueError, match="default"):
        stack.remove_layer("default")


@pytest.mark.parametrize(
    ("order", "message"),
    [
        (["animation", "animation", "default"], "duplicates"),
        (["default"], "every managed key"),
        (["layout", "animation", "default"], "every managed key"),
        (["default", "animation"], "weakest"),
    ],
)
def test_order_rejects_invalid_topology(order, message):
    _stage, stack, _default = _make_stack()
    stack.ensure_layer("animation")

    with pytest.raises(ValueError, match=message):
        stack.set_order(order)


def test_state_uses_portable_keys_and_presentation_labels():
    _stage, stack, _default = _make_stack()
    stack.ensure_layer("shot:animation", label="Animation")
    stack.set_muted("shot:animation", True)

    state = stack.state()

    assert state["revision"] == 3
    assert state["layers"] == [
        {
            "layer_key": "shot:animation",
            "label": "Animation",
            "muted": True,
        },
        {
            "layer_key": "default",
            "label": "Default",
            "muted": False,
        },
    ]
