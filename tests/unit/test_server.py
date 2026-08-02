"""Tests for UsdSyncServer — in-process, no TCP.

Instantiates UsdSyncServer directly and exercises its core logic:
sequence assignment, event log, compaction, replay, apply_txn, edit layer, etc.
"""

import threading

import pytest
from pxr import Ar, Gf, Sdf, Usd, UsdGeom

from openusdconnect.codec import message_to_dict
from openusdconnect.protocol_constants import SHARED_STAGE_KINDS
from openusdconnect.server import TokenBucket, UsdSyncServer


@pytest.fixture
def srv(tmp_path):
    """Create a UsdSyncServer with a temp SQLite DB."""
    db = str(tmp_path / "test.db")
    s = UsdSyncServer(log_path=db)
    yield s
    s.store.close()


# ---------------------------------------------------------------------------
# Sequence assignment
# ---------------------------------------------------------------------------


class TestAssignSeq:
    def test_starts_at_one(self, srv):
        assert srv.assign_seq() == 1

    def test_monotonic(self, srv):
        a = srv.assign_seq()
        b = srv.assign_seq()
        c = srv.assign_seq()
        assert (a, b, c) == (1, 2, 3)


# ---------------------------------------------------------------------------
# Event log (append + read back)
# ---------------------------------------------------------------------------


class TestAppendLog:
    def test_append_and_read(self, srv):
        rec = {
            "type": "event",
            "seq": 1,
            "event": {"k": "ensure_prim", "prim": "/A", "typeName": "Xform"},
            "layer_key": "default",
        }
        srv.append_log(rec)

        rows = srv.store.get_all_asc()
        assert len(rows) == 1
        assert rows[0][0] == 1
        assert message_to_dict(rows[0][1])["event"]["prim"] == "/A"

    def test_multiple_appends(self, srv):
        for i in range(5):
            srv.append_log(
                {
                    "type": "event",
                    "seq": i + 1,
                    "event": {"k": "ensure_prim", "prim": f"/P{i}", "typeName": "Xform"},
                    "layer_key": "default",
                }
            )
        rows = (srv.store.get_count(),)
        assert rows[0] == 5


# ---------------------------------------------------------------------------
# Stage creation
# ---------------------------------------------------------------------------


class TestStageCreation:
    def test_in_memory_stage_has_root(self, srv):
        prim = srv.stage.GetPrimAtPath("/Root")
        assert prim.IsValid()

    def test_invalid_base_path_raises(self, tmp_path):
        with pytest.raises(RuntimeError):
            UsdSyncServer(
                base_usd_path=str(tmp_path / "nonexistent.usda"),
                log_path=str(tmp_path / "err.db"),
            )

    def test_in_memory_stage_uses_explicit_resolver_context(self, tmp_path):
        context = Ar.DefaultResolverContext([str(tmp_path / "assets")])
        server = UsdSyncServer(
            log_path=str(tmp_path / "context.db"),
            resolver_context=context,
        )
        try:
            assert server.stage.GetPathResolverContext() == Ar.ResolverContext(context)
        finally:
            server.shutdown()
            server.store.close()

    def test_accepts_caller_owned_stage(self, tmp_path):
        stage = Usd.Stage.CreateInMemory("caller-owned.usda")
        stage.DefinePrim("/Scene", "Xform")
        server = UsdSyncServer(
            log_path=str(tmp_path / "caller.db"),
            stage=stage,
        )
        try:
            assert server.stage is stage
            assert server.stage.GetPrimAtPath("/Scene")
        finally:
            server.shutdown()
            server.store.close()

    def test_rejects_ambiguous_stage_sources(self, tmp_path):
        stage = Usd.Stage.CreateInMemory("caller-owned.usda")
        with pytest.raises(ValueError, match="mutually exclusive"):
            UsdSyncServer(
                base_usd_path=str(tmp_path / "scene.usda"),
                log_path=str(tmp_path / "ambiguous.db"),
                stage=stage,
            )
        with pytest.raises(ValueError, match="already owns"):
            UsdSyncServer(
                log_path=str(tmp_path / "context.db"),
                stage=stage,
                resolver_context=Ar.DefaultResolverContext([]),
            )


# ---------------------------------------------------------------------------
# apply_txn
# ---------------------------------------------------------------------------


class TestApplyTxn:
    def test_applies_events_to_stage(self, srv):
        events = [
            {"k": "ensure_prim", "prim": "/World/Cube", "typeName": "Cube"},
            {"k": "ensure_xform_ops", "prim": "/World/Cube"},
            {"k": "set_xform_trs", "prim": "/World/Cube", "fields": ["t"], "t": [1.0, 2.0, 3.0]},
        ]
        srv.apply_txn(events)

        prim = srv.stage.GetPrimAtPath("/World/Cube")
        assert prim.IsValid()
        assert prim.GetTypeName() == "Cube"


# ---------------------------------------------------------------------------
# process_txn routing
# ---------------------------------------------------------------------------


class TestProcessTxnRouting:
    def test_record_uses_actual_edit_target_instead_of_cached_client_layer(
        self,
        srv,
    ):
        animation = srv.get_or_create_client_layer(
            "artist",
            department="animation",
        )

        records, _changed = srv.process_txn(
            [{"k": "ensure_prim", "prim": "/World/New", "typeName": "Xform"}],
            client_id="artist",
            layer=srv.edit_layer,
        )
        record = message_to_dict(records[0][1])

        assert record["layer_key"] == "default"
        assert srv.edit_layer.GetPrimAtPath("/World/New")
        assert not animation.GetPrimAtPath("/World/New")

    def test_unmanaged_collaboration_target_fails_before_authoring(self, srv):
        unmanaged = Sdf.Layer.CreateAnonymous("unmanaged")

        with pytest.raises(
            ValueError,
            match="not a managed collaboration layer",
        ):
            srv.process_txn(
                [
                    {
                        "k": "ensure_prim",
                        "prim": "/World/Rejected",
                        "typeName": "Xform",
                    }
                ],
                client_id="artist",
                layer=unmanaged,
            )

        assert not unmanaged.GetPrimAtPath("/World/Rejected")
        assert srv.store.get_count() == 0


# ---------------------------------------------------------------------------
# get_prim_detail (dashboard inspector)
# ---------------------------------------------------------------------------


class TestPrimDetail:
    def test_missing_prim_marked_absent(self, srv):
        assert srv.get_prim_detail("/Nope") == {"path": "/Nope", "exists": False}

    def test_composed_fields(self, srv):
        srv.apply_txn(
            [
                {"k": "ensure_prim", "prim": "/World/Cube", "typeName": "Cube"},
                {"k": "ensure_xform_ops", "prim": "/World/Cube"},
                {"k": "set_xform_trs", "prim": "/World/Cube",
                 "fields": ["t"], "t": [1.0, 2.0, 3.0]},
                {"k": "set_visibility", "prim": "/World/Cube", "visible": False},
                {"k": "set_gprim_attrs", "prim": "/World/Cube", "attrs": {"size": 2.0}},
            ]
        )
        d = srv.get_prim_detail("/World/Cube")
        assert d["exists"] is True
        assert d["typeName"] == "Cube"
        assert d["active"] is True
        assert d["visibility"] == "invisible"
        assert d["xform"]["t"] == [1.0, 2.0, 3.0]
        names = {a["name"] for a in d["attributes"]}
        assert {"xformOp:translate", "size"} <= names

    def test_array_attrs_reported_by_type_not_materialized(self, srv):
        srv.apply_txn(
            [
                {"k": "ensure_prim", "prim": "/World/M", "typeName": "Mesh"},
                {
                    "k": "set_gprim_attrs",
                    "prim": "/World/M",
                    "attrs": {"primvars:st": [[0, 0], [1, 0], [1, 1]]},
                    "primvar_meta": {
                        "primvars:st": {
                            "typeName": "texCoord2f[]",
                            "interpolation": "faceVarying",
                        }
                    },
                },
            ]
        )
        d = srv.get_prim_detail("/World/M")
        st = next(a for a in d["attributes"] if a["name"] == "primvars:st")
        assert st["type"].endswith("[]")
        assert st["value"] == "[array]"


# ---------------------------------------------------------------------------
# Dashboard snapshot helpers (get_transforms_snapshot, export_layer)
# ---------------------------------------------------------------------------


class TestDashboardSnapshots:
    def test_transforms_snapshot_reports_trs(self, srv):
        srv.apply_txn(
            [
                {"k": "ensure_prim", "prim": "/World/Cube", "typeName": "Cube"},
                {"k": "ensure_xform_ops", "prim": "/World/Cube"},
                {"k": "set_xform_trs", "prim": "/World/Cube",
                 "fields": ["t", "s"], "t": [1.0, 2.0, 3.0], "s": [2.0, 2.0, 2.0]},
            ]
        )
        row = next(
            r for r in srv.get_transforms_snapshot() if r["path"] == "/World/Cube"
        )
        assert row["t"] == [1.0, 2.0, 3.0]
        assert row["s"] == [2.0, 2.0, 2.0]

    def test_transforms_snapshot_skips_prims_without_ops(self, srv):
        srv.apply_txn(
            [{"k": "ensure_prim", "prim": "/World/Plain", "typeName": "Xform"}]
        )
        paths = {r["path"] for r in srv.get_transforms_snapshot()}
        assert "/World/Plain" not in paths

    def test_export_layer_not_found(self, srv):
        assert srv.export_layer("nonexistent") == "# layer not found"

    def test_export_layer_resolves_dept_layer(self, srv):
        srv.get_or_create_client_layer("alice", department="anim")
        usda = srv.export_layer("anim")
        assert usda != "# layer not found"
        assert "#usda" in usda


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


class TestCompaction:
    def _insert_events(self, srv, events):
        """Helper: assign seqs, append to log, apply to stage."""
        for ev in events:
            seq = srv.assign_seq()
            rec = {"type": "event", "seq": seq, "event": ev}
            if ev["k"] not in SHARED_STAGE_KINDS:
                rec["layer_key"] = "default"
            srv.append_log(rec)
        srv.apply_txn(events)

    def test_trs_merged(self, srv):
        """Two TRS events for the same prim should merge into one."""
        self._insert_events(
            srv,
            [
                {"k": "ensure_prim", "prim": "/A", "typeName": "Xform"},
                {"k": "ensure_xform_ops", "prim": "/A"},
                {"k": "set_xform_trs", "prim": "/A", "fields": ["t"], "t": [1, 0, 0]},
            ],
        )
        self._insert_events(
            srv,
            [
                {
                    "k": "set_xform_trs",
                    "prim": "/A",
                    "fields": ["t", "r"],
                    "t": [5, 0, 0],
                    "r": [1, 0, 0, 0],
                },
            ],
        )
        count_before = (srv.store.get_count(),)[0]
        assert count_before == 4

        srv.compact_log()

        rows = [(r,) for _, r in srv.store.get_all_asc()]
        events = [message_to_dict(r[0])["event"] for r in rows]
        trs = [e for e in events if e["k"] == "set_xform_trs"]
        assert len(trs) == 1
        assert trs[0]["t"] == [5, 0, 0]
        assert trs[0]["r"] == [1, 0, 0, 0]
        assert "t" in trs[0]["fields"] and "r" in trs[0]["fields"]

    def test_delete_tombstones(self, srv):
        """delete_prim removes all prior events for that prim."""
        self._insert_events(
            srv,
            [
                {"k": "ensure_prim", "prim": "/A", "typeName": "Xform"},
                {"k": "ensure_xform_ops", "prim": "/A"},
                {"k": "set_xform_trs", "prim": "/A", "fields": ["t"], "t": [1, 0, 0]},
            ],
        )
        self._insert_events(
            srv,
            [
                {"k": "delete_prim", "prim": "/A"},
            ],
        )

        srv.compact_log()

        rows = [(r,) for _, r in srv.store.get_all_asc()]
        events = [message_to_dict(r[0])["event"] for r in rows]
        assert len(events) == 1
        assert events[0]["k"] == "delete_prim"

    def test_rename_tombstones_old(self, srv):
        """rename_prim removes prior events for the old path."""
        self._insert_events(
            srv,
            [
                {"k": "ensure_prim", "prim": "/A", "typeName": "Xform"},
                {"k": "set_xform_trs", "prim": "/A", "fields": ["t"], "t": [1, 0, 0]},
            ],
        )
        self._insert_events(
            srv,
            [
                {"k": "rename_prim", "prim": "/A", "new_name": "B"},
            ],
        )

        srv.compact_log()

        rows = [(r,) for _, r in srv.store.get_all_asc()]
        events = [message_to_dict(r[0])["event"] for r in rows]
        prims = [e["prim"] for e in events]
        # Only the rename should remain for /A
        assert all(p != "/A" or e["k"] == "rename_prim" for p, e in zip(prims, events, strict=True))

    def test_visibility_latest_wins(self, srv):
        """Only the latest visibility event per prim survives compaction."""
        self._insert_events(
            srv,
            [
                {"k": "ensure_prim", "prim": "/A", "typeName": "Xform"},
                {"k": "set_visibility", "prim": "/A", "visible": False},
            ],
        )
        self._insert_events(
            srv,
            [
                {"k": "set_visibility", "prim": "/A", "visible": True},
            ],
        )

        srv.compact_log()

        rows = [(r,) for _, r in srv.store.get_all_asc()]
        events = [message_to_dict(r[0])["event"] for r in rows]
        vis = [e for e in events if e["k"] == "set_visibility"]
        assert len(vis) == 1
        assert vis[0]["visible"] is True

    def test_load_unload_latest_wins(self, srv):
        """load/unload are mutually exclusive — only last one kept."""
        self._insert_events(
            srv,
            [
                {"k": "ensure_prim", "prim": "/A", "typeName": "Xform"},
                {"k": "load_payload", "prim": "/A"},
            ],
        )
        self._insert_events(
            srv,
            [
                {"k": "unload_payload", "prim": "/A"},
            ],
        )

        srv.compact_log()

        rows = [(r,) for _, r in srv.store.get_all_asc()]
        events = [message_to_dict(r[0])["event"] for r in rows]
        load_events = [e for e in events if e["k"] in ("load_payload", "unload_payload")]
        assert len(load_events) == 1
        assert load_events[0]["k"] == "unload_payload"

    def test_load_rules_compact_in_global_runtime_scope(self, srv):
        from openusdconnect.codec import encode_message

        latest = {}
        srv._merge_event(
            latest,
            1,
            encode_message(
                {
                    "type": "event",
                    "seq": 1,
                    "event": {"k": "load_payload", "prim": "/A"},
                }
            ),
        )
        srv._merge_event(
            latest,
            2,
            encode_message(
                {
                    "type": "event",
                    "seq": 2,
                    "event": {"k": "unload_payload", "prim": "/A"},
                }
            ),
        )

        assert [entry[0]["k"] for entry in latest.values()] == ["unload_payload"]

    def test_stage_metadata_compaction_merges_sparse_fields(self, srv):
        self._insert_events(
            srv,
            [{"k": "set_stage_metadata", "upAxis": "Z"}],
        )
        self._insert_events(
            srv,
            [{"k": "set_stage_metadata", "metersPerUnit": 0.01}],
        )

        srv.compact_log()

        events = [
            message_to_dict(record)["event"]
            for _seq, record in srv.store.get_all_asc()
        ]
        metadata = [event for event in events if event["k"] == "set_stage_metadata"]
        assert metadata == [
            {
                "k": "set_stage_metadata",
                "upAxis": "Z",
                "metersPerUnit": 0.01,
            }
        ]

    def test_variant_selections_latest_wins(self, srv):
        """Only the latest variant selection per prim survives compaction."""
        self._insert_events(
            srv,
            [
                {"k": "ensure_prim", "prim": "/A", "typeName": "Xform"},
                {"k": "set_variant_selections", "prim": "/A", "selections": {"size": "small"}},
            ],
        )
        self._insert_events(
            srv,
            [
                {"k": "set_variant_selections", "prim": "/A", "selections": {"size": "large"}},
            ],
        )

        srv.compact_log()

        rows = [(r,) for _, r in srv.store.get_all_asc()]
        events = [message_to_dict(r[0])["event"] for r in rows]
        vsel = [e for e in events if e["k"] == "set_variant_selections"]
        assert len(vsel) == 1
        assert vsel[0]["selections"]["size"] == "large"

    def test_gprim_attrs_latest_wins(self, srv):
        """Only the latest gprim attrs event per prim survives compaction."""
        self._insert_events(
            srv,
            [
                {"k": "ensure_prim", "prim": "/A", "typeName": "Sphere"},
                {"k": "set_gprim_attrs", "prim": "/A", "attrs": {"radius": 1.0}},
            ],
        )
        self._insert_events(
            srv,
            [
                {"k": "set_gprim_attrs", "prim": "/A", "attrs": {"radius": 5.0}},
            ],
        )

        srv.compact_log()

        rows = [(r,) for _, r in srv.store.get_all_asc()]
        events = [message_to_dict(r[0])["event"] for r in rows]
        attr_evs = [e for e in events if e["k"] == "set_gprim_attrs"]
        assert len(attr_evs) == 1
        assert attr_evs[0]["attrs"]["radius"] == 5.0

    def test_gprim_attrs_merged_across_events(self, srv):
        """Compaction merges attrs dicts from separate events on the same prim."""
        self._insert_events(
            srv,
            [
                {"k": "ensure_prim", "prim": "/A", "typeName": "Mesh"},
                {
                    "k": "set_gprim_attrs",
                    "prim": "/A",
                    "attrs": {"primvars:st": [[0, 0], [1, 0]]},
                    "primvar_meta": {
                        "primvars:st": {
                            "typeName": "texCoord2f[]",
                            "interpolation": "faceVarying",
                        },
                    },
                },
            ],
        )
        self._insert_events(
            srv,
            [
                {"k": "set_gprim_attrs", "prim": "/A", "attrs": {"radius": 2.0}},
            ],
        )

        srv.compact_log()

        rows = [(r,) for _, r in srv.store.get_all_asc()]
        events = [message_to_dict(r[0])["event"] for r in rows]
        attr_evs = [e for e in events if e["k"] == "set_gprim_attrs"]
        assert len(attr_evs) == 1
        # Both attrs survive — merged, not replaced
        assert attr_evs[0]["attrs"]["primvars:st"] == [[0, 0], [1, 0]]
        assert attr_evs[0]["attrs"]["radius"] == 2.0
        # Interpolation metadata survives
        assert attr_evs[0]["primvar_meta"]["primvars:st"]["typeName"] == "texCoord2f[]"
        assert attr_evs[0]["primvar_meta"]["primvars:st"]["interpolation"] == "faceVarying"

    def test_attr_interp_merged_across_events(self, srv):
        """Compaction merges attr_interp from separate events on the same prim."""
        self._insert_events(
            srv,
            [
                {"k": "ensure_prim", "prim": "/A", "typeName": "Mesh"},
                {
                    "k": "set_gprim_attrs",
                    "prim": "/A",
                    "attrs": {"normals": [[0, 0, 1]]},
                    "attr_interp": {"normals": "faceVarying"},
                },
            ],
        )
        self._insert_events(
            srv,
            [
                {"k": "set_gprim_attrs", "prim": "/A", "attrs": {"radius": 2.0}},
            ],
        )

        srv.compact_log()

        rows = [(r,) for _, r in srv.store.get_all_asc()]
        events = [message_to_dict(r[0])["event"] for r in rows]
        attr_evs = [e for e in events if e["k"] == "set_gprim_attrs"]
        assert len(attr_evs) == 1
        assert attr_evs[0]["attrs"]["normals"] == [[0, 0, 1]]
        assert attr_evs[0]["attrs"]["radius"] == 2.0
        assert attr_evs[0]["attr_interp"]["normals"] == "faceVarying"

    def test_shader_inputs_merged_across_events(self, srv):
        """Compaction merges shader inputs from separate events on the same prim."""
        self._insert_events(
            srv,
            [
                {"k": "ensure_prim", "prim": "/Mat", "typeName": "Material"},
                {
                    "k": "set_connectable_input",
                    "prim": "/Mat/PBR",
                    "info_id": "UsdPreviewSurface",
                    "inputs": {"diffuseColor": [1, 0, 0]},
                    "input_types": {"diffuseColor": "color3f"},
                },
            ],
        )
        self._insert_events(
            srv,
            [
                {
                    "k": "set_connectable_input",
                    "prim": "/Mat/PBR",
                    "info_id": "UsdPreviewSurface",
                    "inputs": {"roughness": 0.5},
                    "input_types": {"roughness": "float"},
                },
            ],
        )

        srv.compact_log()

        rows = [(r,) for _, r in srv.store.get_all_asc()]
        events = [message_to_dict(r[0])["event"] for r in rows]
        shader_evs = [e for e in events if e["k"] == "set_connectable_input"]
        assert len(shader_evs) == 1
        # Both inputs survive — merged, not replaced
        assert shader_evs[0]["inputs"]["diffuseColor"] == [1, 0, 0]
        assert shader_evs[0]["inputs"]["roughness"] == 0.5
        assert shader_evs[0]["input_types"]["diffuseColor"] == "color3f"
        assert shader_evs[0]["input_types"]["roughness"] == "float"
        assert shader_evs[0]["info_id"] == "UsdPreviewSurface"

    def test_compact_empty_log_noop(self, srv):
        """Compacting an empty log doesn't crash."""
        srv.compact_log()
        rows = (srv.store.get_count(),)
        assert rows[0] == 0

    def test_compaction_replay_drops_only_disconnected_receiver(
        self,
        srv,
    ):
        """A failed replay must not prevent healthy receivers from resyncing."""
        import io

        from openusdconnect.framing import recv_framed_rfile

        self._insert_events(
            srv,
            [{"k": "ensure_prim", "prim": "/A", "typeName": "Xform"}],
        )

        class BrokenRequest:
            def sendall(self, _payload):
                raise OSError("receiver disconnected")

        class FakeHandler:
            def __init__(self, request, address):
                self.request = request
                self.client_address = address
                self.send_lock = threading.Lock()

        healthy_request = io.BytesIO()
        healthy_request.sendall = healthy_request.write
        broken = FakeHandler(BrokenRequest(), ("broken", 1))
        healthy = FakeHandler(healthy_request, ("healthy", 2))
        srv.receivers.update((broken, healthy))

        srv.compact_log()

        assert broken not in srv.receivers
        assert healthy in srv.receivers
        healthy_request.seek(0)
        resync = message_to_dict(recv_framed_rfile(healthy_request))
        replayed = message_to_dict(recv_framed_rfile(healthy_request))
        assert resync["type"] == "resync"
        assert replayed["event"]["prim"] == "/A"

    def test_compaction_drains_queued_broadcast_before_resync(self, srv):
        import io
        import time

        from openusdconnect.framing import recv_framed_rfile

        self._insert_events(
            srv,
            [{"k": "ensure_prim", "prim": "/A", "typeName": "Xform"}],
        )

        class FakeHandler:
            def __init__(self):
                self.request = io.BytesIO()
                self.request.sendall = self.request.write
                self.client_address = ("slow", 1)
                self.send_lock = threading.Lock()

        handler = FakeHandler()
        handler.send_lock.acquire()
        srv.receivers.add(handler)
        srv.broadcast(
            {
                "type": "event",
                "seq": 99,
                "event": {
                    "k": "ensure_prim",
                    "prim": "/Queued",
                    "typeName": "Xform",
                },
            }
        )

        deadline = time.monotonic() + 2
        while srv._broadcast_queue.qsize() and time.monotonic() < deadline:
            time.sleep(0.001)
        assert srv._broadcast_queue.qsize() == 0

        compact = threading.Thread(target=srv.compact_log)
        compact.start()
        try:
            assert compact.is_alive()
        finally:
            handler.send_lock.release()
        compact.join(timeout=5)
        assert not compact.is_alive()

        handler.request.seek(0)
        size = len(handler.request.getvalue())
        messages = []
        while handler.request.tell() < size:
            messages.append(message_to_dict(recv_framed_rfile(handler.request)))

        assert messages[0]["seq"] == 99
        assert messages[1]["type"] == "resync"
        assert messages[2]["event"]["prim"] == "/A"

    def test_seq_resets_after_compact(self, srv):
        """After compaction, sequence numbers restart from 1."""
        self._insert_events(
            srv,
            [
                {"k": "ensure_prim", "prim": "/A", "typeName": "Xform"},
                {"k": "ensure_prim", "prim": "/B", "typeName": "Xform"},
            ],
        )
        assert srv.assign_seq() == 3  # next would be 3

        srv.compact_log()

        # After compact, seqs were reassigned starting from 1
        rows = srv.store.get_all_asc()
        seqs = [r[0] for r in rows]
        assert seqs[0] == 1


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


class TestReplay:
    def test_replay_from_sends_events(self, srv):
        """replay_from writes length-prefixed FB events to a handler's socket."""
        from openusdconnect.codec import message_to_dict
        from openusdconnect.framing import recv_framed_rfile

        for i in range(3):
            seq = srv.assign_seq()
            rec = {
                "type": "event",
                "seq": seq,
                "event": {"k": "ensure_prim", "prim": f"/P{i}", "typeName": "Xform"},
                "layer_key": "default",
            }
            srv.append_log(rec)

        import io

        class FakeHandler:
            def __init__(self):
                self.request = io.BytesIO()

        handler = FakeHandler()
        handler.request.sendall = handler.request.write

        srv.replay_from(handler, 1)

        handler.request.seek(0)
        msgs = []
        raw = handler.request.getvalue()
        while handler.request.tell() < len(raw):
            buf = recv_framed_rfile(handler.request)
            msgs.append(message_to_dict(buf))
        assert len(msgs) == 3
        assert msgs[0]["seq"] == 1
        assert msgs[2]["seq"] == 3

    def test_replay_from_with_offset(self, srv):
        """replay_from with seq_start=2 skips earlier events."""
        from openusdconnect.codec import message_to_dict
        from openusdconnect.framing import recv_framed_rfile

        for i in range(3):
            seq = srv.assign_seq()
            srv.append_log(
                {
                    "type": "event",
                    "seq": seq,
                    "event": {"k": "ensure_prim", "prim": f"/P{i}", "typeName": "Xform"},
                    "layer_key": "default",
                }
            )

        import io

        class FakeHandler:
            def __init__(self):
                self.request = io.BytesIO()

        handler = FakeHandler()
        handler.request.sendall = handler.request.write

        srv.replay_from(handler, 2)

        handler.request.seek(0)
        raw = handler.request.getvalue()
        msgs = []
        while handler.request.tell() < len(raw):
            buf = recv_framed_rfile(handler.request)
            msgs.append(message_to_dict(buf))
        assert len(msgs) == 2
        assert msgs[0]["seq"] == 2

    def test_replay_from_propagates_send_failure(self, srv):
        seq = srv.assign_seq()
        srv.append_log(
            {
                "type": "event",
                "seq": seq,
                "event": {"k": "ensure_prim", "prim": "/P", "typeName": "Xform"},
                "layer_key": "default",
            }
        )

        class BrokenRequest:
            def sendall(self, _payload):
                raise OSError("receiver disconnected")

        class FakeHandler:
            request = BrokenRequest()

        with pytest.raises(OSError, match="receiver disconnected"):
            srv.replay_from(FakeHandler(), 1)


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------


class TestBroadcast:
    def test_broadcast_to_receivers(self, srv):
        """broadcast sends to all registered receivers."""
        import io

        class FakeHandler:
            def __init__(self):
                self.request = io.BytesIO()
                self.client_address = ("fake", 0)
                self.send_lock = threading.Lock()

        h1 = FakeHandler()
        h1.request.sendall = h1.request.write
        h2 = FakeHandler()
        h2.request.sendall = h2.request.write

        srv.receivers.add(h1)
        srv.receivers.add(h2)

        srv.broadcast(
            {
                "type": "event",
                "seq": 1,
                "event": {"k": "ensure_prim", "prim": "/A", "typeName": "Xform"},
            }
        )
        srv._broadcast_queue.join()  # wait for async broadcast thread

        from openusdconnect.codec import message_to_dict
        from openusdconnect.framing import recv_framed_rfile

        for h in (h1, h2):
            h.request.seek(0)
            buf = recv_framed_rfile(h.request)
            msg = message_to_dict(buf)
            assert msg["type"] == "event"
            assert msg["event"]["prim"] == "/A"

    def test_broadcast_removes_dead_receivers(self, srv):
        """broadcast discards receivers whose socket is broken."""

        class DeadHandler:
            client_address = ("dead", 0)
            send_lock = threading.Lock()

            class request:
                @staticmethod
                def sendall(data):
                    raise OSError("broken pipe")

        h = DeadHandler()
        srv.receivers.add(h)
        srv.broadcast(
            {
                "type": "event",
                "seq": 1,
                "event": {"k": "ensure_prim", "prim": "/X", "typeName": "Xform"},
            }
        )
        srv._broadcast_queue.join()  # wait for async broadcast thread
        assert h not in srv.receivers

    def test_broadcast_targets_negotiated_receiver_audience(self, srv):
        import io

        class FakeHandler:
            def __init__(self, layered):
                self.request = io.BytesIO()
                self.request.sendall = self.request.write
                self.client_address = ("fake", int(layered))
                self.send_lock = threading.Lock()
                self._layered_replay = layered

        flat = FakeHandler(False)
        layered = FakeHandler(True)
        srv.receivers.update((flat, layered))
        message = {
            "type": "event",
            "seq": 1,
            "event": {"k": "ensure_prim", "prim": "/A", "typeName": "Xform"},
        }

        srv.broadcast(message, audience="layered")
        srv._broadcast_queue.join()
        assert flat.request.getvalue() == b""
        assert layered.request.getvalue()

        flat.request.seek(0)
        flat.request.truncate()
        layered.request.seek(0)
        layered.request.truncate()
        srv.broadcast(message, audience="flat")
        srv._broadcast_queue.join()
        assert flat.request.getvalue()
        assert layered.request.getvalue() == b""

    def test_layered_transaction_skips_flat_wire_but_not_listeners(
        self,
        srv,
        monkeypatch,
    ):
        import io

        class LayeredHandler:
            def __init__(self):
                self.request = io.BytesIO()
                self.request.sendall = self.request.write
                self.client_address = ("layered", 1)
                self.send_lock = threading.Lock()
                self._layered_replay = True

        layered = LayeredHandler()
        srv.receivers.add(layered)
        observed = []
        srv.add_event_listener(observed.append)

        audiences = []
        send_to_all = srv._send_to_all

        def _record_audience(
            payload,
            exclude_origin=None,
            target_origin=None,
            audience="all",
        ):
            audiences.append(audience)
            return send_to_all(
                payload,
                exclude_origin=exclude_origin,
                target_origin=target_origin,
                audience=audience,
            )

        monkeypatch.setattr(srv, "_send_to_all", _record_audience)
        event = {
            "k": "ensure_prim",
            "prim": "/LayeredOnly",
            "typeName": "Xform",
        }
        records, changed = srv.process_txn([event])

        srv.broadcast_transaction_views(records, changed, [event])
        srv._broadcast_queue.join()

        assert audiences == ["layered"]
        assert observed == [records[0][0]]
        assert layered.request.getvalue()

    def test_layered_transaction_includes_the_authoring_origin(self, srv):
        import io

        class FakeHandler:
            def __init__(self, layered):
                self.request = io.BytesIO()
                self.request.sendall = self.request.write
                self.client_address = ("fake", int(layered))
                self.send_lock = threading.Lock()
                self._layered_replay = layered
                self._origin = "shared-session"

        flat = FakeHandler(False)
        layered = FakeHandler(True)
        srv.receivers.update((flat, layered))
        event = {
            "k": "ensure_prim",
            "prim": "/LayeredEcho",
            "typeName": "Xform",
        }
        records, changed = srv.process_txn(
            [event],
            client_id="author",
            origin="shared-session",
        )

        srv.broadcast_transaction_views(
            records,
            changed,
            [event],
            exclude_origin="shared-session",
        )
        srv._broadcast_queue.join()

        assert layered.request.getvalue()
        assert flat.request.getvalue() == b""


# ---------------------------------------------------------------------------
# DB resume
# ---------------------------------------------------------------------------


class TestDBResume:
    def test_resumes_seq_from_existing_db(self, tmp_path):
        """Server resumes sequence counter from existing DB."""
        db = str(tmp_path / "resume.db")

        # First server writes events
        s1 = UsdSyncServer(log_path=db)
        for i in range(5):
            seq = s1.assign_seq()
            s1.append_log(
                {
                    "type": "event",
                    "seq": seq,
                    "event": {"k": "ensure_prim", "prim": f"/P{i}", "typeName": "Xform"},
                    "layer_key": "default",
                }
            )
        s1.store.close()

        # Second server should resume from seq 6
        s2 = UsdSyncServer(log_path=db)
        assert s2.assign_seq() == 6
        s2.store.close()

    def test_restores_stage_from_log(self, tmp_path):
        """Server restores stage state from the event log on startup."""
        db = str(tmp_path / "restore.db")

        # First server: create prims and apply events
        s1 = UsdSyncServer(log_path=db)
        events = [
            {"k": "ensure_prim", "prim": "/World", "typeName": "Xform"},
            {"k": "ensure_prim", "prim": "/World/Box", "typeName": "Cube"},
            {"k": "ensure_xform_ops", "prim": "/World/Box"},
            {"k": "set_xform_trs", "prim": "/World/Box", "fields": ["t"], "t": [5.0, 0.0, 0.0]},
        ]
        s1.apply_txn(events)
        for ev in events:
            seq = s1.assign_seq()
            s1.append_log(
                {
                    "type": "event",
                    "seq": seq,
                    "event": ev,
                    "layer_key": "default",
                }
            )
        s1.store.close()

        # Second server: stage should be restored from log
        s2 = UsdSyncServer(log_path=db)
        prim = s2.stage.GetPrimAtPath("/World/Box")
        assert prim.IsValid()
        assert prim.GetTypeName() == "Cube"
        # Verify transform was restored
        xf = UsdGeom.Xformable(prim)
        local = xf.GetLocalTransformation(Usd.TimeCode.Default())
        if isinstance(local, tuple):
            local = local[0]
        t = local.ExtractTranslation()
        assert abs(t[0] - 5.0) < 1e-6
        s2.store.close()


# ---------------------------------------------------------------------------
# Edit layer (non-destructive override sublayer)
# ---------------------------------------------------------------------------


class TestEditLayer:
    def test_edit_layer_is_not_root_layer(self, srv):
        """The edit layer must be a separate layer from the root."""
        assert srv.edit_layer is not None
        assert srv.edit_layer.identifier != srv.stage.GetRootLayer().identifier

    def test_edit_layer_is_strongest_session_sublayer(self, srv):
        """The edit layer is inserted at position 0 (strongest) on the session layer."""
        session = srv.stage.GetSessionLayer()
        assert len(session.subLayerPaths) >= 1
        assert session.subLayerPaths[0] == srv.edit_layer.identifier

    def test_edit_target_is_edit_layer(self, srv):
        """The stage's edit target points to the edit layer, not root."""
        target_layer = srv.stage.GetEditTarget().GetLayer()
        assert target_layer.identifier == srv.edit_layer.identifier

    def test_base_layer_untouched_after_apply(self, srv):
        """Applying events must not add specs to the root layer."""
        root = srv.stage.GetRootLayer()
        root_specs_before = set(root.rootPrims.keys())

        srv.apply_txn(
            [
                {"k": "ensure_prim", "prim": "/World/Sphere", "typeName": "Sphere"},
                {"k": "ensure_xform_ops", "prim": "/World/Sphere"},
                {
                    "k": "set_xform_trs",
                    "prim": "/World/Sphere",
                    "fields": ["t"],
                    "t": [5.0, 0.0, 0.0],
                },
            ]
        )

        root_specs_after = set(root.rootPrims.keys())
        assert root_specs_after == root_specs_before
        # But the composed stage sees the prim
        prim = srv.stage.GetPrimAtPath("/World/Sphere")
        assert prim.IsValid()

    def test_edits_land_in_edit_layer(self, srv):
        """Applied events should create specs in the edit layer."""
        srv.apply_txn(
            [
                {"k": "ensure_prim", "prim": "/World/Box", "typeName": "Cube"},
            ]
        )

        spec = srv.edit_layer.GetPrimAtPath("/World/Box")
        assert spec is not None

    def test_apply_txn_with_explicit_layer(self, srv):
        """Passing an explicit layer routes edits there instead."""
        alt_layer = Sdf.Layer.CreateAnonymous("alt-edits")
        session = srv.stage.GetSessionLayer()
        session.subLayerPaths.insert(0, alt_layer.identifier)

        srv.apply_txn(
            [{"k": "ensure_prim", "prim": "/World/Alt", "typeName": "Xform"}],
            layer=alt_layer,
        )

        # Landed in the explicit layer, not the default edit layer
        assert alt_layer.GetPrimAtPath("/World/Alt") is not None
        assert srv.edit_layer.GetPrimAtPath("/World/Alt") is None
        # Composed stage still sees it
        assert srv.stage.GetPrimAtPath("/World/Alt").IsValid()


class TestEditLayerWithBaseFile:
    """Verify non-destructive editing when the server opens a base USD file."""

    @pytest.fixture
    def base_srv(self, tmp_path):
        """Create a base USD with prims, then start a server on it."""
        base_path = str(tmp_path / "base.usda")
        # Create a base file with existing content
        base_stage = Usd.Stage.CreateNew(base_path)
        base_stage.DefinePrim("/World", "Xform")
        chair = base_stage.DefinePrim("/World/Chair", "Xform")
        xf = UsdGeom.Xformable(chair)
        xf.AddTranslateOp().Set(Gf.Vec3d(3.0, 0.0, 0.0))
        base_stage.GetRootLayer().Save()
        del base_stage  # close it

        db = str(tmp_path / "test.db")
        srv = UsdSyncServer(base_usd_path=base_path, log_path=db)
        yield srv, base_path
        srv.store.close()

    def test_base_file_untouched_after_edits(self, base_srv):
        """Server edits must not modify the base file's layer."""
        srv, base_path = base_srv
        base_layer = srv.stage.GetRootLayer()

        srv.apply_txn(
            [
                {"k": "ensure_prim", "prim": "/World/Table", "typeName": "Xform"},
                {"k": "ensure_xform_ops", "prim": "/World/Table"},
                {
                    "k": "set_xform_trs",
                    "prim": "/World/Table",
                    "fields": ["t"],
                    "t": [-2.0, 0.0, 0.0],
                },
            ]
        )

        # Table should NOT be in the base layer
        assert base_layer.GetPrimAtPath("/World/Table") is None
        # But IS visible on the composed stage
        assert srv.stage.GetPrimAtPath("/World/Table").IsValid()

    def test_server_edits_compose_over_base(self, base_srv):
        """Server override opinions win over base layer opinions."""
        srv, _ = base_srv

        # Override Chair's position (originally at (3, 0, 0))
        srv.apply_txn(
            [
                {"k": "ensure_xform_ops", "prim": "/World/Chair"},
                {
                    "k": "set_xform_trs",
                    "prim": "/World/Chair",
                    "fields": ["t"],
                    "t": [10.0, 0.0, 0.0],
                },
            ]
        )

        # Composed value should be the override
        prim = srv.stage.GetPrimAtPath("/World/Chair")
        xf = UsdGeom.Xformable(prim)
        local = xf.GetLocalTransformation(Usd.TimeCode.Default())
        if isinstance(local, tuple):
            local = local[0]
        t = local.ExtractTranslation()
        assert abs(t[0] - 10.0) < 1e-6

        # Base layer should still have original value
        base_layer = srv.stage.GetRootLayer()
        base_attr = base_layer.GetAttributeAtPath("/World/Chair.xformOp:translate")
        assert base_attr is not None
        assert abs(base_attr.default[0] - 3.0) < 1e-6

    def test_nested_base_layers_untouched(self, tmp_path):
        """Server with a base that has sublayers — all original layers stay clean."""
        # Create sublayer: layout.usda
        layout_path = str(tmp_path / "layout.usda")
        layout_stage = Usd.Stage.CreateNew(layout_path)
        layout_stage.DefinePrim("/World", "Xform")
        chair = layout_stage.DefinePrim("/World/Chair", "Xform")
        UsdGeom.Xformable(chair).AddTranslateOp().Set(Gf.Vec3d(3.0, 0.0, 0.0))
        layout_stage.GetRootLayer().Save()
        del layout_stage

        # Create root: shot.usda that sublayers layout.usda
        shot_path = str(tmp_path / "shot.usda")
        shot_layer = Sdf.Layer.CreateNew(shot_path)
        shot_layer.subLayerPaths = ["./layout.usda"]
        shot_layer.Save()

        db = str(tmp_path / "test.db")
        srv = UsdSyncServer(base_usd_path=shot_path, log_path=db)

        # Server applies edits
        srv.apply_txn(
            [
                {"k": "ensure_prim", "prim": "/World/Table", "typeName": "Xform"},
                {"k": "ensure_xform_ops", "prim": "/World/Table"},
                {
                    "k": "set_xform_trs",
                    "prim": "/World/Table",
                    "fields": ["t"],
                    "t": [0.0, 5.0, 0.0],
                },
            ]
        )

        # Composed stage sees both Chair (from layout) and Table (from edit layer)
        assert srv.stage.GetPrimAtPath("/World/Chair").IsValid()
        assert srv.stage.GetPrimAtPath("/World/Table").IsValid()

        # Neither the root layer nor the sublayer has Table
        shot_layer_live = srv.stage.GetRootLayer()
        assert shot_layer_live.GetPrimAtPath("/World/Table") is None

        layout_layer = Sdf.Layer.Find(layout_path)
        assert layout_layer.GetPrimAtPath("/World/Table") is None

        # Table is only in the edit layer
        assert srv.edit_layer.GetPrimAtPath("/World/Table") is not None

        srv.store.close()


class TestExportEditLayer:
    def test_export_empty(self, srv):
        """Exporting with no edits returns valid minimal USDA."""
        usda = srv.export_edit_layer()
        assert "#usda 1.0" in usda

    def test_export_contains_edits(self, srv):
        """Exported USDA contains authored opinions."""
        srv.apply_txn(
            [
                {"k": "ensure_prim", "prim": "/World/Mesh", "typeName": "Mesh"},
            ]
        )
        usda = srv.export_edit_layer()
        assert "Mesh" in usda
        assert "World" in usda

    def test_export_to_file(self, srv, tmp_path):
        """Exported file composes correctly with a base stage."""
        srv.apply_txn(
            [
                {"k": "ensure_prim", "prim": "/World/Exported", "typeName": "Xform"},
                {"k": "ensure_xform_ops", "prim": "/World/Exported"},
                {
                    "k": "set_xform_trs",
                    "prim": "/World/Exported",
                    "fields": ["t"],
                    "t": [7.0, 8.0, 9.0],
                },
            ]
        )

        diff_path = str(tmp_path / "diff.usda")
        srv.export_edit_layer(diff_path)

        # Re-open the diff as a standalone layer and verify content
        diff_layer = Sdf.Layer.FindOrOpen(diff_path)
        assert diff_layer is not None
        assert diff_layer.GetPrimAtPath("/World/Exported") is not None

    def test_export_flattened_matches_composed(self, tmp_path):
        """Flattened export produces identical values to the live composed stage."""
        # Build a base file with existing content
        base_path = str(tmp_path / "base.usda")
        base_stage = Usd.Stage.CreateNew(base_path)
        base_stage.DefinePrim("/World", "Xform")
        chair = base_stage.DefinePrim("/World/Chair", "Xform")
        UsdGeom.Xformable(chair).AddTranslateOp().Set(Gf.Vec3d(3.0, 0.0, 0.0))
        table = base_stage.DefinePrim("/World/Table", "Xform")
        UsdGeom.Xformable(table).AddTranslateOp().Set(Gf.Vec3d(-2.0, 0.0, 0.0))
        base_stage.GetRootLayer().Save()
        del base_stage

        db = str(tmp_path / "test.db")
        srv = UsdSyncServer(base_usd_path=base_path, log_path=db)

        # Server overrides Chair, adds a new prim
        srv.apply_txn(
            [
                {"k": "ensure_xform_ops", "prim": "/World/Chair"},
                {
                    "k": "set_xform_trs",
                    "prim": "/World/Chair",
                    "fields": ["t"],
                    "t": [10.0, 0.0, 0.0],
                },
                {"k": "ensure_prim", "prim": "/World/Lamp", "typeName": "Xform"},
                {"k": "ensure_xform_ops", "prim": "/World/Lamp"},
                {
                    "k": "set_xform_trs",
                    "prim": "/World/Lamp",
                    "fields": ["t"],
                    "t": [0.0, 5.0, 0.0],
                },
            ]
        )

        # Collect composed values from the live stage
        def get_translate(stage, path):
            prim = stage.GetPrimAtPath(path)
            if not prim.IsValid():
                return None
            xf = UsdGeom.Xformable(prim)
            local = xf.GetLocalTransformation(Usd.TimeCode.Default())
            if isinstance(local, tuple):
                local = local[0]
            t = local.ExtractTranslation()
            return (t[0], t[1], t[2])

        paths = ["/World", "/World/Chair", "/World/Table", "/World/Lamp"]
        composed = {p: get_translate(srv.stage, p) for p in paths}

        # Export flattened
        flat_path = str(tmp_path / "flat.usda")
        srv.export_flattened(flat_path)
        flat_stage = Usd.Stage.Open(flat_path)

        # Every prim and transform must match exactly
        for path in paths:
            flat_t = get_translate(flat_stage, path)
            assert flat_t is not None, f"{path} missing in flattened stage"
            for i in range(3):
                assert abs(composed[path][i] - flat_t[i]) < 1e-6, (
                    f"{path}[{i}]: composed={composed[path][i]} flat={flat_t[i]}"
                )

        # No sublayers in the flattened file
        assert len(flat_stage.GetRootLayer().subLayerPaths) == 0

        srv.store.close()


class TestNestedLayerEditing:
    """Simulate multi-user editing of specific layers in a nested stack.

    Scene layout:
        shot.usda (root)
          ├── animation.usda  (defines /World/Chair anim override)
          │     └── char_anim.usda  (defines /World/Chair keyframe)
          └── layout.usda     (defines /World/Chair + /World/Table)

    Two "users" (Alice and Bob) target different layers via apply_txn(layer=...).
    """

    @pytest.fixture
    def nested_srv(self, tmp_path):
        # layout.usda — base positions
        layout_path = str(tmp_path / "layout.usda")
        layout_stage = Usd.Stage.CreateNew(layout_path)
        layout_stage.DefinePrim("/World", "Xform")
        chair = layout_stage.DefinePrim("/World/Chair", "Xform")
        UsdGeom.Xformable(chair).AddTranslateOp().Set(Gf.Vec3d(3.0, 0.0, 0.0))
        table = layout_stage.DefinePrim("/World/Table", "Xform")
        UsdGeom.Xformable(table).AddTranslateOp().Set(Gf.Vec3d(-2.0, 0.0, 0.0))
        layout_stage.GetRootLayer().Save()
        del layout_stage

        # char_anim.usda — character keyframe
        char_path = str(tmp_path / "char_anim.usda")
        char_stage = Usd.Stage.CreateNew(char_path)
        char_stage.OverridePrim("/World")
        char_prim = char_stage.OverridePrim("/World/Chair")
        UsdGeom.Xformable(char_prim).AddTranslateOp().Set(Gf.Vec3d(3.0, 0.0, 5.0))
        char_stage.GetRootLayer().Save()
        del char_stage

        # animation.usda — sublayers char_anim.usda
        anim_path = str(tmp_path / "animation.usda")
        anim_layer = Sdf.Layer.CreateNew(anim_path)
        anim_layer.subLayerPaths = ["./char_anim.usda"]
        anim_layer.Save()

        # shot.usda — sublayers animation + layout
        shot_path = str(tmp_path / "shot.usda")
        shot_layer = Sdf.Layer.CreateNew(shot_path)
        shot_layer.subLayerPaths = ["./animation.usda", "./layout.usda"]
        shot_layer.Save()

        db = str(tmp_path / "test.db")
        srv = UsdSyncServer(base_usd_path=shot_path, log_path=db)
        yield srv, tmp_path
        srv.store.close()

    def _get_composed_translate(self, stage, prim_path):
        prim = stage.GetPrimAtPath(prim_path)
        xf = UsdGeom.Xformable(prim)
        local = xf.GetLocalTransformation(Usd.TimeCode.Default())
        if isinstance(local, tuple):
            local = local[0]
        return local.ExtractTranslation()

    def test_composed_baseline(self, nested_srv):
        """Before any edits, Chair=(3,0,5) from char_anim, Table=(-2,0,0) from layout."""
        srv, _ = nested_srv
        t_chair = self._get_composed_translate(srv.stage, "/World/Chair")
        t_table = self._get_composed_translate(srv.stage, "/World/Table")
        # char_anim is stronger than layout (via animation.usda sublayer ordering)
        assert abs(t_chair[2] - 5.0) < 1e-6
        assert abs(t_table[0] - (-2.0)) < 1e-6

    def test_two_users_different_layers(self, nested_srv):
        """Alice and Bob edit different session sublayers — both opinions visible."""
        srv, _ = nested_srv

        alice_layer = Sdf.Layer.CreateAnonymous("alice")
        bob_layer = Sdf.Layer.CreateAnonymous("bob")
        session = srv.stage.GetSessionLayer()
        # Alice is stronger (inserted first = position 0 after bob)
        session.subLayerPaths.append(alice_layer.identifier)
        session.subLayerPaths.append(bob_layer.identifier)

        # Alice moves Table
        srv.apply_txn(
            [
                {"k": "ensure_xform_ops", "prim": "/World/Table"},
                {
                    "k": "set_xform_trs",
                    "prim": "/World/Table",
                    "fields": ["t"],
                    "t": [0.0, 10.0, 0.0],
                },
            ],
            layer=alice_layer,
        )

        # Bob moves Chair
        srv.apply_txn(
            [
                {"k": "ensure_xform_ops", "prim": "/World/Chair"},
                {
                    "k": "set_xform_trs",
                    "prim": "/World/Chair",
                    "fields": ["t"],
                    "t": [0.0, 0.0, 20.0],
                },
            ],
            layer=bob_layer,
        )

        # Both edits are visible in the composed stage
        t_table = self._get_composed_translate(srv.stage, "/World/Table")
        t_chair = self._get_composed_translate(srv.stage, "/World/Chair")
        assert abs(t_table[1] - 10.0) < 1e-6
        assert abs(t_chair[2] - 20.0) < 1e-6

        # Opinions landed in the correct layers
        assert alice_layer.GetPrimAtPath("/World/Table") is not None
        assert alice_layer.GetPrimAtPath("/World/Chair") is None
        assert bob_layer.GetPrimAtPath("/World/Chair") is not None
        assert bob_layer.GetPrimAtPath("/World/Table") is None

    def test_stronger_layer_wins_on_same_prim(self, nested_srv):
        """When two users edit the same prim, the stronger layer wins."""
        srv, _ = nested_srv

        alice_layer = Sdf.Layer.CreateAnonymous("alice")
        bob_layer = Sdf.Layer.CreateAnonymous("bob")
        session = srv.stage.GetSessionLayer()
        # Alice's layer is stronger (earlier in list)
        session.subLayerPaths.append(alice_layer.identifier)
        session.subLayerPaths.append(bob_layer.identifier)

        # Bob edits Chair first
        srv.apply_txn(
            [
                {"k": "ensure_xform_ops", "prim": "/World/Chair"},
                {
                    "k": "set_xform_trs",
                    "prim": "/World/Chair",
                    "fields": ["t"],
                    "t": [0.0, 0.0, 99.0],
                },
            ],
            layer=bob_layer,
        )

        # Alice edits Chair after
        srv.apply_txn(
            [
                {"k": "ensure_xform_ops", "prim": "/World/Chair"},
                {
                    "k": "set_xform_trs",
                    "prim": "/World/Chair",
                    "fields": ["t"],
                    "t": [0.0, 0.0, 42.0],
                },
            ],
            layer=alice_layer,
        )

        # Alice wins — her layer is stronger regardless of write order
        t = self._get_composed_translate(srv.stage, "/World/Chair")
        assert abs(t[2] - 42.0) < 1e-6

        # But Bob's opinion still exists in his layer
        bob_attr = bob_layer.GetAttributeAtPath("/World/Chair.xformOp:translate")
        assert bob_attr is not None

    def test_session_edits_override_all_base_sublayers(self, nested_srv):
        """Session layer edits override the entire root layer stack."""
        srv, _ = nested_srv

        # Chair is at (3,0,5) from char_anim.usda — override via default edit layer
        srv.apply_txn(
            [
                {"k": "ensure_xform_ops", "prim": "/World/Chair"},
                {
                    "k": "set_xform_trs",
                    "prim": "/World/Chair",
                    "fields": ["t"],
                    "t": [0.0, 0.0, 0.0],
                },
            ]
        )

        t = self._get_composed_translate(srv.stage, "/World/Chair")
        assert abs(t[0]) < 1e-6
        assert abs(t[1]) < 1e-6
        assert abs(t[2]) < 1e-6

        # Original layers untouched
        srv_root = srv.stage.GetRootLayer()
        # shot.usda has no Chair spec (it's in sublayers)
        assert srv_root.GetPrimAtPath("/World/Chair") is None

    def test_base_sublayers_preserve_original_opinions(self, nested_srv):
        """All original base sublayer opinions survive server edits."""
        srv, tmp_path = nested_srv

        srv.apply_txn(
            [
                {"k": "ensure_prim", "prim": "/World/NewPrim", "typeName": "Xform"},
            ]
        )

        # layout.usda still has Chair and Table
        layout_layer = Sdf.Layer.Find(str(tmp_path / "layout.usda"))
        assert layout_layer.GetPrimAtPath("/World/Chair") is not None
        assert layout_layer.GetPrimAtPath("/World/Table") is not None

        # char_anim.usda still has Chair override
        char_layer = Sdf.Layer.Find(str(tmp_path / "char_anim.usda"))
        assert char_layer.GetPrimAtPath("/World/Chair") is not None

        # None of them have NewPrim
        assert layout_layer.GetPrimAtPath("/World/NewPrim") is None
        assert char_layer.GetPrimAtPath("/World/NewPrim") is None

    def test_nested_user_layers_parent_wins(self, nested_srv):
        """Bob's layer is nested inside Alice's — Alice's opinions are stronger.

        Session structure:
            sessionLayer
              └── alice
                    └── subLayers:
                          └── bob   (weaker — child sublayer of alice)
        """
        srv, tmp_path = nested_srv

        alice_layer = Sdf.Layer.CreateAnonymous("alice")
        bob_layer = Sdf.Layer.CreateAnonymous("bob")
        # Bob is a sublayer OF Alice (weaker)
        alice_layer.subLayerPaths.append(bob_layer.identifier)
        # Alice is on the session
        session = srv.stage.GetSessionLayer()
        session.subLayerPaths.append(alice_layer.identifier)

        # Bob edits Chair
        srv.apply_txn(
            [
                {"k": "ensure_xform_ops", "prim": "/World/Chair"},
                {
                    "k": "set_xform_trs",
                    "prim": "/World/Chair",
                    "fields": ["t"],
                    "t": [0.0, 0.0, 99.0],
                },
            ],
            layer=bob_layer,
        )

        # Before Alice edits, Bob's value is visible
        t = self._get_composed_translate(srv.stage, "/World/Chair")
        assert abs(t[2] - 99.0) < 1e-6

        # Alice edits the same prim on her (parent) layer
        srv.apply_txn(
            [
                {"k": "ensure_xform_ops", "prim": "/World/Chair"},
                {
                    "k": "set_xform_trs",
                    "prim": "/World/Chair",
                    "fields": ["t"],
                    "t": [0.0, 0.0, 7.0],
                },
            ],
            layer=alice_layer,
        )

        # Alice wins — parent layer is stronger than child sublayer
        t = self._get_composed_translate(srv.stage, "/World/Chair")
        assert abs(t[2] - 7.0) < 1e-6

        # Bob's opinion still exists, just shadowed
        bob_attr = bob_layer.GetAttributeAtPath("/World/Chair.xformOp:translate")
        assert bob_attr is not None

        # Export both diffs, compose them onto the original base, verify result
        alice_diff = Sdf.Layer.CreateAnonymous("alice-diff")
        alice_diff.TransferContent(alice_layer)
        bob_diff = Sdf.Layer.CreateAnonymous("bob-diff")
        bob_diff.TransferContent(bob_layer)

        fresh_stage = Usd.Stage.Open(str(tmp_path / "shot.usda"))
        root = fresh_stage.GetRootLayer()
        # Compose: alice (strongest) → bob → base stack
        root.subLayerPaths.insert(0, bob_diff.identifier)
        root.subLayerPaths.insert(0, alice_diff.identifier)

        t = self._get_composed_translate(fresh_stage, "/World/Chair")
        # Alice's (0,0,7) wins over Bob's (0,0,99)
        assert abs(t[2] - 7.0) < 1e-6

    def test_nested_user_layers_no_overlap(self, nested_srv):
        """Bob nested under Alice, editing different prims — both visible.

        Session structure:
            sessionLayer
              └── alice        (edits /World/Table)
                    └── bob    (edits /World/Chair)
        """
        srv, tmp_path = nested_srv

        alice_layer = Sdf.Layer.CreateAnonymous("alice")
        bob_layer = Sdf.Layer.CreateAnonymous("bob")
        alice_layer.subLayerPaths.append(bob_layer.identifier)
        session = srv.stage.GetSessionLayer()
        session.subLayerPaths.append(alice_layer.identifier)

        # Alice edits Table
        srv.apply_txn(
            [
                {"k": "ensure_xform_ops", "prim": "/World/Table"},
                {
                    "k": "set_xform_trs",
                    "prim": "/World/Table",
                    "fields": ["t"],
                    "t": [0.0, 50.0, 0.0],
                },
            ],
            layer=alice_layer,
        )

        # Bob edits Chair
        srv.apply_txn(
            [
                {"k": "ensure_xform_ops", "prim": "/World/Chair"},
                {
                    "k": "set_xform_trs",
                    "prim": "/World/Chair",
                    "fields": ["t"],
                    "t": [0.0, 0.0, 60.0],
                },
            ],
            layer=bob_layer,
        )

        # Both visible — no conflict
        t_table = self._get_composed_translate(srv.stage, "/World/Table")
        t_chair = self._get_composed_translate(srv.stage, "/World/Chair")
        assert abs(t_table[1] - 50.0) < 1e-6
        assert abs(t_chair[2] - 60.0) < 1e-6

        # Opinions in correct layers
        assert alice_layer.GetPrimAtPath("/World/Table") is not None
        assert alice_layer.GetPrimAtPath("/World/Chair") is None
        assert bob_layer.GetPrimAtPath("/World/Chair") is not None
        assert bob_layer.GetPrimAtPath("/World/Table") is None

        # Export both diffs, compose onto a fresh base, verify result
        alice_diff = Sdf.Layer.CreateAnonymous("alice-diff")
        alice_diff.TransferContent(alice_layer)
        bob_diff = Sdf.Layer.CreateAnonymous("bob-diff")
        bob_diff.TransferContent(bob_layer)

        fresh_stage = Usd.Stage.Open(str(tmp_path / "shot.usda"))
        root = fresh_stage.GetRootLayer()
        # Alice's diff sublayers bob's diff (same nesting as session)
        alice_diff.subLayerPaths.append(bob_diff.identifier)
        root.subLayerPaths.insert(0, alice_diff.identifier)

        t_table = self._get_composed_translate(fresh_stage, "/World/Table")
        t_chair = self._get_composed_translate(fresh_stage, "/World/Chair")
        assert abs(t_table[1] - 50.0) < 1e-6  # Alice's edit
        assert abs(t_chair[2] - 60.0) < 1e-6  # Bob's edit
        # Unedited prims still have original base values
        # Chair's original translate from layout is (3,0,0) but char_anim overrides to (3,0,5)
        # Bob's edit replaces it entirely with (0,0,60)
        assert abs(t_chair[0]) < 1e-6


class TestCompactionWithEditLayer:
    def _insert_events(self, srv, events):
        for ev in events:
            seq = srv.assign_seq()
            rec = {"type": "event", "seq": seq, "event": ev}
            if ev["k"] not in SHARED_STAGE_KINDS:
                rec["layer_key"] = "default"
            srv.append_log(rec)
        srv.apply_txn(events)

    def test_compaction_preserves_edit_layer_isolation(self, srv):
        """After compaction, root layer is still untouched."""
        self._insert_events(
            srv,
            [
                {"k": "ensure_prim", "prim": "/A", "typeName": "Xform"},
                {"k": "ensure_xform_ops", "prim": "/A"},
                {"k": "set_xform_trs", "prim": "/A", "fields": ["t"], "t": [1, 0, 0]},
            ],
        )
        self._insert_events(
            srv,
            [
                {"k": "set_xform_trs", "prim": "/A", "fields": ["t"], "t": [5, 0, 0]},
            ],
        )

        srv.compact_log()

        # Root layer has no specs for /A
        root = srv.stage.GetRootLayer()
        assert root.GetPrimAtPath("/A") is None
        # Edit layer has the specs
        assert srv.edit_layer.GetPrimAtPath("/A") is not None
        # Composed stage is correct
        assert srv.stage.GetPrimAtPath("/A").IsValid()


class TestTokenBucket:
    def test_burst_allows_immediate(self):
        """Burst tokens are available immediately."""
        tb = TokenBucket(rate=10.0, burst=5)
        for _ in range(5):
            assert tb.try_consume() == 0.0
        # 6th should be denied
        assert tb.try_consume() > 0.0

    def test_refill_over_time(self):
        """Tokens refill at the configured rate."""
        tb = TokenBucket(rate=10.0, burst=5)
        # Exhaust burst
        for _ in range(5):
            tb.try_consume()
        assert tb.try_consume() > 0.0
        # Simulate 0.5s passing (10/s * 0.5s = 5 tokens)
        tb._last -= 0.5
        assert tb.try_consume() == 0.0

    def test_does_not_exceed_burst(self):
        """Tokens cap at burst even after long idle."""
        tb = TokenBucket(rate=1000.0, burst=3)
        # Simulate 10s passing — would refill 10000, but capped at burst=3
        tb._last -= 10.0
        for _ in range(3):
            assert tb.try_consume() == 0.0
        assert tb.try_consume() > 0.0

    def test_wait_time_is_positive(self):
        """Denied consume returns a positive wait estimate."""
        tb = TokenBucket(rate=10.0, burst=1)
        tb.try_consume()
        wait = tb.try_consume()
        assert wait == pytest.approx(0.1, abs=0.01)  # 1 token / 10 per sec


class TestRateLimitedServer:
    def test_rate_limit_params_stored(self, tmp_path):
        """txn_rate and txn_burst are stored on the server."""
        db = str(tmp_path / "rl.db")
        s = UsdSyncServer(log_path=db, txn_rate=50.0, txn_burst=100)
        assert s.txn_rate == 50.0
        assert s.txn_burst == 100
        s.store.close()

    def test_zero_rate_disables(self, tmp_path):
        """txn_rate=0 means no rate limiting (default)."""
        db = str(tmp_path / "rl2.db")
        s = UsdSyncServer(log_path=db)
        assert s.txn_rate == 0
        assert s.txn_burst == 0
        s.store.close()


# ---------------------------------------------------------------------------
# Event store prim-prefix query
# ---------------------------------------------------------------------------


class TestGetByPrimPrefix:
    def _append(self, srv, seq, kind, prim):
        ev = {"k": kind, "prim": prim}
        if kind == "ensure_prim":
            ev["typeName"] = "Xform"
        elif kind == "set_visibility":
            ev["visible"] = True
        srv.append_log(
            {
                "type": "event",
                "seq": seq,
                "event": ev,
                "layer_key": "default",
            }
        )

    def test_filters_by_prefix_and_kind(self, srv):
        self._append(srv, 1, "ensure_prim", "/World/Asset/A")
        self._append(srv, 2, "ensure_prim", "/World/Asset/B")
        self._append(srv, 3, "ensure_prim", "/World/Other")
        self._append(srv, 4, "set_visibility", "/World/Asset/A")
        self._append(srv, 5, "delete_prim", "/World/Asset/A")

        blobs = srv.store.get_by_prim_prefix(
            "/World/Asset/", {"ensure_prim", "set_visibility"}
        )
        decoded = [message_to_dict(b)["event"] for b in blobs]
        assert [(e["k"], e["prim"]) for e in decoded] == [
            ("ensure_prim", "/World/Asset/A"),
            ("ensure_prim", "/World/Asset/B"),
            ("set_visibility", "/World/Asset/A"),
        ]

    def test_prefix_does_not_match_siblings(self, srv):
        self._append(srv, 1, "ensure_prim", "/World/Asset/Child")
        self._append(srv, 2, "ensure_prim", "/World/AssetB")

        blobs = srv.store.get_by_prim_prefix("/World/Asset/", {"ensure_prim"})
        decoded = [message_to_dict(b)["event"]["prim"] for b in blobs]
        assert decoded == ["/World/Asset/Child"]

    def test_empty_kinds_returns_nothing(self, srv):
        self._append(srv, 1, "ensure_prim", "/World/Asset/A")
        assert srv.store.get_by_prim_prefix("/World/Asset/", set()) == []


# ---------------------------------------------------------------------------
# apply_txn single-layer fast path
# ---------------------------------------------------------------------------


class TestApplyTxnSingleLayerFastPath:
    def test_skips_strength_checks(self, srv, monkeypatch):
        """With only the edit layer in the session stack, apply_txn must not
        run per-event strength checks at all."""

        def _boom(*args, **kwargs):
            raise AssertionError("_is_layer_winning called on single-layer fast path")

        monkeypatch.setattr(srv, "_is_layer_winning", _boom)
        events = [
            {"k": "ensure_prim", "prim": "/World/X", "typeName": "Xform"},
            {"k": "set_visibility", "prim": "/World/X", "visible": False},
        ]
        changed = srv.apply_txn(events)
        assert changed == [0, 1]
        assert "/World/X" in srv._prim_paths

    def test_department_layers_still_check_strength(self, tmp_path, monkeypatch):
        """With department layers in the stack, strength checks still run."""
        db = str(tmp_path / "dept.db")
        s = UsdSyncServer(log_path=db, department_priority=["anim", "layout"])
        try:
            layer = s.get_or_create_client_layer("alice", department="anim")
            calls = []
            real = s._is_layer_winning

            def _spy(prim, target, ev):
                calls.append(ev.get("k"))
                return real(prim, target, ev)

            monkeypatch.setattr(s, "_is_layer_winning", _spy)
            s.apply_txn(
                [{"k": "ensure_prim", "prim": "/World/Y", "typeName": "Xform"}],
                layer=layer,
            )
            assert calls == ["ensure_prim"]
        finally:
            s.store.close()
