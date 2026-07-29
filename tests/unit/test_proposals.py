"""Tests for cross-department edit proposals."""

from __future__ import annotations

import pytest

from openusdconnect.server import UsdSyncServer


@pytest.fixture
def srv():
    return UsdSyncServer(
        log_path=":memory:",
        department_priority=["lighting", "animation", "layout"],
    )


@pytest.fixture
def srv_with_layers(srv):
    """Server with Alice (layout) and Bob (animation) layers populated."""
    from pxr import Usd

    from openusdconnect.event_apply import apply_events

    layer_a = srv.get_or_create_client_layer("alice", "layout")
    srv.stage.SetEditTarget(Usd.EditTarget(layer_a))
    apply_events(srv.stage, [
        {"k": "ensure_prim", "prim": "/World/Cube", "typeName": "Xform"},
        {"k": "ensure_xform_ops", "prim": "/World/Cube"},
        {"k": "set_xform_trs", "prim": "/World/Cube",
         "fields": ["t"], "t": [1.0, 0.0, 0.0]},
    ], op_cache=srv._op_cache_for(layer_a))

    layer_b = srv.get_or_create_client_layer("bob", "animation")
    srv.stage.SetEditTarget(Usd.EditTarget(layer_b))
    apply_events(srv.stage, [
        {"k": "ensure_prim", "prim": "/World/Cube", "typeName": "Xform"},
        {"k": "ensure_xform_ops", "prim": "/World/Cube"},
        {"k": "set_xform_trs", "prim": "/World/Cube",
         "fields": ["t"], "t": [5.0, 3.0, 0.0]},
    ], op_cache=srv._op_cache_for(layer_b))

    return srv


class TestProposalLifecycle:
    def test_create_proposal(self, srv_with_layers):
        pid = srv_with_layers.create_proposal("alice", "lighting", "fix key light")
        assert pid.startswith("prop-")
        assert pid in srv_with_layers.proposals
        p = srv_with_layers.proposals[pid]
        assert p.from_client == "alice"
        assert p.from_department == "layout"
        assert p.target_department == "lighting"
        assert p.status == "pending"

    def test_proposal_layer_is_muted(self, srv_with_layers):
        pid = srv_with_layers.create_proposal("alice", "animation", "fix animation")
        p = srv_with_layers.proposals[pid]
        muted = set(srv_with_layers.stage.GetMutedLayers())
        assert p.layer.identifier in muted

    def test_list_proposals_all(self, srv_with_layers):
        srv_with_layers.create_proposal("alice", "lighting", "prop 1")
        srv_with_layers.create_proposal("bob", "layout", "prop 2")
        proposals = srv_with_layers.list_proposals()
        assert len(proposals) == 2

    def test_list_proposals_filtered_by_department(self, srv_with_layers):
        srv_with_layers.create_proposal("alice", "lighting", "for lighting")
        srv_with_layers.create_proposal("bob", "layout", "for layout")
        lighting_props = srv_with_layers.list_proposals(department="lighting")
        assert len(lighting_props) == 1
        assert lighting_props[0]["target_department"] == "lighting"

    def test_reject_removes_layer(self, srv_with_layers):
        pid = srv_with_layers.create_proposal("alice", "animation", "reject test")
        assert srv_with_layers.reject_proposal(pid)
        p = srv_with_layers.proposals[pid]
        assert p.status == "rejected"
        session = srv_with_layers.stage.GetSessionLayer()
        assert p.layer.identifier not in list(session.subLayerPaths)

    def test_reject_nonexistent(self, srv_with_layers):
        assert not srv_with_layers.reject_proposal("nonexistent")

    def test_cannot_reject_twice(self, srv_with_layers):
        pid = srv_with_layers.create_proposal("alice", "animation", "test")
        srv_with_layers.reject_proposal(pid)
        assert not srv_with_layers.reject_proposal(pid)


class TestProposalApproval:
    def test_approve_merges_into_target(self, srv_with_layers):
        pid = srv_with_layers.create_proposal("alice", "animation", "move cube")
        events = [
            {"k": "ensure_prim", "prim": "/World/KeyLight", "typeName": "Xform"},
            {"k": "ensure_xform_ops", "prim": "/World/KeyLight"},
            {"k": "set_xform_trs", "prim": "/World/KeyLight",
             "fields": ["t"], "t": [10.0, 8.0, -5.0]},
        ]
        _apply_to_proposal(srv_with_layers, pid, events)

        # Approve
        assert srv_with_layers.approve_proposal(pid)
        p = srv_with_layers.proposals[pid]
        assert p.status == "approved"

        # Opinions should now be in the animation layer
        anim_layer = srv_with_layers.resolve_layer("animation")
        assert anim_layer is not None
        spec = anim_layer.GetPrimAtPath("/World/KeyLight")
        assert spec is not None

    def test_approve_removes_proposal_layer(self, srv_with_layers):
        pid = srv_with_layers.create_proposal("alice", "animation", "test")
        srv_with_layers.approve_proposal(pid)
        p = srv_with_layers.proposals[pid]
        session = srv_with_layers.stage.GetSessionLayer()
        assert p.layer.identifier not in list(session.subLayerPaths)

    def test_approve_without_materialized_target_layer_fails(self, srv):
        pid = srv.create_proposal("alice", "lighting", "test")
        assert not srv.approve_proposal(pid)

    def test_get_proposal_layer_returns_none_after_approval(self, srv_with_layers):
        pid = srv_with_layers.create_proposal("alice", "animation", "test")
        assert srv_with_layers.get_proposal_layer(pid) is not None
        srv_with_layers.approve_proposal(pid)
        assert srv_with_layers.get_proposal_layer(pid) is None

    def test_approve_projects_composed_sdf_state(self, srv_with_layers, monkeypatch):
        from pxr import Sdf, Usd

        from openusdconnect.codec import message_to_dict
        from openusdconnect.emitter import NoticeEmitter
        from openusdconnect.event_apply import apply_events
        from openusdconnect.protocol_constants import K_SET_SDF_PROPERTY_FIELDS

        def _events(value):
            stage = Usd.Stage.CreateInMemory()
            prim = stage.DefinePrim("/World/Cube", "Xform")
            prim.CreateAttribute(
                "userProperties:value",
                Sdf.ValueTypeNames.Int,
                custom=True,
            ).Set(value)
            emitter = NoticeEmitter(stage)
            try:
                return emitter.snapshot_events()
            finally:
                emitter.cleanup()

        lighting = srv_with_layers.get_or_create_client_layer("lead", "lighting")
        srv_with_layers.apply_txn(_events(2), layer=lighting)

        proposal_id = srv_with_layers.create_proposal("bob", "layout", "weak override")
        _apply_to_proposal(srv_with_layers, proposal_id, _events(1))
        broadcast = []
        monkeypatch.setattr(
            srv_with_layers,
            "broadcast",
            lambda record, **_kwargs: broadcast.append(record),
        )
        # The composed correction is a flat-receiver projection.
        monkeypatch.setattr(
            srv_with_layers,
            "_receiver_audience_presence",
            lambda: (True, False),
        )

        assert srv_with_layers.approve_proposal(proposal_id)
        corrections = [
            record
            for record in broadcast
            if record["event"]["k"] == K_SET_SDF_PROPERTY_FIELDS
        ]
        assert len(corrections) == 1
        stored_generic = [
            record
            for record in map(
                message_to_dict,
                srv_with_layers.store.get_from_seq_bin(1),
            )
            if record["event"]["k"] == K_SET_SDF_PROPERTY_FIELDS
        ]
        assert corrections[0]["seq"] == stored_generic[-1]["seq"]

        flat = Usd.Stage.CreateInMemory()
        apply_events(flat, [record["event"] for record in broadcast])
        assert flat.GetAttributeAtPath("/World/Cube.userProperties:value").Get() == 2


def _apply_to_proposal(srv, proposal_id, events):
    """Helper: route events through the real apply_proposal_txn path."""
    assert srv.apply_proposal_txn(proposal_id, events)
    return srv.get_proposal_layer(proposal_id)


class TestProposalTxnRouting:
    def test_proposal_layer_receives_events(self, srv_with_layers):
        pid = srv_with_layers.create_proposal("alice", "lighting", "light fix")
        layer = _apply_to_proposal(srv_with_layers, pid, [
            {"k": "ensure_prim", "prim": "/World/SpotLight", "typeName": "Xform"},
        ])

        # Opinions land in proposal layer, not in any department layer
        assert layer.GetPrimAtPath("/World/SpotLight") is not None
        for collaboration_layer in srv_with_layers.layer_stack.managed_layers:
            assert collaboration_layer.GetPrimAtPath("/World/SpotLight") is None

    def test_proposal_events_not_in_composed_stage(self, srv_with_layers):
        """Muted proposal layer opinions should not be visible on composed stage."""
        pid = srv_with_layers.create_proposal("alice", "lighting", "test")
        layer = _apply_to_proposal(srv_with_layers, pid, [
            {"k": "ensure_prim", "prim": "/World/HiddenPrim", "typeName": "Xform"},
        ])

        # Prim exists in proposal layer but NOT on composed stage (muted)
        assert layer.GetPrimAtPath("/World/HiddenPrim") is not None
        prim = srv_with_layers.stage.GetPrimAtPath("/World/HiddenPrim")
        assert not prim or not prim.IsValid()

    def test_apply_proposal_txn_unknown_returns_false(self, srv_with_layers):
        assert not srv_with_layers.apply_proposal_txn("nonexistent", [
            {"k": "ensure_prim", "prim": "/World/X", "typeName": "Xform"},
        ])

    def test_proposal_survives_session_reorder(self, srv_with_layers):
        """A reorder mid-proposal must keep the proposal layer attached —
        else approve/reject raise and proposal edits have no layer to land in.
        """
        pid = srv_with_layers.create_proposal("alice", "lighting", "rim")
        _apply_to_proposal(srv_with_layers, pid, [
            {"k": "ensure_prim", "prim": "/World/RimLight", "typeName": "Xform"},
        ])
        # A new department's first client updates the managed layer stack.
        srv_with_layers.get_or_create_client_layer("carol", "lighting")

        layer = srv_with_layers.proposals[pid].layer
        session = srv_with_layers.stage.GetSessionLayer()
        assert layer.identifier in list(session.subLayerPaths)
        # Still muted → not composed.
        prim = srv_with_layers.stage.GetPrimAtPath("/World/RimLight")
        assert not prim or not prim.IsValid()
        # And resolving it no longer raises ValueError.
        assert srv_with_layers.reject_proposal(pid)
