"""Tests for dashboard instancing-awareness on UsdSyncServer.

Pins the in-memory tracker invariants that the dashboard tree relies on
(no per-prim pxr calls per refresh), plus the new fields in
``get_prim_detail`` and the status counters.
"""

from __future__ import annotations

import numpy as np
import pytest

try:
    from pxr import Sdf, Usd  # noqa: F401
    PXR_AVAILABLE = True
except ImportError:
    PXR_AVAILABLE = False

pytestmark = pytest.mark.skipif(not PXR_AVAILABLE, reason="pxr not available")

from openusdconnect.server import UsdSyncServer


@pytest.fixture
def srv(tmp_path):
    s = UsdSyncServer(log_path=str(tmp_path / "instancing.db"))
    yield s
    s.store.close()


# ---------------------------------------------------------------------------
# Incremental trackers
# ---------------------------------------------------------------------------


class TestInstancingTrackers:
    def test_ensure_prim_point_instancer_marks_set(self, srv):
        srv.process_txn([
            {"k": "ensure_prim", "prim": "/PI", "typeName": "PointInstancer"},
            {"k": "ensure_prim", "prim": "/Xform", "typeName": "Xform"},
        ])
        assert "/PI" in srv._point_instancer_paths
        assert "/Xform" not in srv._point_instancer_paths

    def test_set_instanceable_toggles_set(self, srv):
        srv.process_txn([
            {"k": "ensure_prim", "prim": "/Proto", "typeName": "Xform"},
            {"k": "ensure_prim", "prim": "/Inst", "typeName": "Xform"},
            {"k": "set_reference", "prim": "/Inst",
             "refs": [{"asset_path": "", "prim_path": "/Proto"}]},
            {"k": "set_instanceable", "prim": "/Inst", "instanceable": True},
        ])
        assert srv._instanceable_paths == {"/Inst"}

        srv.process_txn([
            {"k": "set_instanceable", "prim": "/Inst", "instanceable": False},
        ])
        assert srv._instanceable_paths == set()

    def test_delete_removes_from_all_trackers(self, srv):
        srv.process_txn([
            {"k": "ensure_prim", "prim": "/PI", "typeName": "PointInstancer"},
            {"k": "ensure_prim", "prim": "/Inst", "typeName": "Xform"},
            {"k": "set_instanceable", "prim": "/Inst", "instanceable": True},
            {"k": "delete_prim", "prim": "/Inst"},
            {"k": "delete_prim", "prim": "/PI"},
        ])
        assert srv._instanceable_paths == set()
        assert srv._point_instancer_paths == set()

    def test_rename_preserves_flags(self, srv):
        srv.process_txn([
            {"k": "ensure_prim", "prim": "/Proto", "typeName": "Xform"},
            {"k": "ensure_prim", "prim": "/Inst", "typeName": "Xform"},
            {"k": "set_reference", "prim": "/Inst",
             "refs": [{"asset_path": "", "prim_path": "/Proto"}]},
            {"k": "set_instanceable", "prim": "/Inst", "instanceable": True},
            {"k": "rename_prim", "prim": "/Inst", "new_name": "Renamed"},
        ])
        assert srv._instanceable_paths == {"/Renamed"}

    def test_compaction_rebuilds_trackers(self, srv):
        srv.process_txn([
            {"k": "ensure_prim", "prim": "/PI", "typeName": "PointInstancer"},
            {"k": "ensure_prim", "prim": "/Proto", "typeName": "Xform"},
            {"k": "ensure_prim", "prim": "/Inst", "typeName": "Xform"},
            {"k": "set_reference", "prim": "/Inst",
             "refs": [{"asset_path": "", "prim_path": "/Proto"}]},
            {"k": "set_instanceable", "prim": "/Inst", "instanceable": True},
        ])
        srv.compact_log()
        assert srv._point_instancer_paths == {"/PI"}
        assert srv._instanceable_paths == {"/Inst"}


# ---------------------------------------------------------------------------
# Tree + detail surfaces
# ---------------------------------------------------------------------------


class TestTreeFields:
    def test_get_prim_tree_has_children_after_index(self, srv):
        """Single-pass child counter must report has_children correctly
        across a multi-level hierarchy, leaf prims included."""
        srv.process_txn([
            {"k": "ensure_prim", "prim": "/World", "typeName": "Xform"},
            {"k": "ensure_prim", "prim": "/World/A", "typeName": "Xform"},
            {"k": "ensure_prim", "prim": "/World/A/Leaf", "typeName": "Sphere"},
            {"k": "ensure_prim", "prim": "/World/B", "typeName": "Xform"},
            {"k": "ensure_prim", "prim": "/Other", "typeName": "Scope"},
        ])
        rows = {r["path"]: r["has_children"] for r in srv.get_prim_tree()}
        assert rows["/World"] is True
        assert rows["/World/A"] is True
        assert rows["/World/A/Leaf"] is False
        assert rows["/World/B"] is False
        assert rows["/Other"] is False

    def test_get_prim_tree_includes_flags(self, srv):
        srv.process_txn([
            {"k": "ensure_prim", "prim": "/Proto", "typeName": "Xform"},
            {"k": "ensure_prim", "prim": "/PI", "typeName": "PointInstancer"},
            {"k": "ensure_prim", "prim": "/Inst", "typeName": "Xform"},
            {"k": "set_reference", "prim": "/Inst",
             "refs": [{"asset_path": "", "prim_path": "/Proto"}]},
            {"k": "set_instanceable", "prim": "/Inst", "instanceable": True},
        ])
        tree = {row["path"]: row for row in srv.get_prim_tree()}
        assert tree["/Inst"]["instanceable"] is True
        assert tree["/Inst"]["is_point_instancer"] is False
        assert tree["/PI"]["is_point_instancer"] is True
        assert tree["/PI"]["instanceable"] is False


class TestPrimDetailInstancing:
    def test_detail_reports_instance_flags(self, srv):
        srv.process_txn([
            {"k": "ensure_prim", "prim": "/Proto", "typeName": "Xform"},
            {"k": "ensure_prim", "prim": "/Inst", "typeName": "Xform"},
            {"k": "set_reference", "prim": "/Inst",
             "refs": [{"asset_path": "", "prim_path": "/Proto"}]},
            {"k": "set_instanceable", "prim": "/Inst", "instanceable": True},
        ])
        d = srv.get_prim_detail("/Inst")
        assert d["isInstanceable"] is True
        assert d["isInstance"] is True
        assert d["prototype"] is not None
        assert d["prototype"].startswith("/__Prototype")
        assert d["isInstanceProxy"] is False

    def test_detail_pointinstancer_block(self, srv):
        srv.process_txn([
            {"k": "ensure_prim", "prim": "/Protos", "typeName": "Scope"},
            {"k": "ensure_prim", "prim": "/Protos/A", "typeName": "Xform"},
            {"k": "ensure_prim", "prim": "/PI", "typeName": "PointInstancer"},
            {"k": "set_point_instancer", "prim": "/PI",
             "fields": ["prototypes", "proto_indices", "positions"],
             "prototypes": ["/Protos/A"],
             "proto_indices": np.array([0, 0, 0], dtype=np.int32),
             "positions": np.array([[0, 0, 0], [1, 0, 0], [2, 0, 0]], dtype=np.float32)},
        ])
        d = srv.get_prim_detail("/PI")
        pi = d["pointInstancer"]
        assert pi["prototypes"] == ["/Protos/A"]
        assert pi["instanceCount"] == 3
        assert pi["inactiveIdCount"] is None
        # Static arrays, no time samples authored.
        assert pi["animatedArrays"] == []


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


class TestCounters:
    def test_get_instance_count(self, srv):
        assert srv.get_instance_count() == 0
        events = [{"k": "ensure_prim", "prim": "/Proto", "typeName": "Xform"}]
        events += [
            {"k": "ensure_prim", "prim": f"/I{i}", "typeName": "Xform"}
            for i in range(3)
        ]
        events += [
            {"k": "set_reference", "prim": f"/I{i}",
             "refs": [{"asset_path": "", "prim_path": "/Proto"}]}
            for i in range(3)
        ]
        events += [
            {"k": "set_instanceable", "prim": f"/I{i}", "instanceable": True}
            for i in range(3)
        ]
        srv.process_txn(events)
        assert srv.get_instance_count() == 3

    def test_get_prototype_count_grows_with_distinct_instancing_keys(self, srv):
        # No instances yet.
        assert srv.get_prototype_count() == 0
        srv.process_txn([
            {"k": "ensure_prim", "prim": "/Proto", "typeName": "Xform"},
            {"k": "ensure_prim", "prim": "/I0", "typeName": "Xform"},
            {"k": "ensure_prim", "prim": "/I1", "typeName": "Xform"},
            {"k": "set_reference", "prim": "/I0",
             "refs": [{"asset_path": "", "prim_path": "/Proto"}]},
            {"k": "set_reference", "prim": "/I1",
             "refs": [{"asset_path": "", "prim_path": "/Proto"}]},
            {"k": "set_instanceable", "prim": "/I0", "instanceable": True},
            {"k": "set_instanceable", "prim": "/I1", "instanceable": True},
        ])
        # Same arc + no differentiating opinions, so both share one prototype.
        assert srv.get_prototype_count() == 1
