"""Portable shared-stage layer topology and resolver anchoring."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pxr import Ar, Sdf, Usd

from openusdconnect.shared_layer_graph import (
    SharedLayerGraph,
    StaleLayerGraphError,
    apply_sublayer_entries,
    normalize_sublayer_entries,
)


def _create_layer(path: Path, prim_path: str | None = None) -> Sdf.Layer:
    layer = Sdf.Layer.CreateNew(str(path))
    if prim_path:
        Sdf.CreatePrimInLayer(layer, prim_path)
    layer.Save()
    return layer


def _create_equivalent_stage(directory: Path) -> Usd.Stage:
    _create_layer(directory / "asset.usda", "/Asset")
    root = _create_layer(directory / "scene.usda")
    root.subLayerPaths.append("./asset.usda")
    root.subLayerOffsets[0] = Sdf.LayerOffset(7.0, 2.0)
    root.Save()
    return Usd.Stage.Open(root.identifier)


def test_baseline_maps_equivalent_graphs_without_transmitting_local_paths(tmp_path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    source = _create_equivalent_stage(source_dir)
    target = _create_equivalent_stage(target_dir)

    authoritative = SharedLayerGraph(source, authoritative=True)
    message = authoritative.state_message(seq=1)
    receiver = SharedLayerGraph(target)
    receiver.apply_state(message)

    assert len(receiver.reachable_layer_keys()) == 2
    assert all(receiver.layer_for(key) is not None for key in receiver.reachable_layer_keys())
    assert str(source_dir) not in repr(message)
    assert str(target_dir) not in repr(message)
    root_entry = receiver.sublayers_for(receiver.root_layer_key)[0]
    assert root_entry["authored_path"] == "./asset.usda"
    assert (root_entry["offset"], root_entry["scale"]) == (7.0, 2.0)


def test_nonanchored_sublayers_use_each_stage_resolver_context(tmp_path):
    def _stage(directory: Path) -> Usd.Stage:
        root_dir = directory / "root"
        search_dir = directory / "search"
        root_dir.mkdir(parents=True)
        search_dir.mkdir()
        _create_layer(search_dir / "asset.usda", "/Asset")
        root = _create_layer(root_dir / "scene.usda")
        root.subLayerPaths.append("asset.usda")
        root.Save()
        context = Ar.DefaultResolverContext([str(search_dir)])
        return Usd.Stage.Open(root.identifier, context)

    source = _stage(tmp_path / "source")
    target = _stage(tmp_path / "target")
    authoritative = SharedLayerGraph(source, authoritative=True)
    receiver = SharedLayerGraph(target)

    receiver.apply_state(authoritative.state_message(seq=1))

    child_key = receiver.sublayers_for(receiver.root_layer_key)[0]["layer_key"]
    resolved = str(receiver.layer_for(child_key).resolvedPath)
    assert resolved.endswith(os.path.join("target", "search", "asset.usda"))


def test_alias_paths_share_one_layer_key(tmp_path):
    asset = _create_layer(tmp_path / "asset.usda", "/Asset")
    root = _create_layer(tmp_path / "scene.usda")
    root.subLayerPaths = ["./asset.usda", "asset.usda"]
    root.Save()
    graph = SharedLayerGraph(Usd.Stage.Open(root.identifier), authoritative=True)

    entries = graph.sublayers_for(graph.root_layer_key)
    assert entries[0]["layer_key"] == entries[1]["layer_key"]
    assert graph.layer_for(entries[0]["layer_key"]).identifier == asset.identifier
    assert len(graph.reachable_layer_keys()) == 2


def test_duplicate_authored_paths_and_non_finite_offsets_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        normalize_sublayer_entries(
            [
                {"authored_path": "same.usda"},
                {"authored_path": "same.usda"},
            ]
        )
    with pytest.raises(ValueError, match="finite"):
        normalize_sublayer_entries([{"authored_path": "asset.usda", "offset": float("nan")}])
    with pytest.raises(ValueError, match="portable asset identifiers"):
        normalize_sublayer_entries([{"authored_path": Sdf.Layer.CreateAnonymous().identifier}])
    for local_path in (
        "/show/assets/asset.usda",
        "C:/show/assets/asset.usda",
        r"\\server\share\asset.usda",
        "file:///show/assets/asset.usda",
        "/show/assets/package.usdz[asset.usda]",
    ):
        with pytest.raises(ValueError, match="portable asset identifiers"):
            normalize_sublayer_entries([{"authored_path": local_path}])

    assert normalize_sublayer_entries(
        [
            {"authored_path": "./asset.usda"},
            {"authored_path": "search-path.usda"},
            {"authored_path": "studio://show/assets/asset.usda"},
        ]
    )

    assert normalize_sublayer_entries(
        [{"authored_path": "asset.usda", "offset": -3.0, "scale": 0.0}]
    )[0] == {"authored_path": "asset.usda", "offset": -3.0, "scale": 0.0}

    root = Sdf.Layer.CreateAnonymous()
    root.subLayerPaths.append(Sdf.Layer.CreateAnonymous().identifier)
    with pytest.raises(ValueError, match="portable asset identifiers"):
        SharedLayerGraph(Usd.Stage.Open(root), authoritative=True)


def test_new_sublayer_recursively_declares_descendant_keys(tmp_path):
    server_dir = tmp_path / "server"
    client_dir = tmp_path / "client"
    server_dir.mkdir()
    client_dir.mkdir()

    def _stage(directory: Path) -> Usd.Stage:
        _create_layer(directory / "leaf.usda", "/Leaf")
        child = _create_layer(directory / "child.usda")
        child.subLayerPaths.append("./leaf.usda")
        child.Save()
        return Usd.Stage.Open(_create_layer(directory / "root.usda").identifier)

    server_stage = _stage(server_dir)
    client_stage = _stage(client_dir)
    server = SharedLayerGraph(server_stage, authoritative=True)
    client = SharedLayerGraph(client_stage)
    client.apply_state(server.state_message(seq=1))

    request = {
        "k": "set_sublayers",
        "prim": "/",
        "generation": server.generation,
        "revision": server.parent_revision(server.root_layer_key),
        "sublayers": [{"authored_path": "./child.usda", "offset": 3.0, "scale": -2.0}],
    }
    prepared = server.canonicalize_sublayers(server.root_layer_key, request)
    apply_sublayer_entries(server_stage.GetRootLayer(), prepared.event["sublayers"])
    server.accept_sublayers(prepared)
    records = [(server.root_layer_key, prepared.event)]
    records.extend(server.discover_sublayer_states(prepared.mappings))

    assert len(records) == 3
    assert [event["revision"] for _key, event in records] == [2, 1, 1]
    for layer_key, event in records:
        client.apply_sublayers(layer_key, event)

    assert len(client.reachable_layer_keys()) == 3
    assert all(client.layer_for(key) is not None for key in client.reachable_layer_keys())
    assert client.sublayers_for(client.root_layer_key)[0]["scale"] == -2.0


def test_late_dependency_gets_keys_without_changing_authored_path(tmp_path):
    root = _create_layer(tmp_path / "root.usda")
    root.subLayerPaths.append("./late.usda")
    root.Save()
    stage = Usd.Stage.Open(root.identifier)
    graph = SharedLayerGraph(stage, authoritative=True)

    assert "layer_key" not in graph.sublayers_for(graph.root_layer_key)[0]
    _create_layer(tmp_path / "late.usda", "/Late")

    records = graph.refresh_resolved_sublayers()

    assert len(records) == 2
    assert records[0][1]["sublayers"][0]["authored_path"] == "./late.usda"
    assert len(graph.reachable_layer_keys()) == 2


def test_cycles_are_finite_and_preserve_both_edges(tmp_path):
    first = _create_layer(tmp_path / "first.usda")
    second = _create_layer(tmp_path / "second.usda")
    first.subLayerPaths.append("./second.usda")
    second.subLayerPaths.append("./first.usda")
    first.Save()
    second.Save()

    graph = SharedLayerGraph(Usd.Stage.Open(first.identifier), authoritative=True)
    keys = graph.reachable_layer_keys()

    assert len(keys) == 2
    assert graph.sublayers_for(keys[0])[0]["layer_key"] == keys[1]
    assert graph.sublayers_for(keys[1])[0]["layer_key"] == keys[0]


def test_prepared_topology_edits_on_different_parents_do_not_conflict(tmp_path):
    child = _create_layer(tmp_path / "child.usda")
    root = _create_layer(tmp_path / "root.usda")
    root.subLayerPaths.append("./child.usda")
    root.Save()
    graph = SharedLayerGraph(Usd.Stage.Open(root.identifier), authoritative=True)
    child_key = next(
        key for key in graph.reachable_layer_keys() if key != graph.root_layer_key
    )

    root_edit = graph.canonicalize_sublayers(
        graph.root_layer_key,
        graph.describe_sublayers(graph.stage.GetRootLayer()),
    )
    child_edit = graph.canonicalize_sublayers(
        child_key,
        graph.describe_sublayers(child),
    )

    graph.accept_sublayers(child_edit)
    graph.accept_sublayers(root_edit)

    assert graph.parent_revision(graph.root_layer_key) == 2
    assert graph.parent_revision(child_key) == 2
    assert graph.revision == 3


def test_prepared_topology_edits_on_the_same_parent_still_conflict(tmp_path):
    root = _create_layer(tmp_path / "root.usda")
    graph = SharedLayerGraph(Usd.Stage.Open(root.identifier), authoritative=True)
    event = graph.describe_sublayers(graph.stage.GetRootLayer())
    first = graph.canonicalize_sublayers(graph.root_layer_key, event)
    second = graph.canonicalize_sublayers(graph.root_layer_key, event)

    graph.accept_sublayers(first)

    with pytest.raises(StaleLayerGraphError, match="parent topology revision"):
        graph.accept_sublayers(second)


def test_stale_base_parent_revision_is_rejected_before_canonicalization(tmp_path):
    root = _create_layer(tmp_path / "root.usda")
    graph = SharedLayerGraph(Usd.Stage.Open(root.identifier), authoritative=True)
    event = graph.describe_sublayers(graph.stage.GetRootLayer())
    first = graph.canonicalize_sublayers(graph.root_layer_key, event)
    graph.accept_sublayers(first)

    with pytest.raises(StaleLayerGraphError, match="base parent topology revision"):
        graph.canonicalize_sublayers(graph.root_layer_key, event)


def test_new_generation_preserves_keys_and_resets_parent_revisions(tmp_path):
    stage = _create_equivalent_stage(tmp_path)
    graph = SharedLayerGraph(stage, authoritative=True)
    previous_generation = graph.generation
    previous_keys = graph.reachable_layer_keys()

    graph.start_new_generation()

    assert graph.generation != previous_generation
    assert graph.reachable_layer_keys() == previous_keys
    assert graph.revision == 1
    assert {graph.parent_revision(key) for key in previous_keys} == {1}
