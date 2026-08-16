"""Receiver-local retries for late reference and payload dependencies."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from pxr import Ar, Sdf, Usd

from openusdconnect.adapters import MockAdapter, UsdStageAdapter
from openusdconnect.codec import encode_message
from openusdconnect.dispatcher import EventDispatcher
from openusdconnect.protocol_constants import (
    K_DELETE_PRIM,
    K_LOAD_PAYLOAD,
    K_RENAME_PRIM,
    K_SET_PAYLOAD,
    K_SET_REFERENCE,
    K_SET_VARIANT_SELECTIONS,
    K_UNLOAD_PAYLOAD,
)
from openusdconnect.sdf_arc_state import (
    deserialize_reference_custom_data,
    serialize_reference_custom_data,
)


class _NullReceiver:
    layered_replay_active = False
    origin = None

    def drain_queue(self):
        return []

    def mark_replay_applied(self):
        return False

    def request_replay_from(self, _seq_start):
        pass


class _QueueReceiver:
    layered_replay_active = False
    origin = None

    def __init__(self):
        self.messages = []
        self.replay_requests = []

    def drain_queue(self):
        messages, self.messages = self.messages, []
        return messages

    def mark_replay_applied(self):
        return False

    def request_replay_from(self, seq_start):
        self.replay_requests.append(seq_start)


def _create_variant_asset(path, version: str = "1") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = Usd.Stage.CreateNew(str(path))
    asset = stage.DefinePrim("/Asset", "Xform")
    stage.SetDefaultPrim(asset)
    variants = asset.GetVariantSets().AddVariantSet("look")
    for name in ("red", "blue"):
        variants.AddVariant(name)
        variants.SetVariantSelection(name)
        with variants.GetVariantEditContext():
            asset.CreateAttribute(
                "user:resolvedVariant",
                Sdf.ValueTypeNames.String,
                custom=True,
            ).Set(name)
            asset.CreateAttribute(
                "user:assetVersion",
                Sdf.ValueTypeNames.String,
                custom=True,
            ).Set(version)
    variants.SetVariantSelection("red")
    stage.GetRootLayer().Save()


def _stage_with_search_context(asset_directory) -> Usd.Stage:
    context = Ar.DefaultResolverContext([str(asset_directory)])
    return Usd.Stage.CreateInMemory("receiver.usda", context)


def _arc_event(kind: str, prim_path: str, asset_path: str) -> dict:
    field = "refs" if kind == K_SET_REFERENCE else "payloads"
    return {
        "k": kind,
        "prim": prim_path,
        field: [{"asset_path": asset_path, "prim_path": "/Asset"}],
    }


def _arc_item_count(spec, attr_name: str) -> int:
    list_op = getattr(spec, attr_name)
    return sum(
        len(items)
        for items in (
            list_op.explicitItems,
            list_op.addedItems,
            list_op.prependedItems,
            list_op.appendedItems,
            list_op.deletedItems,
            list_op.orderedItems,
        )
    )


@pytest.mark.parametrize(
    ("kind", "list_attr"),
    [
        (K_SET_REFERENCE, "referenceList"),
        (K_SET_PAYLOAD, "payloadList"),
    ],
)
def test_refresh_late_dependency_preserves_variant_composition(
    tmp_path,
    kind,
    list_attr,
):
    asset_directory = tmp_path / "resolver-root"
    asset_directory.mkdir()
    stage = _stage_with_search_context(asset_directory)
    dispatcher = EventDispatcher(
        receiver=_NullReceiver(),
        adapter=UsdStageAdapter(stage),
    )
    dispatcher.last_seq = 41

    prim_path = "/World/SharedAsset"
    selection = {
        "k": K_SET_VARIANT_SELECTIONS,
        "prim": prim_path,
        "selections": {"look": "blue"},
    }
    arc = _arc_event(kind, prim_path, "late.usda")
    dispatcher._apply([selection, arc])

    prim = stage.GetPrimAtPath(prim_path)
    variant_set = prim.GetVariantSets().GetVariantSet("look")
    assert variant_set.GetVariantNames() == []
    assert dispatcher.pending_asset_dependencies == ("late.usda",)

    missing = dispatcher.refresh_asset_dependency("late.usda")
    assert missing == {
        "status": "still_missing",
        "reapplied": 0,
        "affected_prims": [],
        "pending": ["late.usda"],
    }

    _create_variant_asset(asset_directory / "late.usda", version="2")
    refreshed = dispatcher.refresh_asset_dependency("late.usda")

    assert refreshed == {
        "status": "refreshed",
        "reapplied": 1,
        "affected_prims": [prim_path],
        "pending": [],
    }
    assert dispatcher.last_seq == 41
    assert dispatcher.pending_asset_dependencies == ()

    prim = stage.GetPrimAtPath(prim_path)
    variant_set = prim.GetVariantSets().GetVariantSet("look")
    assert variant_set.GetVariantNames() == ["blue", "red"]
    assert variant_set.GetVariantSelection() == "blue"
    assert prim.GetAttribute("user:resolvedVariant").Get() == "blue"
    assert prim.GetAttribute("user:assetVersion").Get() == "2"

    local_spec = stage.GetRootLayer().GetPrimAtPath(prim_path)
    assert dict(local_spec.variantSelections) == {"look": "blue"}
    assert list(local_spec.variantSets.keys()) == []
    assert _arc_item_count(local_spec, list_attr) == 1

    second = dispatcher.refresh_asset_dependency()
    assert second["status"] == "not_tracked"
    assert second["reapplied"] == 0
    assert _arc_item_count(local_spec, list_attr) == 1


def test_pending_dependencies_follow_namespace_and_arc_lifecycle(tmp_path):
    asset_directory = tmp_path / "resolver-root"
    asset_directory.mkdir()
    stage = _stage_with_search_context(asset_directory)
    dispatcher = EventDispatcher(
        receiver=_NullReceiver(),
        adapter=UsdStageAdapter(stage),
    )

    dispatcher._apply([_arc_event(K_SET_REFERENCE, "/World/Original", "renamed.usda")])
    dispatcher._apply([{"k": K_RENAME_PRIM, "prim": "/World/Original", "new_name": "Renamed"}])
    _create_variant_asset(asset_directory / "renamed.usda")

    renamed = dispatcher.refresh_asset_dependency("renamed.usda")
    assert renamed["affected_prims"] == ["/World/Renamed"]
    assert stage.GetPrimAtPath("/World/Renamed").GetAttribute("user:assetVersion").Get() == "1"

    dispatcher._apply([_arc_event(K_SET_REFERENCE, "/World/DeleteMe", "deleted.usda")])
    assert dispatcher.pending_asset_dependencies == ("deleted.usda",)
    dispatcher._apply([{"k": K_DELETE_PRIM, "prim": "/World/DeleteMe"}])
    assert dispatcher.pending_asset_dependencies == ()

    dispatcher._apply([_arc_event(K_SET_REFERENCE, "/World/ClearMe", "cleared.usda")])
    assert dispatcher.pending_asset_dependencies == ("cleared.usda",)
    dispatcher._apply([{"k": K_SET_REFERENCE, "prim": "/World/ClearMe", "refs": []}])
    assert dispatcher.pending_asset_dependencies == ()


def test_pending_dependencies_are_scoped_to_their_authored_layer(tmp_path):
    asset_directory = tmp_path / "resolver-root"
    asset_directory.mkdir()
    stage = _stage_with_search_context(asset_directory)
    strong = Sdf.Layer.CreateAnonymous("strong")
    weak = Sdf.Layer.CreateAnonymous("weak")
    stage.GetSessionLayer().subLayerPaths = [strong.identifier, weak.identifier]
    dispatcher = EventDispatcher(
        receiver=_NullReceiver(),
        adapter=UsdStageAdapter(stage),
    )

    with Usd.EditContext(stage, Usd.EditTarget(strong)):
        dispatcher._apply([_arc_event(K_SET_REFERENCE, "/World/Asset", "strong.usda")])
    with Usd.EditContext(stage, Usd.EditTarget(weak)):
        dispatcher._apply([_arc_event(K_SET_REFERENCE, "/World/Asset", "weak.usda")])

    assert dispatcher.pending_asset_dependencies == ("strong.usda", "weak.usda")

    with Usd.EditContext(stage, Usd.EditTarget(weak)):
        dispatcher._apply(
            [{"k": K_SET_REFERENCE, "prim": "/World/Asset", "refs": []}],
        )

    assert dispatcher.pending_asset_dependencies == ("strong.usda",)

    with Usd.EditContext(stage, Usd.EditTarget(weak)):
        dispatcher._apply([_arc_event(K_SET_REFERENCE, "/World/Asset", "weak.usda")])
        dispatcher._apply(
            [{"k": K_RENAME_PRIM, "prim": "/World/Asset", "new_name": "Moved"}],
        )

    tracked = {
        dependency[0]: event.event["prim"]
        for event in dispatcher._asset_events.values()
        for dependency in event.dependencies
    }
    assert tracked == {
        "strong.usda": "/World/Asset",
        "weak.usda": "/World/Moved",
    }


def test_untracked_asset_refresh_still_refreshes_resolver_context(tmp_path, monkeypatch):
    stage = _stage_with_search_context(tmp_path)
    dispatcher = EventDispatcher(
        receiver=_NullReceiver(),
        adapter=UsdStageAdapter(stage),
    )
    refreshed = []
    monkeypatch.setattr(
        dispatcher,
        "_refresh_resolver_context_suppressed",
        refreshed.append,
    )

    result = dispatcher.refresh_asset_dependency("asset:Texture/latest.tx")

    assert result["status"] == "not_tracked"
    assert refreshed == [stage]


def test_explicit_resolver_context_refresh_needs_no_tracked_arc(tmp_path, monkeypatch):
    stage = _stage_with_search_context(tmp_path)
    dispatcher = EventDispatcher(
        receiver=_NullReceiver(),
        adapter=UsdStageAdapter(stage),
    )
    refreshed = []
    monkeypatch.setattr(
        dispatcher,
        "_refresh_resolver_context_suppressed",
        refreshed.append,
    )

    assert dispatcher.refresh_resolver_context()
    assert refreshed == [stage]


def test_refresh_does_not_overwrite_newer_local_arc_authoring(tmp_path):
    asset_directory = tmp_path / "resolver-root"
    asset_directory.mkdir()
    stage = _stage_with_search_context(asset_directory)
    dispatcher = EventDispatcher(
        receiver=_NullReceiver(),
        adapter=UsdStageAdapter(stage),
    )
    dispatcher._apply([_arc_event(K_SET_REFERENCE, "/World/Asset", "received.usda")])

    references = stage.GetPrimAtPath("/World/Asset").GetReferences()
    references.ClearReferences()
    references.AddReference("local.usda", "/Asset")
    _create_variant_asset(asset_directory / "received.usda")

    result = dispatcher.refresh_asset_dependency("received.usda")
    spec = stage.GetRootLayer().GetPrimAtPath("/World/Asset")

    assert result["status"] == "not_tracked"
    assert result["reapplied"] == 0
    assert spec.referenceList.prependedItems[0].assetPath == "local.usda"


@pytest.mark.parametrize(
    ("kind", "list_attr"),
    [
        (K_SET_REFERENCE, "referenceList"),
        (K_SET_PAYLOAD, "payloadList"),
    ],
)
def test_refresh_does_not_overwrite_newer_rich_arc_state(
    tmp_path,
    kind,
    list_attr,
):
    asset_directory = tmp_path / "resolver-root"
    asset_directory.mkdir()
    stage = _stage_with_search_context(asset_directory)
    dispatcher = EventDispatcher(
        receiver=_NullReceiver(),
        adapter=UsdStageAdapter(stage),
    )
    prim_path = "/World/Asset"
    dispatcher._apply([_arc_event(kind, prim_path, "received.usda")])

    spec = stage.GetRootLayer().GetPrimAtPath(prim_path)
    if kind == K_SET_REFERENCE:
        item = Sdf.Reference(
            "received.usda",
            "/Asset",
            Sdf.LayerOffset(7, 2),
            {"local": "keep"},
        )
    else:
        item = Sdf.Payload(
            "received.usda",
            "/Asset",
            Sdf.LayerOffset(7, 2),
        )
    getattr(spec, list_attr).prependedItems = [item]
    _create_variant_asset(asset_directory / "received.usda")

    result = dispatcher.refresh_asset_dependency("received.usda")
    preserved = getattr(spec, list_attr).prependedItems[0]

    assert result["status"] == "not_tracked"
    assert result["reapplied"] == 0
    assert preserved.layerOffset == Sdf.LayerOffset(7, 2)
    if kind == K_SET_REFERENCE:
        assert preserved.customData == {"local": "keep"}


@pytest.mark.parametrize(
    ("kind", "list_attr"),
    [
        (K_SET_REFERENCE, "referenceList"),
        (K_SET_PAYLOAD, "payloadList"),
    ],
)
def test_refresh_preserves_received_rich_arc_state(
    tmp_path,
    kind,
    list_attr,
):
    asset_directory = tmp_path / "resolver-root"
    asset_directory.mkdir()
    stage = _stage_with_search_context(asset_directory)
    dispatcher = EventDispatcher(
        receiver=_NullReceiver(),
        adapter=UsdStageAdapter(stage),
    )
    entry = {
        "asset_path": "received.usda",
        "prim_path": "/Asset",
        "list_position": "appended",
        "layer_offset": 7.0,
        "layer_scale": 2.0,
    }
    if kind == K_SET_REFERENCE:
        entry["custom_data_fragment"] = serialize_reference_custom_data(
            {"source": "received"},
        )
    entries_key = "refs" if kind == K_SET_REFERENCE else "payloads"
    event = {
        "k": kind,
        "prim": "/World/Asset",
        entries_key: [entry],
        "list_op_authored": True,
        "list_op_explicit": False,
    }

    dispatcher._apply([event])
    assert dispatcher.pending_asset_dependencies == ("received.usda",)
    _create_variant_asset(asset_directory / "received.usda")

    result = dispatcher.refresh_asset_dependency("received.usda")
    item = getattr(
        stage.GetRootLayer().GetPrimAtPath("/World/Asset"),
        list_attr,
    ).appendedItems[0]

    assert result["status"] == "refreshed"
    assert item.layerOffset == Sdf.LayerOffset(7.0, 2.0)
    if kind == K_SET_REFERENCE:
        assert item.customData == deserialize_reference_custom_data(
            entry["custom_data_fragment"],
        )


def test_refresh_does_not_rewrite_local_list_op_position(tmp_path):
    asset_directory = tmp_path / "resolver-root"
    asset_directory.mkdir()
    stage = _stage_with_search_context(asset_directory)
    dispatcher = EventDispatcher(
        receiver=_NullReceiver(),
        adapter=UsdStageAdapter(stage),
    )
    prim_path = "/World/Asset"
    dispatcher._apply([_arc_event(K_SET_REFERENCE, prim_path, "received.usda")])

    spec = stage.GetRootLayer().GetPrimAtPath(prim_path)
    item = spec.referenceList.prependedItems[0]
    spec.referenceList.prependedItems = []
    spec.referenceList.explicitItems = [item]
    _create_variant_asset(asset_directory / "received.usda")

    result = dispatcher.refresh_asset_dependency("received.usda")

    assert result["status"] == "not_tracked"
    assert result["reapplied"] == 0
    assert spec.referenceList.prependedItems == []
    assert spec.referenceList.explicitItems == [item]


def test_refresh_replays_arc_in_original_edit_target(tmp_path):
    asset_directory = tmp_path / "resolver-root"
    asset_directory.mkdir()
    stage = _stage_with_search_context(asset_directory)
    root_layer = stage.GetRootLayer()
    session_layer = stage.GetSessionLayer()
    stage.SetEditTarget(Usd.EditTarget(root_layer))
    dispatcher = EventDispatcher(
        receiver=_NullReceiver(),
        adapter=UsdStageAdapter(stage),
    )
    prim_path = "/World/Asset"
    dispatcher._apply([_arc_event(K_SET_REFERENCE, prim_path, "late.usda")])

    stage.SetEditTarget(Usd.EditTarget(session_layer))
    _create_variant_asset(asset_directory / "late.usda")
    result = dispatcher.refresh_asset_dependency("late.usda")

    assert result["status"] == "refreshed"
    assert stage.GetEditTarget().GetLayer() == session_layer
    assert session_layer.GetPrimAtPath(prim_path) is None
    assert (
        _arc_item_count(
            root_layer.GetPrimAtPath(prim_path),
            "referenceList",
        )
        == 1
    )
    assert stage.GetPrimAtPath(prim_path).GetAttribute("user:assetVersion").Get() == "1"


def test_refresh_preserves_mapped_variant_edit_target(tmp_path):
    asset_directory = tmp_path / "resolver-root"
    asset_directory.mkdir()
    stage = _stage_with_search_context(asset_directory)
    world = stage.DefinePrim("/World", "Xform")
    variants = world.GetVariantSets().AddVariantSet("layout")
    variants.AddVariant("main")
    variants.SetVariantSelection("main")
    dispatcher = EventDispatcher(
        receiver=_NullReceiver(),
        adapter=UsdStageAdapter(stage),
    )
    prim_path = "/World/Asset"

    with variants.GetVariantEditContext():
        dispatcher._apply([_arc_event(K_SET_REFERENCE, prim_path, "late.usda")])

    _create_variant_asset(asset_directory / "late.usda")
    result = dispatcher.refresh_asset_dependency("late.usda")

    assert result["status"] == "refreshed"
    direct_spec = stage.GetRootLayer().GetPrimAtPath(prim_path)
    assert direct_spec is None
    variant_spec = stage.GetRootLayer().GetPrimAtPath(
        "/World{layout=main}Asset",
    )
    assert _arc_item_count(variant_spec, "referenceList") == 1
    assert stage.GetPrimAtPath(prim_path).GetAttribute("user:assetVersion").Get() == "1"


class _RecordingAdapter(MockAdapter):
    def __init__(self):
        super().__init__()
        self.reference_calls = 0
        self.payload_calls = 0

    def set_reference(
        self,
        prim_path: str,
        refs: list,
        **kwargs,
    ) -> bool:
        self.reference_calls += 1
        return super().set_reference(prim_path, refs, **kwargs)

    def set_payload(
        self,
        prim_path: str,
        payloads: list,
        **kwargs,
    ) -> bool:
        self.payload_calls += 1
        return super().set_payload(prim_path, payloads, **kwargs)


class _EmitterProbe:
    def __init__(self):
        self.depth = 0
        self.invalidated_at_depth = []

    @contextmanager
    def suppressed(self):
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1

    def invalidate_for_event(self, event):
        self.invalidated_at_depth.append((event["k"], self.depth))


def test_refresh_reapplies_to_mirror_and_dcc_adapter(tmp_path):
    asset_directory = tmp_path / "resolver-root"
    asset_directory.mkdir()
    stage = _stage_with_search_context(asset_directory)
    adapter = _RecordingAdapter()
    imported = []
    dispatcher = EventDispatcher(
        receiver=_NullReceiver(),
        adapter=adapter,
        mirror_stage=stage,
        on_imported=imported.append,
    )

    event = _arc_event(K_SET_REFERENCE, "/World/Asset", "dcc.usda")
    dispatcher._apply([event])
    assert adapter.reference_calls == 1
    assert imported == [["/World/Asset"]]

    _create_variant_asset(asset_directory / "dcc.usda")
    result = dispatcher.refresh_asset_dependency()

    assert result["status"] == "refreshed"
    assert adapter.reference_calls == 2
    assert imported == [["/World/Asset"], ["/World/Asset"]]
    assert stage.GetPrimAtPath("/World/Asset").GetAttribute("user:assetVersion").Get() == "1"

    explicit = dispatcher.refresh_asset_dependency("dcc.usda")
    assert explicit["status"] == "refreshed"
    assert adapter.reference_calls == 3
    assert (
        _arc_item_count(
            stage.GetRootLayer().GetPrimAtPath("/World/Asset"),
            "referenceList",
        )
        == 1
    )


def test_context_refresh_recovers_other_dependencies_that_changed(tmp_path):
    asset_directory = tmp_path / "resolver-root"
    asset_directory.mkdir()
    stage = _stage_with_search_context(asset_directory)
    dispatcher = EventDispatcher(
        receiver=_NullReceiver(),
        adapter=UsdStageAdapter(stage),
    )
    dispatcher._apply(
        [
            _arc_event(K_SET_REFERENCE, "/World/A", "a.usda"),
            _arc_event(K_SET_REFERENCE, "/World/B", "b.usda"),
        ]
    )
    _create_variant_asset(asset_directory / "a.usda")
    _create_variant_asset(asset_directory / "b.usda")

    result = dispatcher.refresh_asset_dependency("a.usda")

    assert result == {
        "status": "refreshed",
        "reapplied": 2,
        "affected_prims": ["/World/A", "/World/B"],
        "pending": [],
    }
    assert stage.GetPrimAtPath("/World/A").GetAttribute("user:assetVersion").Get() == "1"
    assert stage.GetPrimAtPath("/World/B").GetAttribute("user:assetVersion").Get() == "1"


def test_payload_refresh_preserves_explicit_unloaded_state(tmp_path):
    asset_directory = tmp_path / "resolver-root"
    asset_directory.mkdir()
    _create_variant_asset(asset_directory / "payload.usda")
    stage = _stage_with_search_context(asset_directory)
    dispatcher = EventDispatcher(
        receiver=_NullReceiver(),
        adapter=UsdStageAdapter(stage),
    )
    prim_path = "/World/Payload"
    dispatcher._apply([_arc_event(K_SET_PAYLOAD, prim_path, "payload.usda")])
    assert stage.GetPrimAtPath(prim_path).IsLoaded()

    dispatcher._apply([{"k": K_UNLOAD_PAYLOAD, "prim": prim_path}])
    assert not stage.GetPrimAtPath(prim_path).IsLoaded()

    result = dispatcher.refresh_asset_dependency("payload.usda")
    assert result["status"] == "refreshed"
    assert not stage.GetPrimAtPath(prim_path).IsLoaded()


def test_loaded_payload_refresh_replays_load_to_dcc_adapter(tmp_path):
    asset_directory = tmp_path / "resolver-root"
    asset_directory.mkdir()
    _create_variant_asset(asset_directory / "payload.usda")
    stage = _stage_with_search_context(asset_directory)
    adapter = _RecordingAdapter()
    imported = []
    dispatcher = EventDispatcher(
        receiver=_NullReceiver(),
        adapter=adapter,
        mirror_stage=stage,
        on_imported=imported.append,
    )
    prim_path = "/World/Payload"
    dispatcher._apply(
        [
            _arc_event(K_SET_PAYLOAD, prim_path, "payload.usda"),
            {"k": K_LOAD_PAYLOAD, "prim": prim_path},
        ]
    )
    assert adapter.payload_calls == 1
    assert adapter.calls == [("load_payload", prim_path)]

    result = dispatcher.refresh_asset_dependency("payload.usda")

    assert result["status"] == "refreshed"
    assert adapter.payload_calls == 2
    assert adapter.calls == [
        ("load_payload", prim_path),
        ("load_payload", prim_path),
    ]
    assert imported == [[prim_path], [prim_path]]


def test_resolver_refresh_runs_inside_emitter_suppression(tmp_path, monkeypatch):
    asset_directory = tmp_path / "resolver-root"
    asset_directory.mkdir()
    stage = _stage_with_search_context(asset_directory)
    emitter = _EmitterProbe()
    dispatcher = EventDispatcher(
        receiver=_NullReceiver(),
        adapter=UsdStageAdapter(stage),
        emitter=emitter,
    )
    dispatcher._apply([_arc_event(K_SET_REFERENCE, "/World/Asset", "suppressed.usda")])
    _create_variant_asset(asset_directory / "suppressed.usda")

    original = dispatcher._refresh_asset_dependency_suppressed

    def assert_suppressed(refresh_stage, asset_path, tracked):
        assert emitter.depth == 1
        return original(refresh_stage, asset_path, tracked)

    monkeypatch.setattr(
        dispatcher,
        "_refresh_asset_dependency_suppressed",
        assert_suppressed,
    )
    dispatcher.refresh_asset_dependency("suppressed.usda")

    assert emitter.depth == 0
    assert emitter.invalidated_at_depth == [
        (K_SET_REFERENCE, 1),
        (K_SET_REFERENCE, 1),
    ]


def test_resync_discards_pending_dependency_history(tmp_path):
    asset_directory = tmp_path / "resolver-root"
    asset_directory.mkdir()
    stage = _stage_with_search_context(asset_directory)
    receiver = _QueueReceiver()
    dispatcher = EventDispatcher(
        receiver=receiver,
        adapter=UsdStageAdapter(stage),
    )
    dispatcher._apply([_arc_event(K_SET_REFERENCE, "/World/Asset", "pre-resync.usda")])
    assert dispatcher.pending_asset_dependencies == ("pre-resync.usda",)

    receiver.messages.append(encode_message({"type": "resync"}))
    assert dispatcher.drain_and_apply() == 0
    assert dispatcher.pending_asset_dependencies == ()


def test_repeated_asset_path_resolves_once_per_batch(tmp_path, monkeypatch):
    asset_directory = tmp_path / "resolver-root"
    asset_directory.mkdir()
    stage = _stage_with_search_context(asset_directory)
    dispatcher = EventDispatcher(
        receiver=_NullReceiver(),
        adapter=UsdStageAdapter(stage),
    )
    resolved_paths = []

    def resolve_once(_stage, _layer, authored_path):
        resolved_paths.append(authored_path)
        return authored_path, ""

    monkeypatch.setattr(dispatcher, "_resolve_asset", resolve_once)
    events = [
        _arc_event(K_SET_REFERENCE, f"/World/Asset_{index}", "shared.usda") for index in range(100)
    ]
    dispatcher._apply(events)

    assert resolved_paths == ["shared.usda"]
    assert dispatcher.pending_asset_dependencies == ("shared.usda",)
