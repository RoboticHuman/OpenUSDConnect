"""Drift guard: every replicated event kind must be fully wired.

Adding an event kind touches several parallel registration sites. Missing one
fails silently at runtime (e.g. a kind with no encoder/decoder/applier, or
absent from the adapter dispatch table). These tests turn that into a loud
CI failure instead.

Note: the dispatcher categorization frozensets (STAGE_SYNC_KINDS, ARC_KINDS,
IMPORT_KINDS) and the emitter's _INVALIDATE_DISPATCH are intentionally partial
(only kinds with that property), so they are NOT asserted here.
"""

from __future__ import annotations

from openusdconnect import events
from openusdconnect.adapters import _DISPATCH
from openusdconnect.protocol_constants import EVENT_KEYS


def test_every_event_kind_has_encoder_decoder_applier():
    for kind in sorted(EVENT_KEYS):
        spec = events.get(kind)
        assert spec is not None, f"{kind}: no EventSpec registered"
        assert spec.encode is not None, f"{kind}: missing @register_encoder"
        assert spec.decode is not None, f"{kind}: missing @register_decoder"
        assert spec.apply is not None, f"{kind}: missing @register_applier"
        assert spec.fb_tag is not None, f"{kind}: missing fb_tag"
        assert spec.fb_class is not None, f"{kind}: missing fb_class"


def test_every_event_kind_has_adapter_dispatch():
    for kind in sorted(EVENT_KEYS):
        assert kind in _DISPATCH, f"{kind}: missing from adapters._DISPATCH"


def test_no_orphan_registrations_outside_event_keys():
    for spec in events.all_specs():
        assert spec.kind in EVENT_KEYS, f"{spec.kind}: registered but not in EVENT_KEYS"
    for kind in _DISPATCH:
        assert kind in EVENT_KEYS, f"{kind}: in _DISPATCH but not in EVENT_KEYS"
