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
    ], op_cache=srv.op_cache)

    layer_b = srv.get_or_create_client_layer("bob", "animation")
    srv.stage.SetEditTarget(Usd.EditTarget(layer_b))
    apply_events(srv.stage, [
        {"k": "ensure_prim", "prim": "/World/Cube", "typeName": "Xform"},
        {"k": "ensure_xform_ops", "prim": "/World/Cube"},
        {"k": "set_xform_trs", "prim": "/World/Cube",
         "fields": ["t"], "t": [5.0, 3.0, 0.0]},
    ], op_cache=srv.op_cache)

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
        # Accumulate events as _handle_proposal_txn would
        srv_with_layers.proposals[pid].events.extend(events)

        # Approve
        assert srv_with_layers.approve_proposal(pid)
        p = srv_with_layers.proposals[pid]
        assert p.status == "approved"

        # Opinions should now be in the animation layer
        anim_layer = srv_with_layers._dept_layers["animation"]
        spec = anim_layer.GetPrimAtPath("/World/KeyLight")
        assert spec is not None

    def test_approve_removes_proposal_layer(self, srv_with_layers):
        pid = srv_with_layers.create_proposal("alice", "animation", "test")
        srv_with_layers.approve_proposal(pid)
        p = srv_with_layers.proposals[pid]
        session = srv_with_layers.stage.GetSessionLayer()
        assert p.layer.identifier not in list(session.subLayerPaths)

    def test_approve_without_target_layer_fails(self, srv):
        # No layers created yet, so target department has no layer
        pid = srv.create_proposal("alice", "lighting", "test")
        assert not srv.approve_proposal(pid)

    def test_get_proposal_layer_returns_none_after_approval(self, srv_with_layers):
        pid = srv_with_layers.create_proposal("alice", "animation", "test")
        assert srv_with_layers.get_proposal_layer(pid) is not None
        srv_with_layers.approve_proposal(pid)
        assert srv_with_layers.get_proposal_layer(pid) is None


def _apply_to_proposal(srv, proposal_id, events):
    """Helper: apply events to a proposal layer (unmute/write/remute)."""
    from pxr import Usd

    from openusdconnect.event_apply import apply_events

    layer = srv.get_proposal_layer(proposal_id)
    with srv.stage_lock:
        srv.stage.UnmuteLayer(layer.identifier)
        srv.stage.SetEditTarget(Usd.EditTarget(layer))
        apply_events(srv.stage, events, op_cache=srv.op_cache)
        srv.stage.MuteLayer(layer.identifier)
    return layer


class TestProposalTxnRouting:
    def test_proposal_layer_receives_events(self, srv_with_layers):
        pid = srv_with_layers.create_proposal("alice", "lighting", "light fix")
        layer = _apply_to_proposal(srv_with_layers, pid, [
            {"k": "ensure_prim", "prim": "/World/SpotLight", "typeName": "Xform"},
        ])

        # Opinions land in proposal layer, not in any department layer
        assert layer.GetPrimAtPath("/World/SpotLight") is not None
        for dept_layer in srv_with_layers._dept_layers.values():
            assert dept_layer.GetPrimAtPath("/World/SpotLight") is None

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

