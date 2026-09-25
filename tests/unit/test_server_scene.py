"""Scene ownership of rollback, replay targets, and detached exports."""

import pytest
from pxr import Usd

from openusdconnect.protocol_constants import LayerMode
from openusdconnect.server.scene import SceneState


@pytest.fixture
def scene():
    return SceneState(Usd.Stage.CreateInMemory(), layer_mode=LayerMode.MANAGED, op_cache_size=8)


def transform_events(value):
    return [
        {"k": "ensure_prim", "prim": "/Object", "typeName": "Xform"},
        {"k": "ensure_xform_ops", "prim": "/Object"},
        {"k": "set_xform_trs", "prim": "/Object", "fields": ["t"], "t": [value, 0, 0]},
    ]


def test_replay_restores_target_and_authors_each_layer(scene):
    other, _added = scene.layer_stack.ensure_layer("department:anim", label="anim")
    original_target = Usd.EditTarget(scene.stage.GetRootLayer())
    scene.stage.SetEditTarget(original_target)
    routed = [
        *((scene.edit_layer, event) for event in transform_events(1)),
        *((other, event) for event in transform_events(2)),
        (scene.edit_layer, transform_events(3)[-1]),
    ]

    scene.replay_events(routed)

    assert scene.stage.GetEditTarget() == original_target
    assert tuple(scene.edit_layer.GetAttributeAtPath("/Object.xformOp:translate").default) == (
        3, 0, 0,
    )
    assert tuple(other.GetAttributeAtPath("/Object.xformOp:translate").default) == (2, 0, 0)
    assert scene.edit_layer.GetAttributeAtPath("/Object.xformOpOrder").default
    assert other.GetAttributeAtPath("/Object.xformOpOrder").default
    assert [row["path"] for row in scene.get_prim_tree()] == ["/Object"]


def test_failed_persistence_rolls_back_opinions_and_allows_subsequent_edits(scene):
    scene.apply_validated(transform_events(1))
    original_target = Usd.EditTarget(scene.stage.GetRootLayer())
    scene.stage.SetEditTarget(original_target)
    before_tree = scene.get_prim_tree()
    session = scene.stage.GetSessionLayer()
    before_session = session.ExportToString()

    with pytest.raises(RuntimeError, match="persistence failed"):
        with scene.atomic_edit([(scene.edit_layer, {"/Object"})], include_session=True):
            scene.apply_validated(transform_events(2), update_tracking=False)
            session.customLayerData = {"test": "changed"}
            raise RuntimeError("persistence failed")

    assert scene.stage.GetEditTarget() == original_target
    assert session.ExportToString() == before_session
    assert scene.get_prim_tree() == before_tree
    translation = scene.stage.GetPrimAtPath("/Object").GetAttribute("xformOp:translate")
    assert tuple(translation.Get()) == (1, 0, 0)

    with scene.atomic_edit([(scene.edit_layer, {"/Object"})]):
        scene.apply_validated(transform_events(3))
    assert scene.stage.GetEditTarget() == original_target
    assert tuple(scene.stage.GetPrimAtPath("/Object").GetAttribute("xformOp:translate").Get()) == (
        3, 0, 0,
    )


def test_export_snapshot_does_not_change_with_live_layer(scene):
    scene.apply_validated(transform_events(1))
    snapshot = scene.snapshot_layer(scene.edit_layer)

    scene.apply_validated(transform_events(2))

    exported = Usd.Stage.Open(snapshot)
    value = exported.GetPrimAtPath("/Object").GetAttribute("xformOp:translate").Get()
    assert tuple(value) == (1, 0, 0)
    assert tuple(scene.stage.GetPrimAtPath("/Object").GetAttribute("xformOp:translate").Get()) == (
        2, 0, 0,
    )
