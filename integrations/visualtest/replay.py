"""Replay a curated event log onto a base stage through the real apply pipeline.

Fixtures are stored as JSONL (one semantic event dict per line), not as a binary
event-store db: that keeps the fixture decoupled from the FlatBuffers wire format
and the SQLite schema, so a protocol/storage change can't rot it (regenerate the
JSONL, never re-record a binary). :func:`reconstruct` still round-trips the
events through the *current* codec, so encode/decode stays under test.
"""

from __future__ import annotations

import json


def load_events(path: str) -> list[dict]:
    """Load a JSONL event log -- one semantic event dict per line."""
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def reconstruct(base_path: str, events: list[dict], *, edit_session: bool = True,
                via_codec: bool = True):
    """Apply ``events`` onto ``base_path`` through the receive pipeline.

    With ``via_codec`` (default) the events are first round-tripped through the
    FlatBuffers wire codec, so the fixture exercises encode/decode as well as
    ``apply_events``. Edits a transient session layer so ``base_path`` is left
    untouched. Returns the reconstructed ``Usd.Stage``.

    Callers targeting hdPrman must bootstrap RenderMan first (apply resolves
    shader port types through the Sdr registry).
    """
    from pxr import Usd

    from openusdconnect.event_apply import apply_events

    if via_codec:
        from openusdconnect.codec import encode_message, message_to_dict
        from openusdconnect.protocol import make_txn

        events = message_to_dict(encode_message(make_txn("visualtest", events)))["events"]

    stage = Usd.Stage.Open(base_path)
    if edit_session:
        stage.SetEditTarget(stage.GetSessionLayer())
    apply_events(stage, events)
    return stage
