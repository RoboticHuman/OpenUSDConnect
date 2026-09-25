"""Payload reload preserves partial opinions, authored layers, and samples."""

import numpy as np
import pytest

from openusdconnect.codec import message_to_dict
from openusdconnect.server import UsdSyncServer
from openusdconnect.server.compaction import child_replay_records


@pytest.fixture
def server():
    server = UsdSyncServer(log_path=":memory:")
    try:
        yield server
    finally:
        server.shutdown()
        server.store.close()


def test_child_replay_preserves_latest_opinion_in_each_layer(server):
    for department, values in (("layout", (1, 2)), ("animation", (3, 4))):
        layer = server.get_or_create_client_layer(department, department)
        server._commit_events([
            {"k": "ensure_prim", "prim": "/Payload/Child", "typeName": "Xform"},
            {"k": "ensure_xform_ops", "prim": "/Payload/Child"},
            *({"k": "set_xform_trs", "prim": "/Payload/Child", "fields": ["t"],
               "t": [value, 0, 0]} for value in values),
        ], client_id=department, origin=department, layer=layer)

    previous_head = server.store.get_max_seq()
    server.replay_children_after_load("/Payload")
    replayed = [message_to_dict(blob)
                for _, blob in server.store.get_from_seq_asc(previous_head + 1)]
    assert len(replayed) == 6
    assert [record["event"]["k"] for record in replayed] == [
        "ensure_prim", "ensure_prim", "ensure_xform_ops", "ensure_xform_ops",
        "set_xform_trs", "set_xform_trs",
    ]
    assert [(record["layer_key"], record["origin"], record["event"]["t"])
            for record in replayed if record["event"]["k"] == "set_xform_trs"] == [
        ("department:layout", "layout", [2, 0, 0]),
        ("department:animation", "animation", [4, 0, 0]),
    ]


def test_child_replay_reduces_shader_arrays_and_excludes_non_descendants(server):
    for i in range(100):
        server.append_log({
            "type": "event", "seq": server.assign_seq(), "layer_key": "default",
            "event": {"k": "set_connectable_input", "prim": "/Payload/Child",
                      "info_id": "Shader", "inputs": {"weights": [float(i)] * 16},
                      "input_types": {"weights": "float[]"}},
        })
    for path in ("/Payload", "/PayloadSibling/Child"):
        server.append_log({
            "type": "event", "seq": server.assign_seq(), "layer_key": "default",
            "event": {"k": "ensure_prim", "prim": path, "typeName": "Xform"},
        })

    records = child_replay_records(server.store, "/Payload")
    assert len(records) == 1
    values = records[0].event["inputs"]["weights"]
    np.testing.assert_array_equal(values, [99.0] * 16)
    assert not values.flags.owndata


def _replay_events(server, events):
    for event in events:
        server.append_log({
            "type": "event", "seq": server.assign_seq(), "layer_key": "default",
            "event": {"prim": "/Payload/Child", **event},
        })
    return [record.event for record in child_replay_records(server.store, "/Payload")]


def test_child_replay_merges_partial_transforms_per_time_sample(server):
    events = _replay_events(server, [
        {"k": "set_xform_trs", "fields": ["t"], "t": [1, 2, 3]},
        {"k": "set_xform_trs", "fields": ["r"], "r": [1, 0, 0, 0]},
        {"k": "set_xform_trs", "fields": ["t"], "t": [4, 5, 6], "time": 1},
        {"k": "set_xform_trs", "fields": ["t"], "t": [7, 8, 9], "time": 2},
        {"k": "set_xform_trs", "fields": ["s"], "s": [2, 2, 2], "time": 1},
    ])
    by_time = {event.get("time"): event for event in events}
    assert len(events) == 3
    assert by_time[None]["fields"] == ["t", "r"]
    assert by_time[None]["t"] == [1, 2, 3]
    assert by_time[None]["r"] == [1, 0, 0, 0]
    assert by_time[1]["fields"] == ["t", "s"]
    assert by_time[1]["t"] == [4, 5, 6]
    assert by_time[1]["s"] == [2, 2, 2]
    assert by_time[2]["t"] == [7, 8, 9]


def test_child_replay_keeps_latest_envelope_when_merging(server):
    for sequence, update in enumerate([
        {"fields": ["t"], "t": [1, 2, 3]},
        {"fields": ["r"], "r": [1, 0, 0, 0]},
    ], start=1):
        record = {"type": "event", "seq": sequence, "layer_key": "department:layout",
                  "event": {"k": "set_xform_trs", "prim": "/Payload/Child", **update}}
        if sequence == 1:
            record.update(origin="artist", client_id="first-client", client="first-address")
        server.append_log(record)
    records = child_replay_records(server.store, "/Payload")
    assert len(records) == 1
    latest = records[0]
    assert latest.seq == 2
    assert latest.layer_key == "department:layout"
    assert latest.origin is latest.client_id is latest.client is None
    assert latest.event["fields"] == ["t", "r"]
    assert latest.event["t"] == [1, 2, 3]
    assert latest.event["r"] == [1, 0, 0, 0]


def test_child_replay_merges_partial_shader_inputs_per_time_sample(server):
    events = _replay_events(server, [
        {"k": "set_connectable_input", "info_id": "UsdPreviewSurface",
         "inputs": {"roughness": 0.25}, "input_types": {"roughness": "float"}},
        {"k": "set_connectable_input", "info_id": "",
         "inputs": {"metallic": 0.5}, "input_types": {"metallic": "float"}},
        *({"k": "set_connectable_input", "info_id": "", "time": time,
           "inputs": {"roughness": value}, "input_types": {"roughness": "float"}}
          for time, value in ((1, 0.125), (2, 0.75))),
    ])
    by_time = {event.get("time"): event for event in events}
    assert len(events) == 3
    assert by_time[None]["info_id"] == "UsdPreviewSurface"
    assert by_time[None]["inputs"] == {"roughness": 0.25, "metallic": 0.5}
    assert by_time[None]["input_types"] == {"roughness": "float", "metallic": "float"}
    assert by_time[1]["inputs"] == {"roughness": 0.125}
    assert by_time[2]["inputs"] == {"roughness": 0.75}


def test_child_replay_keeps_material_purposes_separate(server):
    events = _replay_events(server, [
        {"k": "set_material_binding", "material_path": material, "material_purpose": purpose}
        for purpose, material in (("preview", "/Preview"), ("full", "/Full"), ("preview", "/New"))
    ])
    assert {event["material_purpose"]: event["material_path"] for event in events} == {
        "preview": "/New", "full": "/Full",
    }


def test_child_replay_merges_connections_and_disconnects(server):
    source = {"source_prim": "/Payload/Source", "source_attr": "outputs:out"}
    events = _replay_events(server, [
        {"k": "set_connectable_connection", "connections": {"inputs:a": source}},
        {"k": "set_connectable_connection", "connections": {"inputs:b": source}},
        {"k": "set_connectable_connection", "connections": {}, "disconnections": ["inputs:a"]},
        {"k": "ensure_prim", "prim": "/Payload/Source", "typeName": "Shader"},
    ])
    assert events[0]["k"] == "ensure_prim"
    assert events[1]["connections"] == {"inputs:a": source, "inputs:b": source}
    assert events[1]["disconnections"] == ["inputs:a"]
