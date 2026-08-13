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
from openusdconnect.emitter import (
    _EVENTS_ALREADY_PROVEN_LOCAL,
    _EVENTS_REQUIRING_LOCAL_OPINION_FILTERING,
)
from openusdconnect.protocol_constants import (
    ARC_KINDS,
    CREATE_KINDS,
    EVENT_KEYS,
    IMPORT_KINDS,
    MANAGED_KINDS,
    NATIVE_DIRECT_KINDS,
    NATIVE_FIELD_ROUTED_KINDS,
    NATIVE_PROJECTED_KINDS,
    SHARED_STAGE_EVENT_KINDS,
    SHARED_STAGE_ONLY_KINDS,
    STAGE_SYNC_KINDS,
    STRUCTURAL_EVENT_KINDS,
)


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


def test_derived_kind_sets_pin():
    """Pin the sets derived from EVENT_KIND_INFO. A diff here means a flag
    was flipped in the declaration table; update deliberately."""
    assert CREATE_KINDS == {"ensure_prim", "set_stage_metadata"}
    assert STRUCTURAL_EVENT_KINDS == {
        "ensure_prim",
        "ensure_xform_ops",
        "load_payload",
        "replace_sdf_layer_content",
        "set_connectable_connection",
        "set_connectable_input",
        "set_instanceable",
        "set_material_binding",
        "set_payload",
        "set_reference",
        "set_sdf_spec_fields",
        "set_stage_metadata",
        "set_sublayers",
        "set_variant_selections",
        "unload_payload",
    }
    assert STAGE_SYNC_KINDS == {
        "load_payload",
        "replace_sdf_layer_content",
        "set_connectable_connection",
        "set_connectable_input",
        "set_instanceable",
        "set_material_binding",
        "set_payload",
        "set_reference",
        "set_sdf_spec_fields",
        "set_stage_metadata",
        "set_sublayers",
        "set_variant_selections",
        "unload_payload",
    }
    assert ARC_KINDS == {"set_payload", "set_reference", "set_variant_selections"}
    assert IMPORT_KINDS == {"load_payload", "set_reference"}
    assert NATIVE_DIRECT_KINDS == {
        "load_payload",
        "replace_sdf_layer_content",
        "set_stage_metadata",
        "set_sublayers",
        "unload_payload",
    }
    assert NATIVE_FIELD_ROUTED_KINDS == {"set_sdf_spec_fields"}
    assert (NATIVE_PROJECTED_KINDS | NATIVE_DIRECT_KINDS | NATIVE_FIELD_ROUTED_KINDS) == EVENT_KEYS
    assert MANAGED_KINDS == {
        "deactivate_prim",
        "delete_prim",
        "ensure_prim",
        "ensure_xform_ops",
        "load_payload",
        "rename_prim",
        "set_connectable_connection",
        "set_connectable_input",
        "set_gprim_attrs",
        "set_instanceable",
        "set_material_binding",
        "set_payload",
        "set_point_instancer",
        "set_reference",
        "set_sdf_spec_fields",
        "set_stage_metadata",
        "set_variant_selections",
        "set_visibility",
        "set_xform_trs",
        "unload_payload",
    }
    assert SHARED_STAGE_EVENT_KINDS == {
        "replace_sdf_layer_content",
        "set_sdf_spec_fields",
        "set_sublayers",
    }
    assert SHARED_STAGE_ONLY_KINDS == {"replace_sdf_layer_content", "set_sublayers"}
    assert MANAGED_KINDS | SHARED_STAGE_EVENT_KINDS == EVENT_KEYS


def test_stage_sync_kinds_are_structural():
    """Every stage-sync kind must also be structural.

    The dispatcher's mirror commit applies only STAGE_SYNC_KINDS, and
    Structural classification ensures prim-index-affecting stage-sync kinds
    run before ordinary value writes. A kind that needs the mirror but stays
    value-tier should instead
    dedup via event-value writes in the emitter's _INVALIDATE_DISPATCH
    (the set_gprim_attrs pattern).
    """
    assert STAGE_SYNC_KINDS <= STRUCTURAL_EVENT_KINDS, (
        f"value-tier stage-sync kinds: {STAGE_SYNC_KINDS - STRUCTURAL_EVENT_KINDS}"
    )


def test_every_managed_event_has_an_explicit_local_opinion_policy():
    assert not (
        _EVENTS_REQUIRING_LOCAL_OPINION_FILTERING & _EVENTS_ALREADY_PROVEN_LOCAL
    )
    assert (
        _EVENTS_REQUIRING_LOCAL_OPINION_FILTERING | _EVENTS_ALREADY_PROVEN_LOCAL
    ) == MANAGED_KINDS
