"""Tests for generic API-schema replication on the K_ENSURE_PRIM event.

Decoupled from UsdLux on purpose — uses MotionAPI (UsdGeom) and CollectionAPI
to prove the mechanism is generic. UsdLux-specific tests live in
``test_usdlux.py``.
"""

import logging

import flatbuffers
import pytest

try:
    from pxr import Usd, UsdGeom  # noqa: F401

    PXR_AVAILABLE = True
except ImportError:
    PXR_AVAILABLE = False

pytestmark = pytest.mark.skipif(not PXR_AVAILABLE, reason="pxr not available")

from openusdconnect.codec import (
    EventWrapper,
    _encode_event_wrapper,
    event_to_dict,
)
from openusdconnect.emitter import (
    _REPLICATED_API_SCHEMAS,
    DEFAULT_REPLICATED_API_SCHEMAS,
    NoticeEmitter,
    register_replicated_api_schema,
    unregister_replicated_api_schema,
)
from openusdconnect.event_apply import apply_event, apply_events
from openusdconnect.protocol_constants import K_ENSURE_PRIM
from openusdconnect.server.state import UsdSyncServer


def _roundtrip(ev: dict) -> dict:
    """Codec encode + decode an event dict — exercises FlatBuffers."""
    b = flatbuffers.Builder(256)
    off = _encode_event_wrapper(b, ev)
    b.Finish(off)
    ew = EventWrapper.GetRootAs(b.Output(), 0)
    return event_to_dict(ew)


class TestApplyApiSchemas:
    """The applier handles single-apply and multi-apply via Usd.SchemaRegistry."""

    def test_apply_single_api_schema(self):
        stage = Usd.Stage.CreateInMemory()
        apply_event(
            stage,
            {
                "k": K_ENSURE_PRIM,
                "prim": "/Sphere",
                "typeName": "Sphere",
                "api_schemas": ["MotionAPI"],
            },
        )
        prim = stage.GetPrimAtPath("/Sphere")
        assert prim.HasAPI(UsdGeom.MotionAPI)

    def test_apply_multiple_single_apply_schemas(self):
        """Additive — two events apply both APIs."""
        from pxr import UsdLux

        stage = Usd.Stage.CreateInMemory()
        apply_event(
            stage,
            {
                "k": K_ENSURE_PRIM,
                "prim": "/L",
                "typeName": "SphereLight",
                "api_schemas": ["ShapingAPI"],
            },
        )
        apply_event(
            stage,
            {
                "k": K_ENSURE_PRIM,
                "prim": "/L",
                "typeName": "SphereLight",
                "api_schemas": ["ShadowAPI"],
            },
        )
        prim = stage.GetPrimAtPath("/L")
        assert prim.HasAPI(UsdLux.ShapingAPI)
        assert prim.HasAPI(UsdLux.ShadowAPI)

    def test_apply_multi_apply_schema_with_instance(self):
        stage = Usd.Stage.CreateInMemory()
        apply_event(
            stage,
            {
                "k": K_ENSURE_PRIM,
                "prim": "/X",
                "typeName": "Xform",
                "api_schemas": ["CollectionAPI:render"],
            },
        )
        prim = stage.GetPrimAtPath("/X")
        assert prim.HasAPI(Usd.CollectionAPI, "render")

    def test_apply_unknown_schema_warns_and_skips(self, caplog):
        stage = Usd.Stage.CreateInMemory()
        with caplog.at_level(logging.WARNING, logger="openusdconnect.event_apply"):
            apply_event(
                stage,
                {
                    "k": K_ENSURE_PRIM,
                    "prim": "/X",
                    "typeName": "Xform",
                    "api_schemas": ["NonExistentAPI"],
                },
            )
        # No phantom entry on the apiSchemas metadata.
        prim = stage.GetPrimAtPath("/X")
        assert "NonExistentAPI" not in list(prim.GetAppliedSchemas())
        # Warning logged.
        assert any("NonExistentAPI" in rec.message for rec in caplog.records)

    def test_empty_api_schemas_list_no_op(self):
        stage = Usd.Stage.CreateInMemory()
        apply_event(
            stage,
            {
                "k": K_ENSURE_PRIM,
                "prim": "/X",
                "typeName": "Xform",
                "api_schemas": [],
            },
        )
        prim = stage.GetPrimAtPath("/X")
        assert prim.IsValid()
        # No replicated user-applied schemas authored on the spec.


class TestCodecRoundtrip:
    """Wire format mirrors USD's own GetAppliedSchemas() output."""

    def test_roundtrip_single_apply(self):
        ev = {
            "k": K_ENSURE_PRIM,
            "prim": "/X",
            "typeName": "Xform",
            "api_schemas": ["ShapingAPI"],
        }
        out = _roundtrip(ev)
        assert out["api_schemas"] == ["ShapingAPI"]

    def test_roundtrip_multi_apply_instance(self):
        ev = {
            "k": K_ENSURE_PRIM,
            "prim": "/X",
            "typeName": "Xform",
            "api_schemas": ["ShapingAPI", "CollectionAPI:render"],
        }
        out = _roundtrip(ev)
        assert out["api_schemas"] == ["ShapingAPI", "CollectionAPI:render"]

    def test_roundtrip_empty_list_omitted_on_wire(self):
        """An empty list is encoded as no vector; the decoder leaves the key
        absent rather than emitting api_schemas=[]. Both are equivalent on
        the apply side (the applier no-ops on missing/empty)."""
        ev = {
            "k": K_ENSURE_PRIM,
            "prim": "/X",
            "typeName": "Xform",
            "api_schemas": [],
        }
        out = _roundtrip(ev)
        assert out.get("api_schemas") in (None, [])

    def test_roundtrip_missing_field(self):
        """Events without api_schemas (legacy / unaware sender) roundtrip cleanly."""
        ev = {"k": K_ENSURE_PRIM, "prim": "/X", "typeName": "Xform"}
        out = _roundtrip(ev)
        assert out["prim"] == "/X"
        assert out["typeName"] == "Xform"
        assert "api_schemas" not in out


class TestCompactionUnion:
    """Server compaction unions api_schemas across consecutive ensure_prim events."""

    def test_compaction_unions_api_schemas(self, tmp_path):
        from openusdconnect.codec import encode_message
        from openusdconnect.protocol_constants import MSG_EVENT

        # Two consecutive ensure_prim events with different api_schemas
        # should compact to one event whose api_schemas is the union.
        rows = []
        for i, schemas in enumerate([["ShapingAPI"], ["ShadowAPI"]]):
            ev = {
                "k": K_ENSURE_PRIM,
                "prim": "/L",
                "typeName": "SphereLight",
                "api_schemas": schemas,
            }
            msg = {"type": MSG_EVENT, "seq": i + 1, "event": ev}
            rows.append((i + 1, encode_message(msg)))

        latest, _tombstoned = UsdSyncServer._build_compacted(rows)
        merged_ev, _meta = latest[("/L", K_ENSURE_PRIM, None)]
        assert sorted(merged_ev["api_schemas"]) == ["ShadowAPI", "ShapingAPI"]

    def test_compaction_multi_apply_unique_instances(self, tmp_path):
        from openusdconnect.codec import encode_message
        from openusdconnect.protocol_constants import MSG_EVENT

        rows = []
        for i, schemas in enumerate(
            [["CollectionAPI:render"], ["CollectionAPI:proxy"]],
        ):
            ev = {
                "k": K_ENSURE_PRIM,
                "prim": "/X",
                "typeName": "Xform",
                "api_schemas": schemas,
            }
            msg = {"type": MSG_EVENT, "seq": i + 1, "event": ev}
            rows.append((i + 1, encode_message(msg)))

        latest, _tombstoned = UsdSyncServer._build_compacted(rows)
        merged_ev, _meta = latest[("/X", K_ENSURE_PRIM, None)]
        assert sorted(merged_ev["api_schemas"]) == [
            "CollectionAPI:proxy",
            "CollectionAPI:render",
        ]


class TestEmitterFilter:
    """The emitter snapshots the global at construction; explicit override is honored."""

    def test_default_whitelist_replicates_shaping(self):
        from pxr import UsdLux

        stage = Usd.Stage.CreateInMemory()
        em = NoticeEmitter(stage)
        p = stage.DefinePrim("/L", "SphereLight")
        UsdLux.ShapingAPI.Apply(p)
        evs = em.build_events_for_dirty()
        ensure = [e for e in evs if e["k"] == K_ENSURE_PRIM and e["prim"] == "/L"]
        assert ensure
        assert "ShapingAPI" in ensure[0]["api_schemas"]

    def test_default_whitelist_excludes_motion(self):
        """MotionAPI isn't a default — replicating it requires opt-in."""
        stage = Usd.Stage.CreateInMemory()
        em = NoticeEmitter(stage)
        p = stage.DefinePrim("/Sphere", "Sphere")
        UsdGeom.MotionAPI.Apply(p)
        evs = em.build_events_for_dirty()
        ensure = [e for e in evs if e["k"] == K_ENSURE_PRIM and e["prim"] == "/Sphere"]
        assert ensure
        assert "MotionAPI" not in ensure[0].get("api_schemas", [])

    def test_constructor_override(self):
        stage = Usd.Stage.CreateInMemory()
        em = NoticeEmitter(stage, replicated_api_schemas={"MotionAPI"})
        p = stage.DefinePrim("/Sphere", "Sphere")
        UsdGeom.MotionAPI.Apply(p)
        evs = em.build_events_for_dirty()
        ensure = [e for e in evs if e["k"] == K_ENSURE_PRIM and e["prim"] == "/Sphere"]
        assert ensure
        assert ensure[0]["api_schemas"] == ["MotionAPI"]

    def test_constructor_override_does_not_replicate_unwhitelisted(self):
        """A custom whitelist of {MotionAPI} should NOT replicate ShapingAPI."""
        from pxr import UsdLux

        stage = Usd.Stage.CreateInMemory()
        em = NoticeEmitter(stage, replicated_api_schemas={"MotionAPI"})
        p = stage.DefinePrim("/L", "SphereLight")
        UsdLux.ShapingAPI.Apply(p)
        evs = em.build_events_for_dirty()
        ensure = [e for e in evs if e["k"] == K_ENSURE_PRIM and e["prim"] == "/L"]
        assert ensure
        assert "ShapingAPI" not in ensure[0].get("api_schemas", [])


class TestRegisterReplicatedApiSchema:
    """The module-level register affects emitters constructed after the call."""

    def test_register_picked_up_by_new_emitter(self):
        # Snapshot to restore after the test.
        original = set(_REPLICATED_API_SCHEMAS)
        try:
            assert "MotionAPI" not in _REPLICATED_API_SCHEMAS
            register_replicated_api_schema("MotionAPI")
            assert "MotionAPI" in _REPLICATED_API_SCHEMAS

            stage = Usd.Stage.CreateInMemory()
            em = NoticeEmitter(stage)
            p = stage.DefinePrim("/Sphere", "Sphere")
            UsdGeom.MotionAPI.Apply(p)
            evs = em.build_events_for_dirty()
            ensure = [e for e in evs if e["k"] == K_ENSURE_PRIM and e["prim"] == "/Sphere"]
            assert ensure
            assert "MotionAPI" in ensure[0]["api_schemas"]
        finally:
            _REPLICATED_API_SCHEMAS.clear()
            _REPLICATED_API_SCHEMAS.update(original)

    def test_existing_emitter_unaffected_by_late_register(self):
        """Snapshot semantics — emitters constructed before the register call
        keep the old set."""
        original = set(_REPLICATED_API_SCHEMAS)
        try:
            stage = Usd.Stage.CreateInMemory()
            em_before = NoticeEmitter(stage)

            assert "MotionAPI" not in em_before._replicated_apis
            register_replicated_api_schema("MotionAPI")

            # em_before's frozenset still excludes MotionAPI.
            assert "MotionAPI" not in em_before._replicated_apis

            em_after = NoticeEmitter(stage)
            assert "MotionAPI" in em_after._replicated_apis
        finally:
            _REPLICATED_API_SCHEMAS.clear()
            _REPLICATED_API_SCHEMAS.update(original)

    def test_register_validates_against_usd_registry(self, caplog):
        """Bogus schema name logs a warning but is still added (lazy plugin
        loading); real schema name is silent."""
        original = set(_REPLICATED_API_SCHEMAS)
        try:
            with caplog.at_level(logging.WARNING, logger="openusdconnect.emitter"):
                caplog.clear()
                register_replicated_api_schema("MotionAPI")
                assert not any("MotionAPI" in r.message for r in caplog.records)

                caplog.clear()
                register_replicated_api_schema("BogusAPI")
                assert any("BogusAPI" in r.message for r in caplog.records)
                # Still added (lazy plugin loading semantics).
                assert "BogusAPI" in _REPLICATED_API_SCHEMAS
        finally:
            _REPLICATED_API_SCHEMAS.clear()
            _REPLICATED_API_SCHEMAS.update(original)

    def test_register_rejects_instance_suffix(self):
        with pytest.raises(ValueError, match="bare schema name"):
            register_replicated_api_schema("CollectionAPI:render")

    def test_unregister(self):
        original = set(_REPLICATED_API_SCHEMAS)
        try:
            register_replicated_api_schema("MotionAPI")
            assert "MotionAPI" in _REPLICATED_API_SCHEMAS
            unregister_replicated_api_schema("MotionAPI")
            assert "MotionAPI" not in _REPLICATED_API_SCHEMAS
        finally:
            _REPLICATED_API_SCHEMAS.clear()
            _REPLICATED_API_SCHEMAS.update(original)


class TestEmitterReEmitOnSchemaChange:
    def test_re_emit_after_late_api_schema_application(self):
        """Apply ShapingAPI to an existing prim — the next emit cycle produces
        a fresh ensure_prim with updated api_schemas."""
        from pxr import UsdLux

        stage = Usd.Stage.CreateInMemory()
        em = NoticeEmitter(stage)
        p = stage.DefinePrim("/L", "SphereLight")
        # First cycle: just the SphereLight + (built-in LightAPI not in whitelist).
        first = em.build_events_for_dirty()
        first_ensure = [e for e in first if e["k"] == K_ENSURE_PRIM and e["prim"] == "/L"]
        assert first_ensure
        assert first_ensure[0]["api_schemas"] == []

        # Second cycle after applying ShapingAPI.
        UsdLux.ShapingAPI.Apply(p)
        second = em.build_events_for_dirty()
        second_ensure = [e for e in second if e["k"] == K_ENSURE_PRIM and e["prim"] == "/L"]
        assert second_ensure
        assert "ShapingAPI" in second_ensure[0]["api_schemas"]


class TestDefaults:
    def test_default_set_excludes_motion_and_collection(self):
        """MotionAPI and CollectionAPI are NOT defaults — explicit opt-in only."""
        assert "MotionAPI" not in DEFAULT_REPLICATED_API_SCHEMAS
        assert "CollectionAPI" not in DEFAULT_REPLICATED_API_SCHEMAS
        # MaterialBindingAPI has its own dedicated event.
        assert "MaterialBindingAPI" not in DEFAULT_REPLICATED_API_SCHEMAS

    def test_default_set_includes_usdlux_user_applied(self):
        assert "ShapingAPI" in DEFAULT_REPLICATED_API_SCHEMAS
        assert "ShadowAPI" in DEFAULT_REPLICATED_API_SCHEMAS
        assert "MeshLightAPI" in DEFAULT_REPLICATED_API_SCHEMAS
        assert "VolumeLightAPI" in DEFAULT_REPLICATED_API_SCHEMAS


class TestStructuralOrdering:
    def test_api_schemas_applied_before_inputs(self):
        """Shuffled events still apply in dependency order — ShapingAPI must
        be present before inputs:shaping:cone:angle is written."""
        from pxr import UsdLux

        from openusdconnect.protocol_constants import K_SET_CONNECTABLE_INPUT

        events = [
            # Out-of-order: input first, then ensure_prim with ShapingAPI.
            {
                "k": K_SET_CONNECTABLE_INPUT,
                "prim": "/L",
                "info_id": "",
                "inputs": {"shaping:cone:angle": 45.0},
                "input_types": {"shaping:cone:angle": "float"},
            },
            {
                "k": K_ENSURE_PRIM,
                "prim": "/L",
                "typeName": "SphereLight",
                "api_schemas": ["ShapingAPI"],
            },
        ]
        stage = Usd.Stage.CreateInMemory()
        apply_events(stage, events)
        p = stage.GetPrimAtPath("/L")
        assert p.GetTypeName() == "SphereLight"
        assert p.HasAPI(UsdLux.ShapingAPI)
        assert UsdLux.ShapingAPI(p).GetShapingConeAngleAttr().Get() == 45.0


class TestMockAdapter:
    def test_mock_adapter_stores_api_schemas(self):
        from openusdconnect.adapters import MockAdapter

        adapter = MockAdapter()
        adapter.apply_event(
            {
                "k": K_ENSURE_PRIM,
                "prim": "/L",
                "typeName": "SphereLight",
                "api_schemas": ["ShapingAPI", "CollectionAPI:render"],
            },
        )
        assert adapter.get_prim("/L")["api_schemas"] == ["ShapingAPI", "CollectionAPI:render"]

    def test_mock_adapter_additive_api_schemas(self):
        from openusdconnect.adapters import MockAdapter

        adapter = MockAdapter()
        adapter.apply_event(
            {
                "k": K_ENSURE_PRIM,
                "prim": "/L",
                "typeName": "SphereLight",
                "api_schemas": ["ShapingAPI"],
            },
        )
        adapter.apply_event(
            {
                "k": K_ENSURE_PRIM,
                "prim": "/L",
                "typeName": "SphereLight",
                "api_schemas": ["ShadowAPI"],
            },
        )
        merged = adapter.get_prim("/L")["api_schemas"]
        assert sorted(merged) == ["ShadowAPI", "ShapingAPI"]
