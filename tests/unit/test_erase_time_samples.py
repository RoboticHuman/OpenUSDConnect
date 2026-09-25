"""Targeted sample erasure: wire contract, application, projection, and replay."""

import pytest
from pxr import Sdf, Usd, UsdGeom

from openusdconnect.codec import encode_message, message_to_dict
from openusdconnect.composed_projection import ComposedChangeProjection
from openusdconnect.event_apply import (
    apply_event,
    apply_events,
    atomic_apply,
    atomic_apply_prim_paths,
)
from openusdconnect.protocol_validation import validate_event_or_raise
from openusdconnect.sdf_spec_delta import serialize_spec_fields
from openusdconnect.server.compaction import LogCompaction
from openusdconnect.server.maintenance import HistoryMaintenance
from openusdconnect.time_sample_delta import erase_time_samples_event


def _erase(*times, path="/Cube.size"):
    return erase_time_samples_event(Sdf.Path(path), times)


def _write(time, **attrs):
    return {"k": "set_gprim_attrs", "prim": "/Cube", "time": time, "attrs": attrs}


def _encode(event, seq=1, layer_key="test"):
    return encode_message({"type": "event", "seq": seq, "event": event, "layer_key": layer_key})


def _stage():
    stage = Usd.Stage.CreateInMemory()
    attr = UsdGeom.Cube.Define(stage, "/Cube").GetSizeAttr()
    attr.Set(10.0)
    attr.SetDocumentation("Keep me.")
    for time in (1.0, 2.0, 3.0):
        attr.Set(time, time)
    return stage, attr


def test_erase_roundtrip_and_idempotent_application():
    stage, attr = _stage()
    event = _erase(1.0, 3.0)
    validate_event_or_raise(event)
    decoded = message_to_dict(_encode(event))["event"]
    assert decoded == event
    apply_events(stage, [decoded, decoded])
    assert attr.GetTimeSamples() == [2.0]
    assert attr.Get(2.0) == 2.0
    assert attr.Get() == 10.0
    assert attr.GetDocumentation() == "Keep me."
    apply_events(stage, [_erase(2.0), _erase(2.0, path="/Missing.attr")])
    assert attr.GetTimeSamples() == []
    assert not stage.GetPrimAtPath("/Missing")


@pytest.mark.parametrize(
    "changes",
    [
        {"times": []},
        {"times": [True]},
        {"times": [float("nan")]},
        {"times": [float("inf")]},
        {"times": ["1"]},
        {"times": None},
        {"spec_path": "/Cube"},
        {"spec_path": "Cube.size"},
        {"spec_path": "/Cube.rel[/Target]"},
        {"prim": "/Other"},
    ],
)
def test_erase_rejects_invalid_payload(changes):
    with pytest.raises(ValueError):
        validate_event_or_raise({**_erase(1.0), **changes})
    stage, _ = _stage()
    before = stage.GetRootLayer().ExportToString()
    with pytest.raises(ValueError):
        apply_event(stage, {**_erase(1.0), **changes})
    assert stage.GetRootLayer().ExportToString() == before


def test_erase_preserves_batch_order_and_atomic_rollback():
    stage, attr = _stage()
    apply_events(stage, [_write(1.0, size=4.0), _erase(1.0), _write(1.0, size=5.0)])
    assert attr.Get(1.0) == 5.0
    apply_events(stage, [_write(1.0, size=6.0), _erase(1.0)])
    assert attr.GetTimeSamples() == [2.0, 3.0]
    events = [_erase(2.0, 3.0)]
    before = stage.GetRootLayer().ExportToString()
    with pytest.raises(RuntimeError):
        with atomic_apply(stage, prim_paths=atomic_apply_prim_paths(events)):
            apply_events(stage, events)
            raise RuntimeError("rollback")
    assert stage.GetRootLayer().ExportToString() == before


def test_erase_projects_the_new_composed_value_at_the_deleted_time():
    stage, attr = _stage()
    events = [_erase(1.0)]
    with ComposedChangeProjection(stage, events) as projection:
        apply_events(stage, events)
        projected = projection.build_events()
    assert not any(event["k"] == "erase_time_samples" for event in projected)
    assert any(
        event["k"] == "set_gprim_attrs"
        and event.get("time") == 1.0
        and event["attrs"].get("size") == attr.Get(1.0) == 2.0
        for event in projected
    )


def _spec_event(layer, fields, removed=False):
    return {
        "k": "set_sdf_spec_fields",
        "prim": "/Cube",
        "spec_path": "/Cube.size",
        "spec_kind": "attribute",
        "fields": fields,
        "fragment": ""
        if removed
        else serialize_spec_fields(
            layer, Sdf.Path("/Cube.size"), "attribute", fields, stabilize_asset_paths=False
        ),
        "removed": removed,
    }


@pytest.mark.parametrize("exact_samples", [False, True])
def test_compaction_does_not_move_partial_writes_across_erasure(exact_samples):
    source, attr = _stage()
    events = [{"k": "ensure_prim", "prim": "/Cube", "typeName": "Cube"}]
    if exact_samples:
        events.append(_spec_event(source.GetRootLayer(), ["timeSamples", "default"]))
    else:
        events.extend(_write(time, size=time) for time in (1.0, 2.0, 3.0))
    events.extend(
        [
            _write(1.0, extent=[[-1.0] * 3, [1.0] * 3]),
            _erase(1.0),
            _write(1.0, extent=[[-2.0] * 3, [2.0] * 3]),
            _erase(2.0),
            _write(2.0, size=20.0),
            _erase(3.0),
        ]
    )
    attr.SetDocumentation("After deletion.")
    events.append(_spec_event(source.GetRootLayer(), ["documentation"]))
    expected = Usd.Stage.CreateInMemory()
    apply_events(expected, events)
    rows = [(seq, _encode(event, seq)) for seq, event in enumerate(events, 1)]
    for _ in range(3):
        compacted = HistoryMaintenance.build_compacted(rows)
        replay = [entry.event for entry in compacted.replay_records()]
        target = Usd.Stage.CreateInMemory()
        apply_events(target, replay)
        assert target.GetRootLayer().ExportToString() == expected.GetRootLayer().ExportToString()
        rows = [(seq, _encode(event, seq)) for seq, event in enumerate(replay, 1)]


def test_compaction_keeps_erasures_in_their_own_layer():
    rows = [
        (1, _encode(_write(1.0, size=1.0), 1, "weak")),
        (2, _encode(_write(1.0, size=2.0), 2, "strong")),
        (3, _encode(_erase(1.0), 3, "strong")),
        (4, _encode(_write(1.0, extent=[[-1.0] * 3, [1.0] * 3]), 4, "strong")),
    ]
    entries = HistoryMaintenance.build_compacted(rows).replay_records()
    for layer_key, expected_times in (("weak", [1.0]), ("strong", [])):
        target = Usd.Stage.CreateInMemory()
        UsdGeom.Cube.Define(target, "/Cube")
        apply_events(
            target, [entry.event for entry in entries if entry.layer_key == layer_key]
        )
        assert target.GetAttributeAtPath("/Cube.size").GetTimeSamples() == expected_times


def test_compaction_still_collapses_repeated_exact_table_writes():
    source, attr = _stage()
    rows = []
    for seq in range(1, 5):
        attr.Set(float(seq), 1.0)
        rows.append((seq, _encode(_spec_event(source.GetRootLayer(), ["timeSamples"]), seq)))
    entries = HistoryMaintenance.build_compacted(rows).replay_records()
    assert len(entries) == 1
    target = Usd.Stage.CreateInMemory()
    apply_events(target, [entries[0].event])
    assert target.GetAttributeAtPath("/Cube.size").Get(1.0) == 4.0


def test_compaction_does_not_move_exact_tables_past_later_sample_writes():
    source, _ = _stage()
    events = [
        _spec_event(source.GetRootLayer(), ["timeSamples"]),
        _write(1.0, size=42.0),
        _spec_event(source.GetRootLayer(), ["documentation"]),
    ]
    rows = [(seq, _encode(event, seq)) for seq, event in enumerate(events, 1)]
    entries = HistoryMaintenance.build_compacted(rows).replay_records()
    target = Usd.Stage.CreateInMemory()
    apply_events(target, [entry.event for entry in entries])
    assert target.GetAttributeAtPath("/Cube.size").Get(1.0) == 42.0


@pytest.mark.parametrize("kind", ["delete_prim", "rename_prim"])
def test_subtree_removal_discards_preserved_history_only_in_its_layer(kind):
    compaction = LogCompaction()
    source, _ = _stage()
    exact = _spec_event(source.GetRootLayer(), ["timeSamples"])
    records = [
        (exact, "weak"),
        (exact, "strong"),
        (_erase(1.0), "weak"),
        (_erase(1.0), "strong"),
        ({"k": kind, "prim": "/Cube", "new_name": "Renamed"}, "strong"),
    ]
    for seq, (event, layer) in enumerate(records, 1):
        compaction.add_record(seq, _encode(event, seq, layer))
    entries = compaction.replay_records()
    assert [entry.event["k"] for entry in entries if entry.layer_key == "strong"] == [
        kind
    ]
    assert [entry.event["k"] for entry in entries if entry.layer_key == "weak"] == [
        "set_sdf_spec_fields",
        "erase_time_samples",
    ]


def test_layer_replacement_discards_preserved_sample_history():
    compaction = LogCompaction()
    source, _ = _stage()
    events = [
        _spec_event(source.GetRootLayer(), ["timeSamples"]),
        _erase(1.0),
        {"k": "replace_sdf_layer_content", "prim": "/", "fragment": "#usda 1.0\n"},
    ]
    for seq, event in enumerate(events, 1):
        compaction.add_record(seq, _encode(event, seq))
    assert [entry.event for entry in compaction.replay_records()] == events[-1:]
