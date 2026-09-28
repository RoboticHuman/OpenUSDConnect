"""Scene ownership of rollback, replay targets, and detached exports."""

import pytest
from pxr import Sdf, Usd

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


@pytest.mark.parametrize("rename", [False, True])
def test_namespace_edits_update_tracked_descendants(scene, rename):
    events = [
        {"k": "ensure_prim", "prim": path, "typeName": type_name}
        for path, type_name in (
            ("/Parent", "Xform"), ("/Parent/Child", "Sphere"), ("/ParentOther", "Scope"),
        )
    ]
    events.append({"k": "set_instanceable", "prim": "/Parent/Child", "instanceable": True})
    scene.apply_validated(events)
    assert scene.get_prim_count() == 3
    edit = (
        {"k": "rename_prim", "prim": "/Parent", "new_name": "Renamed"}
        if rename else {"k": "delete_prim", "prim": "/Parent"}
    )
    scene.apply_validated([edit])

    def assert_tracking():
        tree = {row["path"]: row for row in scene.get_prim_tree()}
        expected = {"/ParentOther", "/Renamed", "/Renamed/Child"} if rename else {"/ParentOther"}
        assert set(tree) == expected
        assert scene.get_tracked_prim_count() == len(expected)
        assert scene.get_prim_count() == len(expected)
        assert scene.get_instance_count() == int(rename)
        if rename:
            assert tree["/Renamed/Child"]["typeName"] == "Sphere"
            assert tree["/Renamed/Child"]["instanceable"]

    assert_tracking()
    scene.rebuild_caches([*events, edit])
    assert_tracking()


def test_namespace_tracking_handles_repeated_deletes_and_new_descendants(scene):
    scene.apply_validated([
        {"k": "ensure_prim", "prim": "/Parent/First", "typeName": "Sphere"},
        {"k": "ensure_prim", "prim": "/Parent/Second", "typeName": "Cube"},
    ])
    scene.apply_validated([{"k": "delete_prim", "prim": "/Parent/First"}])
    scene.apply_validated([{"k": "delete_prim", "prim": "/Parent/First"}])
    scene.apply_validated([
        {"k": "ensure_prim", "prim": "/Parent/New", "typeName": "Xform"},
        {"k": "rename_prim", "prim": "/Parent", "new_name": "Renamed"},
    ])
    assert {row["path"] for row in scene.get_prim_tree()} == {
        "/Renamed", "/Renamed/Second", "/Renamed/New",
    }
    scene.apply_validated([{"k": "delete_prim", "prim": "/Renamed"}])
    assert scene.get_prim_tree() == []


def test_generic_spec_edits_invalidate_composed_count_without_importing_base_tree(scene):
    scene.stage.GetRootLayer().ImportFromString('#usda 1.0\ndef Xform "Base" {}')
    assert scene.get_prim_count() == 1
    fragment = Sdf.Layer.CreateAnonymous()
    spec = Sdf.PrimSpec(fragment, "Created", Sdf.SpecifierDef, "Xform")
    event = {
        "k": "set_sdf_spec_fields", "prim": "/Created", "spec_path": "/Created",
        "spec_kind": "prim", "fields": list(spec.ListInfoKeys()),
        "fragment": fragment.ExportToString(), "removed": False,
    }
    scene.apply_validated([event])
    assert scene.get_prim_count() == 2
    assert scene.get_prim_tree() == []  # This API remains the specialized-event tree.

    scene.apply_validated([{**event, "removed": True, "fragment": ""}])
    assert scene.get_prim_count() == 1


def test_composition_changes_invalidate_count(scene):
    source = Usd.Stage.CreateInMemory()
    source.DefinePrim("/Source", "Xform")
    source.DefinePrim("/Source/Child", "Sphere")
    scene.apply_validated([{"k": "ensure_prim", "prim": "/Target", "typeName": "Xform"}])
    assert scene.get_prim_count() == 1

    scene.apply_validated([{
        "k": "set_payload", "prim": "/Target",
        "payloads": [{"asset_path": source.GetRootLayer().identifier, "prim_path": "/Source"}],
    }])
    assert scene.get_prim_count() == 2
    for kind, expected in (("unload_payload", 0), ("load_payload", 2)):
        scene.apply_validated([{"k": kind, "prim": "/Target"}])
        assert scene.get_prim_count() == expected
    for active, expected in ((False, 0), (True, 2)):
        scene.apply_validated([{"k": "deactivate_prim", "prim": "/Target", "active": active}])
        assert scene.get_prim_count() == expected


def test_count_follows_layer_muting_removal_and_root_merge(scene):
    layer, _ = scene.layer_stack.ensure_layer("department:anim")
    scene.apply_validated([{"k": "ensure_prim", "prim": "/Object", "typeName": "Xform"}], layer)
    assert scene.get_prim_count() == 1
    scene.layer_stack.set_muted("department:anim", True)
    assert scene.get_prim_count() == 0
    scene.layer_stack.set_muted("department:anim", False)
    assert scene.get_prim_count() == 1
    scene.layer_stack.remove_layer("department:anim")
    assert scene.get_prim_count() == 0
    scene.merge_layer_into_root(layer)
    assert scene.get_prim_count() == 1


def test_transform_update_keeps_composed_count_cached(scene, monkeypatch):
    scene.apply_validated(transform_events(1))
    assert scene.get_prim_count() == 1

    def unexpected_traversal():
        pytest.fail("an ordinary transform update should not rescan the stage")

    monkeypatch.setattr(scene.stage, "Traverse", unexpected_traversal)
    scene.apply_validated([transform_events(2)[-1]])
    assert scene.get_prim_count() == 1
