"""Server routing, replay, and compaction for shared file-layer graphs."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from pxr import Sdf, Usd, UsdGeom

from openusdconnect.codec import message_to_dict
from openusdconnect.protocol_constants import LayerMode
from openusdconnect.sdf_spec_delta import serialize_layer_content, serialize_spec_fields
from openusdconnect.server.state import UsdSyncServer
from openusdconnect.server.types import TransactionRejectedError
from openusdconnect.shared_layer_graph import StaleLayerGraphError


def _create_layer(path: Path, prim_path: str | None = None) -> Sdf.Layer:
    layer = Sdf.Layer.CreateNew(str(path))
    if prim_path:
        Sdf.CreatePrimInLayer(layer, prim_path)
    layer.Save()
    return layer


def _create_stage(directory: Path) -> str:
    child = _create_layer(directory / "asset.usda")
    prim = Sdf.CreatePrimInLayer(child, "/World")
    attr = Sdf.AttributeSpec(prim, "value", Sdf.ValueTypeNames.Float)
    attr.default = 1.0
    child.Save()
    root = _create_layer(directory / "scene.usda")
    root.subLayerPaths.append("./asset.usda")
    root.Save()
    return root.identifier


@contextmanager
def _shared_server(base: str, db: Path):
    server = UsdSyncServer(
        base_usd_path=base,
        log_path=str(db),
        layer_mode=LayerMode.SHARED_STAGE,
    )
    try:
        yield server
    finally:
        server.shutdown()
        server.store.close()


def _value_event(layer: Sdf.Layer, value: float) -> dict:
    source = Sdf.Layer.CreateAnonymous()
    source.TransferContent(layer)
    source.GetAttributeAtPath("/World.value").default = value
    return {
        "k": "set_sdf_spec_fields",
        "prim": "/World",
        "spec_path": "/World.value",
        "spec_kind": "attribute",
        "fields": ["default"],
        "fragment": serialize_spec_fields(
            source,
            "/World.value",
            "attribute",
            ["default"],
            stabilize_asset_paths=False,
        ),
        "removed": False,
    }


def _child_key(server: UsdSyncServer) -> str:
    graph = server.shared_layer_graph
    return next(key for key in graph.reachable_layer_keys() if key != graph.root_layer_key)


def test_restart_and_compaction_restore_exact_target_layer(tmp_path):
    base = _create_stage(tmp_path)
    db = tmp_path / "events.db"
    with _shared_server(base, db) as server:
        original_generation = server.shared_layer_graph.generation
        original_keys = set(server.shared_layer_graph.reachable_layer_keys())
        child_key = _child_key(server)
        child = server.shared_layer_graph.layer_for(child_key)
        records = server.process_txn(
            [_value_event(child, 5.0)],
            layer_key=child_key,
            client_id="test-client",
        )
        assert records[0][0]["layer_key"] == child_key
        assert child.GetAttributeAtPath("/World.value").default == 5.0
        server.compact_log()
        assert server.store.get_count() == 2
        compacted_generation = server.shared_layer_graph.generation
        assert compacted_generation != original_generation
        assert set(server.shared_layer_graph.reachable_layer_keys()) == original_keys

    with _shared_server(base, db) as restored:
        child = restored.shared_layer_graph.layer_for(_child_key(restored))
        assert child.GetAttributeAtPath("/World.value").default == 5.0
        assert set(restored.shared_layer_graph.reachable_layer_keys()) == original_keys
        assert restored.shared_layer_graph.generation == compacted_generation
        first = message_to_dict(restored.store.get_all_asc()[0][1])
        assert first["type"] == "layer_graph_state"
        assert first["seq"] == 1
        assert restored.store.get_count() == 2


def test_detached_layer_recovers_its_key_after_compaction_and_restart(tmp_path):
    base = _create_stage(tmp_path)
    db = tmp_path / "detached-identity.db"
    with _shared_server(base, db) as server:
        graph = server.shared_layer_graph
        original_child_key = _child_key(server)
        server.process_txn(
            [
                {
                    "k": "set_sublayers",
                    "prim": "/",
                    "generation": graph.generation,
                    "revision": 0,
                    "sublayers": [],
                }
            ],
            layer_key=graph.root_layer_key,
        )
        server.compact_log()
        assert original_child_key not in graph.reachable_layer_keys()

    with _shared_server(base, db) as restored:
        graph = restored.shared_layer_graph
        assert original_child_key not in graph.reachable_layer_keys()
        records = restored.process_txn(
            [
                {
                    "k": "set_sublayers",
                    "prim": "/",
                    "generation": graph.generation,
                    "revision": 0,
                    "sublayers": [{"authored_path": "./asset.usda"}],
                }
            ],
            layer_key=graph.root_layer_key,
        )

        assert records[0][0]["event"]["sublayers"][0]["layer_key"] == original_child_key
        assert original_child_key in graph.reachable_layer_keys()


def test_failed_compaction_does_not_rotate_graph_identity(tmp_path, monkeypatch):
    base = _create_stage(tmp_path)
    with _shared_server(base, tmp_path / "failed-compaction.db") as server:
        graph = server.shared_layer_graph
        generation = graph.generation
        revision = graph.revision
        parent_revisions = {
            key: graph.parent_revision(key) for key in graph.reachable_layer_keys()
        }
        rows = server.store.get_all_asc()

        def _fail(_records, **_kwargs):
            raise RuntimeError("injected shared compaction failure")

        monkeypatch.setattr(server.store, "clear_and_rewrite", _fail)

        with pytest.raises(RuntimeError, match="injected shared compaction failure"):
            server.compact_log()

        assert graph.generation == generation
        assert graph.revision == revision
        assert {
            key: graph.parent_revision(key) for key in graph.reachable_layer_keys()
        } == parent_revisions
        assert server.store.get_all_asc() == rows


def test_layer_content_replacement_supersedes_prior_deltas_during_compaction(tmp_path):
    base = _create_stage(tmp_path)
    db = tmp_path / "replacement.db"
    with _shared_server(base, db) as server:
        graph = server.shared_layer_graph
        root = graph.layer_for(graph.root_layer_key)
        server.process_txn(
            [
                {
                    "k": "set_sdf_spec_fields",
                    "prim": "/Transient",
                    "spec_path": "/Transient",
                    "spec_kind": "prim",
                    "fields": ["specifier"],
                    "fragment": '#usda 1.0\n\ndef "Transient" {}\n',
                    "removed": False,
                }
            ],
            layer_key=graph.root_layer_key,
        )
        replacement = Sdf.Layer.CreateAnonymous("replacement")
        Sdf.CreatePrimInLayer(replacement, "/Final").documentation = "complete state"
        event = {
            "k": "replace_sdf_layer_content",
            "prim": "/",
            "fragment": serialize_layer_content(replacement),
        }

        server.process_txn([event], layer_key=graph.root_layer_key)
        assert root.GetPrimAtPath("/Transient") is None
        assert root.GetPrimAtPath("/Final").documentation == "complete state"
        assert list(root.subLayerPaths) == ["./asset.usda"]
        server.compact_log()

        records = [message_to_dict(row[1]) for row in server.store.get_all_asc()]
        assert [record["type"] for record in records] == [
            "layer_graph_state",
            "event",
        ]
        assert records[1]["event"]["k"] == "replace_sdf_layer_content"

    with _shared_server(base, db) as restored:
        root = restored.shared_layer_graph.layer_for(restored.shared_layer_graph.root_layer_key)
        assert root.GetPrimAtPath("/Transient") is None
        assert root.GetPrimAtPath("/Final").documentation == "complete state"
        assert list(root.subLayerPaths) == ["./asset.usda"]


def test_generic_property_removal_supersedes_concrete_property_delta(tmp_path):
    base = _create_stage(tmp_path)
    db = tmp_path / "property-removal.db"
    with _shared_server(base, db) as server:
        child_key = _child_key(server)
        child = server.shared_layer_graph.layer_for(child_key)
        server.process_txn([_value_event(child, 5.0)], layer_key=child_key)
        server.process_txn(
            [
                {
                    "k": "set_sdf_spec_fields",
                    "prim": "/World",
                    "spec_path": "/World.value",
                    "spec_kind": "property",
                    "fields": [],
                    "fragment": "",
                    "removed": True,
                }
            ],
            layer_key=child_key,
        )
        server.compact_log()

        records = [message_to_dict(row[1]) for row in server.store.get_all_asc()]
        assert len(records) == 2
        assert records[1]["event"]["spec_kind"] == "property"
        assert records[1]["event"]["removed"] is True

    with _shared_server(base, db) as restored:
        child = restored.shared_layer_graph.layer_for(_child_key(restored))
        assert child.GetObjectAtPath("/World.value") is None


def test_new_sublayer_transaction_emits_complete_recursive_routing(tmp_path):
    leaf = _create_layer(tmp_path / "leaf.usda", "/Leaf")
    child = _create_layer(tmp_path / "child.usda")
    child.subLayerPaths.append("./leaf.usda")
    child.Save()
    root = _create_layer(tmp_path / "root.usda")
    db = tmp_path / "events.db"

    with _shared_server(root.identifier, db) as server:
        graph = server.shared_layer_graph
        event = {
            "k": "set_sublayers",
            "prim": "/",
            "generation": graph.generation,
            "revision": 0,
            "sublayers": [{"authored_path": "./child.usda", "offset": 4.0, "scale": 0.5}],
        }
        records = server.process_txn([event], layer_key=graph.root_layer_key)

        assert len(records) == 3
        assert [record[0]["event"]["revision"] for record in records] == [2, 1, 1]
        assert len({record[0]["layer_key"] for record in records}) == 3
        assert len(graph.reachable_layer_keys()) == 3
        assert graph.layer_for(graph.sublayers_for(graph.root_layer_key)[0]["layer_key"])
        assert leaf.identifier in {
            graph.layer_for(key).identifier for key in graph.reachable_layer_keys()
        }


def test_layer_strength_is_composed_by_openusd_not_event_arrival(tmp_path):
    weak = _create_layer(tmp_path / "weak.usda")
    weak_stage = Usd.Stage.Open(weak)
    UsdGeom.Sphere.Define(weak_stage, "/Sphere").GetRadiusAttr().Set(1.0)
    weak.Save()
    strong = _create_layer(tmp_path / "strong.usda")
    strong_stage = Usd.Stage.Open(strong)
    UsdGeom.Sphere.Define(strong_stage, "/Sphere").GetRadiusAttr().Set(2.0)
    strong.Save()
    root = _create_layer(tmp_path / "root.usda")
    root.subLayerPaths = ["./strong.usda", "./weak.usda"]
    root.Save()

    with _shared_server(root.identifier, tmp_path / "events.db") as server:
        graph = server.shared_layer_graph
        weak_key = next(
            key
            for key in graph.reachable_layer_keys()
            if graph.layer_for(key).identifier == weak.identifier
        )
        radius = graph.layer_for(weak_key).GetAttributeAtPath("/Sphere.radius")
        source = Sdf.Layer.CreateAnonymous()
        source.TransferContent(graph.layer_for(weak_key))
        source.GetAttributeAtPath("/Sphere.radius").default = 9.0
        event = {
            "k": "set_sdf_spec_fields",
            "prim": "/Sphere",
            "spec_path": "/Sphere.radius",
            "spec_kind": "attribute",
            "fields": ["default"],
            "fragment": serialize_spec_fields(
                source,
                "/Sphere.radius",
                "attribute",
                ["default"],
                stabilize_asset_paths=False,
            ),
            "removed": False,
        }

        server.process_txn([event], layer_key=weak_key)

        assert radius.default == 9.0
        assert UsdGeom.Sphere(server.stage.GetPrimAtPath("/Sphere")).GetRadiusAttr().Get() == 2.0


def test_modes_reject_ambiguous_layer_routing(tmp_path):
    base = _create_stage(tmp_path)
    event = {"k": "set_visibility", "prim": "/World", "visible": False}
    managed = UsdSyncServer(base_usd_path=base, log_path=str(tmp_path / "managed.db"))
    try:
        with pytest.raises(ValueError, match="arbitrary layer key"):
            managed.process_txn([event], layer_key="layer:root")
    finally:
        managed.shutdown()
        managed.store.close()

    with _shared_server(base, tmp_path / "shared.db") as shared:
        child = shared.shared_layer_graph.layer_for(_child_key(shared))
        with pytest.raises(ValueError, match="require layer_key"):
            shared.process_txn([_value_event(child, 3.0)])
        with pytest.raises(ValueError, match="unsupported shared-stage events"):
            shared.process_txn(
                [event],
                layer_key=shared.shared_layer_graph.root_layer_key,
            )
        with pytest.raises(RuntimeError, match="purge is unavailable"):
            shared.purge()


def test_shared_server_requires_a_portable_root_layer(tmp_path):
    with pytest.raises(ValueError, match="portable root layer"):
        UsdSyncServer(
            stage=Usd.Stage.CreateInMemory(),
            log_path=str(tmp_path / "events.db"),
            layer_mode=LayerMode.SHARED_STAGE,
        )


def test_detached_layer_key_cannot_receive_more_opinions(tmp_path):
    base = _create_stage(tmp_path)
    with _shared_server(base, tmp_path / "events.db") as server:
        graph = server.shared_layer_graph
        child_key = _child_key(server)
        child = graph.layer_for(child_key)
        topology = {
            "k": "set_sublayers",
            "prim": "/",
            "generation": graph.generation,
            "revision": 0,
            "sublayers": [],
        }

        server.process_txn([topology], layer_key=graph.root_layer_key)

        assert child_key not in graph.reachable_layer_keys()
        with pytest.raises(TransactionRejectedError, match="unknown or unresolved") as caught:
            server.process_txn([_value_event(child, 3.0)], layer_key=child_key)
        assert caught.value.code == "stale_layer_graph"


def test_stale_topology_generation_is_a_recoverable_rejection(tmp_path):
    base = _create_stage(tmp_path)
    with _shared_server(base, tmp_path / "events.db") as server:
        graph = server.shared_layer_graph
        topology = {
            "k": "set_sublayers",
            "prim": "/",
            "generation": "obsolete-generation",
            "revision": 0,
            "sublayers": [],
        }

        with pytest.raises(TransactionRejectedError) as caught:
            server.process_txn([topology], layer_key=graph.root_layer_key)

        assert caught.value.code == "stale_layer_graph"
        assert list(server.stage.GetRootLayer().subLayerPaths) == ["./asset.usda"]


def test_topology_that_becomes_stale_at_commit_is_rolled_back_and_recoverable(
    tmp_path, monkeypatch
):
    base = _create_stage(tmp_path)
    with _shared_server(base, tmp_path / "events.db") as server:
        graph = server.shared_layer_graph
        topology = {
            "k": "set_sublayers",
            "prim": "/",
            "generation": graph.generation,
            "revision": 0,
            "sublayers": [],
        }

        def _become_stale(_prepared):
            raise StaleLayerGraphError("layer graph changed before commit")

        monkeypatch.setattr(graph, "accept_sublayers", _become_stale)

        with pytest.raises(TransactionRejectedError) as caught:
            server.process_txn([topology], layer_key=graph.root_layer_key)

        assert caught.value.code == "stale_layer_graph"
        assert list(server.stage.GetRootLayer().subLayerPaths) == ["./asset.usda"]
        assert graph.revision == 1
        assert server.store.get_count() == 1


def test_topology_persistence_failure_rolls_back_new_layer_identity(
    tmp_path, monkeypatch
):
    _create_layer(tmp_path / "child.usda", "/Child")
    root = _create_layer(tmp_path / "root.usda")
    with _shared_server(root.identifier, tmp_path / "identity-rollback.db") as server:
        graph = server.shared_layer_graph
        identities = graph.identity_records()
        durable_identities = server.store.get_layer_identities()

        def _fail(*_args, **_kwargs):
            raise RuntimeError("injected identity commit failure")

        monkeypatch.setattr(server.store, "append_batch", _fail)

        with pytest.raises(RuntimeError, match="injected identity commit failure"):
            server.process_txn(
                [
                    {
                        "k": "set_sublayers",
                        "prim": "/",
                        "generation": graph.generation,
                        "revision": 0,
                        "sublayers": [{"authored_path": "./child.usda"}],
                    }
                ],
                layer_key=graph.root_layer_key,
            )

        assert list(server.stage.GetRootLayer().subLayerPaths) == []
        assert graph.identity_records() == identities
        assert server.store.get_layer_identities() == durable_identities
        assert server.store.get_count() == 1
        assert server._next_seq == 2


def test_concurrent_topology_revisions_keep_sequence_order(tmp_path, monkeypatch):
    _create_layer(tmp_path / "first.usda", "/First")
    _create_layer(tmp_path / "second.usda", "/Second")
    root = _create_layer(tmp_path / "root.usda")
    with _shared_server(root.identifier, tmp_path / "events.db") as server:
        graph = server.shared_layer_graph
        first_is_ready_to_persist = threading.Event()
        persist = server._persist_shared_events

        def _delayed_persist(routed_events, **kwargs):
            if routed_events[0][1]["revision"] == 2:
                first_is_ready_to_persist.set()
                time.sleep(0.05)
            return persist(routed_events, **kwargs)

        monkeypatch.setattr(server, "_persist_shared_events", _delayed_persist)

        def _replace(path: str) -> None:
            server.process_txn(
                [
                    {
                        "k": "set_sublayers",
                        "prim": "/",
                        "generation": graph.generation,
                        "revision": 0,
                        "sublayers": [{"authored_path": path}],
                    }
                ],
                layer_key=graph.root_layer_key,
            )

        first = threading.Thread(target=_replace, args=("./first.usda",))
        second = threading.Thread(target=_replace, args=("./second.usda",))
        first.start()
        assert first_is_ready_to_persist.wait(timeout=1)
        second.start()
        first.join(timeout=2)
        second.join(timeout=2)
        assert not first.is_alive()
        assert not second.is_alive()

        records = [message_to_dict(row[1]) for row in server.store.get_all_asc()[1:]]
        assert [record["event"]["revision"] for record in records] == [2, 1, 3, 1]


def test_compaction_preserves_relative_asset_text(tmp_path):
    base = _create_stage(tmp_path)
    db = tmp_path / "events.db"
    with _shared_server(base, db) as server:
        root_key = server.shared_layer_graph.root_layer_key
        source = Sdf.Layer.CreateAnonymous()
        prim = Sdf.CreatePrimInLayer(source, "/Material")
        attr = Sdf.AttributeSpec(prim, "inputs:file", Sdf.ValueTypeNames.Asset)
        attr.default = Sdf.AssetPath("./textures/albedo.exr")
        event = {
            "k": "set_sdf_spec_fields",
            "prim": "/Material",
            "spec_path": "/Material.inputs:file",
            "spec_kind": "attribute",
            "fields": ["custom", "default", "typeName", "variability"],
            "fragment": serialize_spec_fields(
                source,
                "/Material.inputs:file",
                "attribute",
                attr.ListInfoKeys(),
                stabilize_asset_paths=False,
            ),
            "removed": False,
        }
        server.process_txn([event], layer_key=root_key)
        server.compact_log()
        compacted = message_to_dict(server.store.get_all_asc()[1][1])
        assert "@./textures/albedo.exr@" in compacted["event"]["fragment"]


def test_server_refresh_publishes_newly_resolved_layer_keys(tmp_path):
    root = _create_layer(tmp_path / "root.usda")
    root.subLayerPaths.append("./late.usda")
    root.Save()
    with _shared_server(root.identifier, tmp_path / "events.db") as server:
        graph = server.shared_layer_graph
        assert len(graph.reachable_layer_keys()) == 1
        assert server.store.get_count() == 1
        _create_layer(tmp_path / "late.usda", "/Late")

        mapped = server.refresh_shared_layer_dependencies()

        assert len(mapped) == 1
        assert len(graph.reachable_layer_keys()) == 2
        assert server.store.get_count() == 3
        records = [message_to_dict(row[1]) for row in server.store.get_all_asc()[1:]]
        assert [record["event"]["revision"] for record in records] == [2, 1]
        assert records[0]["event"]["sublayers"][0]["authored_path"] == "./late.usda"
