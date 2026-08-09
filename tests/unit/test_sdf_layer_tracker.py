"""Notice-scoped exact diffs for shared-stage file layers."""

from __future__ import annotations

from pxr import Sdf, Usd, Vt

from openusdconnect.event_apply import apply_events
from openusdconnect.sdf_layer_tracker import SdfLayerChangeTracker
from openusdconnect.sdf_spec_delta import serialize_spec_fields
from openusdconnect.shared_layer_graph import SharedLayerGraph


def _property_info(layer: Sdf.Layer, path: str) -> dict:
    spec = layer.GetPropertyAtPath(path)
    return {str(key): spec.GetInfo(key) for key in spec.ListInfoKeys()}


def _root_tracker(stage: Usd.Stage) -> tuple[SharedLayerGraph, SdfLayerChangeTracker]:
    graph = SharedLayerGraph(stage, authoritative=True)
    return graph, SdfLayerChangeTracker(stage, graph)


def test_tracks_custom_property_fields_connections_targets_and_samples():
    stage = Usd.Stage.CreateInMemory()
    prim = stage.DefinePrim("/World/Thing", "Xform")
    attr = prim.CreateAttribute("userProperties:value", Sdf.ValueTypeNames.Double, True)
    attr.Set(1.0)
    rel = prim.CreateRelationship("userProperties:link", True)
    baseline = Sdf.Layer.CreateAnonymous()
    baseline.TransferContent(stage.GetRootLayer())
    _graph, tracker = _root_tracker(stage)
    try:
        attr.Set(2.0)
        attr.Set(3.0, 12.0)
        attr.SetDocumentation("tracked documentation")
        attr.SetCustomData({"department": "lookdev"})
        attr.SetMetadata("allowedTokens", Vt.TokenArray(["one", "two"]))
        attr.AddConnection(Sdf.Path("/World/Source.outputs:value"))
        rel.AddTarget(Sdf.Path("/World/Target"))

        batches = tracker.prepare_local_changes()
        assert len(batches) == 1
        by_path = {event["spec_path"]: event for event in batches[0].events}
        assert set(by_path["/World/Thing.userProperties:value"]["fields"]) == {
            "allowedTokens",
            "connectionPaths",
            "customData",
            "default",
            "documentation",
            "timeSamples",
        }
        assert by_path["/World/Thing.userProperties:link"]["fields"] == ["targetPaths"]

        target = Usd.Stage.Open(baseline)
        apply_events(target, list(batches[0].events))
        for path in by_path:
            assert _property_info(target.GetRootLayer(), path) == _property_info(
                stage.GetRootLayer(), path
            )
    finally:
        tracker.close()


def test_one_metadata_change_does_not_republish_other_fields():
    stage = Usd.Stage.CreateInMemory()
    attr = stage.DefinePrim("/Thing", "Xform").CreateAttribute(
        "user:value",
        Sdf.ValueTypeNames.Double,
        True,
    )
    attr.Set(4.0)
    _graph, tracker = _root_tracker(stage)
    try:
        attr.SetDocumentation("only this changed")
        batch = tracker.prepare_local_changes()[0]
        event = next(event for event in batch.events if event["spec_path"] == "/Thing.user:value")
        assert event["fields"] == ["documentation"]
    finally:
        tracker.close()


def test_layer_metadata_is_an_exact_pseudo_root_delta():
    stage = Usd.Stage.CreateInMemory()
    _graph, tracker = _root_tracker(stage)
    try:
        stage.GetRootLayer().documentation = "layer documentation"
        batch = tracker.prepare_local_changes()[0]
        event = batch.events[0]
        assert event["spec_kind"] == "layer"
        assert event["spec_path"] == "/"
        assert event["fields"] == ["documentation"]
    finally:
        tracker.close()


def test_relative_asset_values_remain_authored_relative():
    stage = Usd.Stage.CreateInMemory()
    attr = stage.DefinePrim("/Material", "Scope").CreateAttribute(
        "inputs:file",
        Sdf.ValueTypeNames.Asset,
        True,
    )
    _graph, tracker = _root_tracker(stage)
    try:
        attr.Set(Sdf.AssetPath("./textures/albedo.exr"))
        batch = tracker.prepare_local_changes()[0]
        event = next(
            event for event in batch.events if event["spec_path"] == "/Material.inputs:file"
        )
        assert "@./textures/albedo.exr@" in event["fragment"]
    finally:
        tracker.close()


def test_binary_layer_uses_the_same_exact_delta_path(tmp_path):
    layer = Sdf.Layer.CreateNew(str(tmp_path / "scene.usdc"))
    stage = Usd.Stage.Open(layer)
    attr = stage.DefinePrim("/Binary", "Xform").CreateAttribute(
        "user:value",
        Sdf.ValueTypeNames.Int,
        True,
    )
    attr.Set(1)
    layer.Save()
    baseline = Sdf.Layer.CreateAnonymous()
    baseline.TransferContent(layer)
    _graph, tracker = _root_tracker(stage)
    try:
        attr.Set(9)
        batch = tracker.prepare_local_changes()[0]

        target = Usd.Stage.Open(baseline)
        apply_events(target, list(batch.events))
        assert target.GetAttributeAtPath("/Binary.user:value").Get() == 9
        assert layer.GetFileFormat().formatId == "usdc"
    finally:
        tracker.close()


def test_variant_and_property_removals_are_explicit():
    stage = Usd.Stage.CreateInMemory()
    prim = Sdf.CreatePrimInLayer(stage.GetRootLayer(), "/Model")
    attr = Sdf.AttributeSpec(prim, "user:value", Sdf.ValueTypeNames.Int)
    attr.default = 7
    variant_set = Sdf.VariantSetSpec(prim, "look")
    Sdf.VariantSpec(variant_set, "red")
    blue = Sdf.VariantSpec(variant_set, "blue")
    _graph, tracker = _root_tracker(stage)
    try:
        stage.GetPrimAtPath("/Model").RemoveProperty("user:value")
        variant_set.RemoveVariant(blue)
        batch = tracker.prepare_local_changes()[0]
        removed = {
            (event["spec_kind"], event["spec_path"])
            for event in batch.events
            if event.get("removed")
        }
        assert ("attribute", "/Model.user:value") in removed
        assert ("variant", "/Model{look=blue}") in removed
    finally:
        tracker.close()


def test_notice_candidate_does_not_hide_an_inactive_variant_edit():
    stage = Usd.Stage.CreateInMemory()
    usd_prim = stage.DefinePrim("/Model", "Xform")
    visible = usd_prim.CreateAttribute("user:visible", Sdf.ValueTypeNames.Int, True)
    visible.Set(1)
    prim = stage.GetRootLayer().GetPrimAtPath("/Model")
    variant_set = Sdf.VariantSetSpec(prim, "look")
    Sdf.VariantSpec(variant_set, "active")
    inactive = Sdf.VariantSpec(variant_set, "inactive")
    hidden = Sdf.AttributeSpec(
        inactive.primSpec,
        "user:hidden",
        Sdf.ValueTypeNames.Int,
    )
    hidden.default = 1
    usd_prim.GetVariantSet("look").SetVariantSelection("active")
    baseline = Sdf.Layer.CreateAnonymous()
    baseline.TransferContent(stage.GetRootLayer())
    _graph, tracker = _root_tracker(stage)
    try:
        with Sdf.ChangeBlock():
            visible.Set(2)
            hidden.default = 2

        batch = tracker.prepare_local_changes()[0]
        by_path = {event["spec_path"]: event for event in batch.events}
        assert by_path["/Model.user:visible"]["fields"] == ["default"]
        assert by_path["/Model{look=inactive}.user:hidden"]["fields"] == ["default"]

        target = Usd.Stage.Open(baseline)
        apply_events(target, list(batch.events))
        assert target.GetRootLayer().ExportToString() == stage.GetRootLayer().ExportToString()
    finally:
        tracker.close()


def test_edits_in_a_muted_shared_layer_still_emit(tmp_path):
    child = Sdf.Layer.CreateNew(str(tmp_path / "child.usda"))
    child_prim = Sdf.CreatePrimInLayer(child, "/Child")
    value = Sdf.AttributeSpec(child_prim, "value", Sdf.ValueTypeNames.Int)
    value.default = 1
    child.Save()
    root = Sdf.Layer.CreateNew(str(tmp_path / "root.usda"))
    root.subLayerPaths.append("./child.usda")
    root.Save()
    stage = Usd.Stage.Open(root)
    _graph, tracker = _root_tracker(stage)
    try:
        stage.MuteLayer(child.identifier)
        assert tracker.prepare_local_changes() == ()

        value.default = 2
        batches = tracker.prepare_local_changes()

        assert len(batches) == 1
        assert batches[0].layer is child
        assert batches[0].events[0]["spec_path"] == "/Child.value"
        assert batches[0].events[0]["fields"] == ["default"]
    finally:
        tracker.close()


def test_detached_layer_edits_are_outside_the_shared_root_graph(tmp_path):
    child = Sdf.Layer.CreateNew(str(tmp_path / "child.usda"))
    child_prim = Sdf.CreatePrimInLayer(child, "/Child")
    value = Sdf.AttributeSpec(child_prim, "value", Sdf.ValueTypeNames.Int)
    value.default = 1
    child.Save()
    root = Sdf.Layer.CreateNew(str(tmp_path / "root.usda"))
    root.subLayerPaths.append("./child.usda")
    root.Save()
    stage = Usd.Stage.Open(root)
    _graph, tracker = _root_tracker(stage)
    try:
        with Sdf.ChangeBlock():
            value.default = 2
            root.subLayerPaths.clear()

        batches = tracker.prepare_local_changes()

        assert len(batches) == 1
        assert batches[0].layer is root
        assert [event["k"] for event in batches[0].events] == ["set_sublayers"]
    finally:
        tracker.close()


def test_remote_unrelated_field_is_not_echoed_with_a_local_edit():
    stage = Usd.Stage.CreateInMemory()
    attr = stage.DefinePrim("/World", "Xform").CreateAttribute(
        "user:value",
        Sdf.ValueTypeNames.Double,
        True,
    )
    attr.Set(1.0)
    _graph, tracker = _root_tracker(stage)
    try:
        attr.Set(3.0)
        batch = tracker.prepare_local_changes()[0]

        remote = Sdf.Layer.CreateAnonymous()
        remote.TransferContent(stage.GetRootLayer())
        remote.GetAttributeAtPath("/World.user:value").documentation = "remote"
        event = {
            "k": "set_sdf_spec_fields",
            "prim": "/World",
            "spec_path": "/World.user:value",
            "spec_kind": "attribute",
            "fields": ["documentation"],
            "fragment": serialize_spec_fields(
                remote,
                "/World.user:value",
                "attribute",
                ["documentation"],
                stabilize_asset_paths=False,
            ),
            "removed": False,
        }
        with tracker.suppressed():
            apply_events(stage, [event])
            tracker.accept_authoritative_event(stage.GetRootLayer(), event)
        tracker.restore_prepared()

        assert attr.Get() == 3.0
        assert attr.GetDocumentation() == "remote"
        routed = tracker.next_routed_batch()
        assert routed is not None
        assert routed[2][0]["fields"] == ["default"]
        tracker.mark_prepared_sent(batch)

        attr.Set(4.0)
        next_batch = tracker.prepare_local_changes()[0]
        assert next_batch.events[0]["fields"] == ["default"]
    finally:
        tracker.close()


def test_prepared_batch_is_stable_until_sent():
    stage = Usd.Stage.CreateInMemory()
    attr = stage.DefinePrim("/Thing", "Xform").CreateAttribute(
        "user:value",
        Sdf.ValueTypeNames.Int,
        True,
    )
    attr.Set(1)
    _graph, tracker = _root_tracker(stage)
    try:
        attr.Set(2)
        first = tracker.prepare_local_changes()[0]
        second = tracker.prepare_local_changes()[0]
        assert second is first
        assert tracker.prepared_event_count == len(first.events)
    finally:
        tracker.close()


def test_newly_attached_shared_file_is_baselined_not_republished(tmp_path):
    root = Sdf.Layer.CreateNew(str(tmp_path / "root.usda"))
    root.Save()
    stage = Usd.Stage.Open(root)
    _graph, tracker = _root_tracker(stage)
    try:
        child = Sdf.Layer.CreateNew(str(tmp_path / "child.usda"))
        prim = Sdf.CreatePrimInLayer(child, "/Child")
        attr = Sdf.AttributeSpec(prim, "value", Sdf.ValueTypeNames.Int)
        attr.default = 12
        child.Save()
        root.subLayerPaths.append("./child.usda")

        batches = tracker.prepare_local_changes()

        assert len(batches) == 1
        assert [event["k"] for event in batches[0].events] == ["set_sublayers"]
    finally:
        tracker.close()


def test_session_layer_is_outside_the_shared_file_graph():
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/World", "Xform")
    _graph, tracker = _root_tracker(stage)
    try:
        stage.SetEditTarget(Usd.EditTarget(stage.GetSessionLayer()))
        stage.GetPrimAtPath("/World").CreateAttribute(
            "user:temporary",
            Sdf.ValueTypeNames.Int,
            True,
        ).Set(3)

        assert tracker.prepare_local_changes() == ()
    finally:
        tracker.close()
