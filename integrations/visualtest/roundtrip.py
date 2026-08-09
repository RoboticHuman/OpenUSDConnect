"""Mode-1 round-trip: rebuild a stage by routing it through openusdconnect.

The renderer-invariant fidelity test. A source stage is serialized to events by
the emitter, encoded and decoded across the FlatBuffers wire codec, and applied
to a fresh stage by the receive-side apply engine -- exactly the emit -> wire ->
apply pipeline the framework replicates. Rendering the source and the rebuilt
stage with the same delegate and FLIP-comparing them isolates *replication*
fidelity from any renderer difference: a non-zero result means openusdconnect
dropped or altered something visible.

No DCC required -- this exercises the core library end to end on its own.
"""

from __future__ import annotations


def roundtrip_stage(source_path: str):
    """Rebuild ``source_path`` through openusdconnect; return (source, rebuilt, n_events).

    Routes the stage through ``snapshot_events -> make_txn -> encode_message ->
    message_to_dict -> apply_events`` so the rebuilt stage reflects whatever the
    real emit/wire/apply path preserves.
    """
    from pxr import Usd, UsdGeom

    from openusdconnect.codec import encode_message, message_to_dict
    from openusdconnect.emitter import NoticeEmitter
    from openusdconnect.event_apply import apply_events
    from openusdconnect.protocol import make_txn

    source = Usd.Stage.Open(source_path)
    events = NoticeEmitter(source).snapshot_events()
    decoded = message_to_dict(encode_message(make_txn(events)))

    rebuilt = Usd.Stage.CreateInMemory()
    # Stage metadata travels out-of-band (hello_ok) in the live flow, not in the
    # per-prim snapshot; seed it so up-axis/units parity holds for the render.
    UsdGeom.SetStageUpAxis(rebuilt, UsdGeom.GetStageUpAxis(source))
    UsdGeom.SetStageMetersPerUnit(rebuilt, UsdGeom.GetStageMetersPerUnit(source))
    apply_events(rebuilt, decoded["events"])
    return source, rebuilt, len(events)
