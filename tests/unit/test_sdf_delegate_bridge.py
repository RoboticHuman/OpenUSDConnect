"""Optional exact-build coverage for the native Sdf notice bridge."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pxr import Ar, Sdf, Usd

import openusdconnect.sdf_delegate_bridge as sdf_delegate_bridge
from openusdconnect.event_apply import apply_events
from openusdconnect.sdf_delegate_bridge import (
    NativeDelegateTracker,
    NativeSdfLayerChangeTracker,
)
from openusdconnect.shared_layer_graph import SharedLayerGraph


@pytest.fixture
def bridge_path() -> Path:
    value = os.environ.get("OPENUSDCONNECT_SDF_DELEGATE_BRIDGE")
    if not value:
        pytest.skip("OPENUSDCONNECT_SDF_DELEGATE_BRIDGE is not configured")
    return Path(value)


def test_native_tracker_binds_the_stage_context_while_registering_layers(
    tmp_path,
    monkeypatch,
):
    root = Sdf.Layer.CreateNew(str(tmp_path / "root.usda"))
    root.Save()
    context = Ar.DefaultResolverContext([str(tmp_path)])
    stage = Usd.Stage.Open(root.identifier, context)
    graph = SharedLayerGraph(stage)
    observed_contexts = []

    class _Bridge:
        def __init__(self, _path, _identifiers, *, max_queued_bytes):
            del max_queued_bytes
            observed_contexts.append(Ar.GetResolver().GetCurrentContext())

        def set_layers(self, _identifiers):
            observed_contexts.append(Ar.GetResolver().GetCurrentContext())

        def close(self):
            pass

    monkeypatch.setattr(sdf_delegate_bridge, "NativeDelegateTracker", _Bridge)
    tracker = NativeSdfLayerChangeTracker(stage, graph, "unused")
    try:
        assert len(observed_contexts) == 2
        assert all(item == stage.GetPathResolverContext() for item in observed_contexts)
    finally:
        tracker.close()


def test_bridge_updates_layers_and_suppresses_authoritative_edits(bridge_path):
    tracked = Sdf.Layer.CreateAnonymous("tracked")
    other = Sdf.Layer.CreateAnonymous("other")
    bridge = NativeDelegateTracker(bridge_path, [tracked.identifier])
    try:
        Sdf.CreatePrimInLayer(tracked, "/Local")
        assert {record.layer_identifier for record in bridge.drain()} == {tracked.identifier}

        bridge.set_suppressed(True)
        Sdf.CreatePrimInLayer(tracked, "/Authoritative")
        bridge.set_suppressed(False)
        assert bridge.drain() == ()

        bridge.set_layers([other.identifier])
        Sdf.CreatePrimInLayer(tracked, "/Ignored")
        Sdf.CreatePrimInLayer(other, "/Observed")
        assert {record.layer_identifier for record in bridge.drain()} == {other.identifier}
    finally:
        bridge.close()


def test_bridge_keeps_one_delegate_per_tracked_layer(bridge_path):
    first = Sdf.Layer.CreateAnonymous("first")
    second = Sdf.Layer.CreateAnonymous("second")
    bridge = NativeDelegateTracker(bridge_path, [first.identifier, second.identifier])
    try:
        Sdf.CreatePrimInLayer(first, "/First")
        records = bridge.drain()
        assert {record.layer_identifier for record in records} == {first.identifier}
        assert first.GetPrimAtPath("/First") is not None
        assert second.GetPrimAtPath("/First") is None
    finally:
        bridge.close()


def test_bridge_reports_dictionary_key_mutations(bridge_path):
    layer = Sdf.Layer.CreateAnonymous("dictionary")
    prim = Sdf.CreatePrimInLayer(layer, "/Model")
    bridge = NativeDelegateTracker(bridge_path, [layer.identifier])
    try:
        prim.SetInfoDictionaryValue("customData", "nested:key", 7)
        records = bridge.drain()
        assert any(
            record.layer_identifier == layer.identifier
            and record.path == "/Model"
            and record.fields == ("customData",)
            for record in records
        )
    finally:
        bridge.close()


def test_native_tracker_replays_dictionary_and_time_sample_changes(
    bridge_path,
    tmp_path,
):
    source_layer = Sdf.Layer.CreateNew(str(tmp_path / "source.usda"))
    source_stage = Usd.Stage.Open(source_layer)
    prim = source_stage.DefinePrim("/Model", "Xform")
    sampled = prim.CreateAttribute("sampled", Sdf.ValueTypeNames.Double)
    sampled.Set(1.0)
    graph = SharedLayerGraph(source_stage, authoritative=True)
    target_layer = Sdf.Layer.CreateNew(str(tmp_path / "target.usda"))
    target_layer.TransferContent(source_layer)
    target_stage = Usd.Stage.Open(target_layer)
    tracker = NativeSdfLayerChangeTracker(source_stage, graph, bridge_path)
    try:
        prim.SetCustomDataByKey("nested:key", 7)
        sampled.Set(2.0, 1.0)
        tracker.prepare_local_changes()
        _batch, _layer_key, events = tracker.next_routed_batch()
        with Usd.EditContext(target_stage, Usd.EditTarget(target_layer)):
            apply_events(target_stage, events)
        assert target_layer.ExportToString() == source_layer.ExportToString()
    finally:
        tracker.close()


def test_native_tracker_replays_mixed_changes_without_baseline_snapshot(
    bridge_path,
    tmp_path,
):
    source_layer = Sdf.Layer.CreateNew(str(tmp_path / "source.usda"))
    source_stage = Usd.Stage.Open(source_layer)
    prim = source_stage.DefinePrim("/Model", "Xform")
    prim.CreateAttribute("user:removed", Sdf.ValueTypeNames.Int, True).Set(1)
    prim.CreateRelationship("user:link", True).AddTarget(Sdf.Path("/Model"))
    prim.CreateAttribute("user:before", Sdf.ValueTypeNames.String, True).Set("before")
    graph = SharedLayerGraph(source_stage, authoritative=True)

    target_layer = Sdf.Layer.CreateNew(str(tmp_path / "target.usda"))
    target_layer.TransferContent(source_layer)
    target_stage = Usd.Stage.Open(target_layer)
    tracker = NativeSdfLayerChangeTracker(source_stage, graph, bridge_path)
    try:
        with Sdf.ChangeBlock():
            prim.RemoveProperty("user:removed")
            prim.RemoveProperty("user:link")
            prim.SetDocumentation("changed")
            created = prim.CreateAttribute("user:new", Sdf.ValueTypeNames.Double, True)
            created.Set(3.5)
            created.Set(7.0, 2.0)
            edit = Sdf.BatchNamespaceEdit()
            edit.Add(Sdf.NamespaceEdit.Rename("/Model.user:before", "after"))
            assert source_layer.Apply(edit)

        assert len(tracker.prepare_local_changes()) == 1
        _batch, layer_key, events = tracker.next_routed_batch()
        assert layer_key == graph.root_layer_key
        with Usd.EditContext(target_stage, Usd.EditTarget(target_layer)):
            apply_events(target_stage, events)
        assert target_layer.ExportToString() == source_layer.ExportToString()
    finally:
        tracker.close()


def test_queue_coalescing_replays_complete_current_state(bridge_path, tmp_path):
    source_layer = Sdf.Layer.CreateNew(str(tmp_path / "source.usda"))
    source_stage = Usd.Stage.Open(source_layer)
    source_stage.DefinePrim("/Old", "Xform")
    graph = SharedLayerGraph(source_stage, authoritative=True)
    target_layer = Sdf.Layer.CreateNew(str(tmp_path / "target.usda"))
    target_layer.TransferContent(source_layer)
    target_stage = Usd.Stage.Open(target_layer)
    tracker = NativeSdfLayerChangeTracker(
        source_stage,
        graph,
        bridge_path,
        max_queued_bytes=1,
    )
    try:
        source_stage.GetPrimAtPath("/Old").SetDocumentation("replacement")
        tracker.prepare_local_changes()
        _batch, _layer_key, events = tracker.next_routed_batch()
        assert [event["k"] for event in events] == [
            "replace_sdf_layer_content",
            "set_sublayers",
        ]
        with Usd.EditContext(target_stage, Usd.EditTarget(target_layer)):
            apply_events(target_stage, events)
        assert target_layer.ExportToString() == source_layer.ExportToString()
        assert tracker.coalesced_batch_count >= 1
    finally:
        tracker.close()


def test_prim_rename_replays_complete_descendant_namespace(bridge_path, tmp_path):
    source_layer = Sdf.Layer.CreateNew(str(tmp_path / "source.usda"))
    source_stage = Usd.Stage.Open(source_layer)
    child = source_stage.DefinePrim("/Old/Child", "Scope")
    child.CreateAttribute("user:value", Sdf.ValueTypeNames.Int, True).Set(7)
    graph = SharedLayerGraph(source_stage, authoritative=True)
    target_layer = Sdf.Layer.CreateNew(str(tmp_path / "target.usda"))
    target_layer.TransferContent(source_layer)
    target_stage = Usd.Stage.Open(target_layer)
    tracker = NativeSdfLayerChangeTracker(source_stage, graph, bridge_path)
    try:
        edit = Sdf.BatchNamespaceEdit()
        edit.Add(Sdf.NamespaceEdit.Rename("/Old", "New"))
        assert source_layer.Apply(edit)
        tracker.prepare_local_changes()
        _batch, _layer_key, events = tracker.next_routed_batch()
        with Usd.EditContext(target_stage, Usd.EditTarget(target_layer)):
            apply_events(target_stage, events)
        assert target_layer.ExportToString() == source_layer.ExportToString()
    finally:
        tracker.close()


def test_layer_reload_replays_changes_and_discards_marker_only_notices(
    bridge_path,
    tmp_path,
):
    source_path = tmp_path / "source.usda"
    source_layer = Sdf.Layer.CreateNew(str(source_path))
    source_stage = Usd.Stage.Open(source_layer)
    source_stage.DefinePrim("/Before", "Scope")
    assert source_layer.Save()
    graph = SharedLayerGraph(source_stage, authoritative=True)

    target_layer = Sdf.Layer.CreateNew(str(tmp_path / "target.usda"))
    target_layer.TransferContent(source_layer)
    target_stage = Usd.Stage.Open(target_layer)
    tracker = NativeSdfLayerChangeTracker(source_stage, graph, bridge_path)
    try:
        external = Sdf.Layer.CreateAnonymous("external")
        Sdf.CreatePrimInLayer(external, "/After")
        assert external.Export(str(source_path))
        assert source_layer.Reload(force=True)

        tracker.prepare_local_changes()
        batch, _layer_key, events = tracker.next_routed_batch()
        with Usd.EditContext(target_stage, Usd.EditTarget(target_layer)):
            apply_events(target_stage, events)
        assert target_layer.ExportToString() == source_layer.ExportToString()
        tracker.mark_prepared_sent(batch)

        assert source_layer.Reload(force=True)
        assert tracker.prepare_local_changes() == ()
        assert not tracker.has_local_changes
    finally:
        tracker.close()
