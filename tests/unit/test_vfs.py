"""Tests for the VFS provider (VirtualStageFile) — in-process, no HTTP.

Exercises snapshot generation, embedded connection metadata, cache/etag
invalidation, write policy, and the browsable multi-file VFS directory.
"""

import json

import pytest
from pxr import Sdf, Usd

from openusdconnect.protocol_constants import PROTOCOL_VERSION
from openusdconnect.server import UsdSyncServer
from openusdconnect.server.types import InvalidVfsWriteError
from openusdconnect.server.vfs import VirtualStageFile, VirtualStageFileSet, WriteMode


@pytest.fixture
def srv(tmp_path):
    """Create a UsdSyncServer with a temp SQLite DB."""
    db = str(tmp_path / "test.db")
    s = UsdSyncServer(log_path=db)
    yield s
    s.store.close()


@pytest.fixture
def dept_srv(tmp_path):
    """Create a server with composed department layers enabled."""
    db = str(tmp_path / "dept.db")
    s = UsdSyncServer(log_path=db, department_priority=["layout", "animation"])
    yield s
    s.store.close()


@pytest.fixture
def vfile(srv):
    return VirtualStageFile(
        srv,
        name="live.usd",
        advertise_host="127.0.0.1",
        sync_port=7200,
        department="layout",
    )


@pytest.fixture
def drop_vfile(srv):
    return VirtualStageFile(
        srv,
        name="live.usd",
        advertise_host="127.0.0.1",
        sync_port=7200,
        write_mode=WriteMode.DROP,
    )


@pytest.fixture
def drop_vfile_without_validation(srv):
    return VirtualStageFile(
        srv,
        name="live.usd",
        advertise_host="127.0.0.1",
        sync_port=7200,
        write_mode=WriteMode.DROP,
        validate_writes=False,
    )


@pytest.fixture
def translate_vfile(srv):
    return VirtualStageFile(
        srv,
        name="live.usd",
        advertise_host="127.0.0.1",
        sync_port=7200,
        write_mode=WriteMode.TRANSLATE,
    )


@pytest.fixture
def translate_vfile_without_validation(srv):
    return VirtualStageFile(
        srv,
        name="live.usd",
        advertise_host="127.0.0.1",
        sync_port=7200,
        write_mode=WriteMode.TRANSLATE,
        validate_writes=False,
    )


@pytest.fixture
def vset(srv):
    return VirtualStageFileSet(
        srv,
        flat_name="scene.usd",
        advertise_host="127.0.0.1",
        sync_port=7200,
        share="usd",
        vfs_base_url="http://127.0.0.1:7280/usd",
    )


def _send(srv, events):
    """Apply events through the same path a TCP txn takes (seq + persist)."""
    srv.process_txn(events, client_id="test-client", origin="test-origin")


def _send_to_layer(srv, layer, events, client_id="test-client"):
    """Apply events to an explicit edit layer and persist them."""
    srv.process_txn(events, client_id=client_id, origin="test-origin", layer=layer)


def _open_stage(data: bytes) -> Usd.Stage:
    layer = Sdf.Layer.CreateAnonymous(".usda")
    assert layer.ImportFromString(data.decode("utf-8"))
    return Usd.Stage.Open(layer)


def _stage_bytes(specs: list[tuple[str, str]]) -> bytes:
    layer = Sdf.Layer.CreateAnonymous(".usda")
    stage = Usd.Stage.Open(layer)
    for path, type_name in specs:
        stage.DefinePrim(path, type_name)
    return layer.ExportToString().encode("utf-8")


# ---------------------------------------------------------------------------
# Snapshot token (state.py additions)
# ---------------------------------------------------------------------------


class TestSnapshotToken:
    def test_initial(self, srv):
        assert srv.get_snapshot_token() == (0, 0)

    def test_seq_advances(self, srv):
        _send(srv, [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}])
        epoch, seq = srv.get_snapshot_token()
        assert epoch == 0
        assert seq == 1

    def test_epoch_bumps_on_compaction(self, srv):
        _send(srv, [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}])
        srv.compact_log()
        epoch, _seq = srv.get_snapshot_token()
        assert epoch == 1

    def test_epoch_bumps_on_purge(self, srv):
        _send(srv, [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}])
        srv.purge()
        assert srv.get_snapshot_token() == (1, 0)

    def test_epoch_bumps_on_department_layer_create(self, dept_srv):
        assert dept_srv.get_snapshot_token() == (0, 0)
        dept_srv.get_or_create_client_layer("alice", "layout")
        assert dept_srv.get_snapshot_token() == (1, 0)

    def test_epoch_bumps_on_mute_unmute(self, dept_srv):
        layer = dept_srv.get_or_create_client_layer("alice", "layout")
        _send_to_layer(
            dept_srv,
            layer,
            [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}],
            client_id="alice",
        )
        epoch, seq = dept_srv.get_snapshot_token()

        assert dept_srv.mute_layer("layout")
        assert dept_srv.get_snapshot_token() == (epoch + 1, seq)

        assert dept_srv.unmute_layer("layout")
        assert dept_srv.get_snapshot_token() == (epoch + 2, seq)

    def test_epoch_bumps_on_priority_change(self, dept_srv):
        dept_srv.get_or_create_client_layer("alice", "layout")
        epoch, seq = dept_srv.get_snapshot_token()
        dept_srv.set_department_priority(["animation", "layout"])
        assert dept_srv.get_snapshot_token() == (epoch + 1, seq)

    def test_epoch_bumps_on_merge_layer(self, dept_srv):
        layer = dept_srv.get_or_create_client_layer("alice", "layout")
        _send_to_layer(
            dept_srv,
            layer,
            [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}],
            client_id="alice",
        )
        epoch, seq = dept_srv.get_snapshot_token()
        assert dept_srv.merge_layer("alice")
        assert dept_srv.get_snapshot_token() == (epoch + 1, seq)

    def test_epoch_bumps_on_delete_layer(self, dept_srv):
        dept_srv.get_or_create_client_layer("alice", "layout")
        epoch, seq = dept_srv.get_snapshot_token()
        assert dept_srv.delete_layer("alice")
        assert dept_srv.get_snapshot_token() == (epoch + 1, seq)

    def test_epoch_bumps_on_proposal_reject(self, dept_srv):
        dept_srv.get_or_create_client_layer("lead", "layout")
        proposal_id = dept_srv.create_proposal("alice", "layout")
        epoch, seq = dept_srv.get_snapshot_token()
        assert dept_srv.reject_proposal(proposal_id)
        assert dept_srv.get_snapshot_token() == (epoch + 1, seq)

    def test_epoch_bumps_on_proposal_approve(self, dept_srv):
        dept_srv.get_or_create_client_layer("lead", "layout")
        proposal_id = dept_srv.create_proposal("alice", "layout")
        assert dept_srv.apply_proposal_txn(
            proposal_id,
            [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}],
        )
        epoch, _seq = dept_srv.get_snapshot_token()
        assert dept_srv.approve_proposal(proposal_id)
        assert dept_srv.get_snapshot_token() == (epoch + 1, 1)


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


class TestRead:
    def test_parses_as_usd(self, vfile):
        stage = _open_stage(vfile.read())
        assert stage.GetPrimAtPath("/Root")

    def test_content_tracks_server_state(self, srv, vfile):
        assert not _open_stage(vfile.read()).GetPrimAtPath("/World/Cube")
        _send(
            srv,
            [
                {"k": "ensure_prim", "prim": "/World", "typeName": "Xform"},
                {"k": "ensure_prim", "prim": "/World/Cube", "typeName": "Cube"},
            ],
        )
        stage = _open_stage(vfile.read())
        prim = stage.GetPrimAtPath("/World/Cube")
        assert prim and prim.GetTypeName() == "Cube"

    def test_stat_matches_read(self, vfile):
        st = vfile.stat()
        assert st.size == len(vfile.read())
        assert st.mtime > 0
        assert st.generation_ms >= 0


class TestMetadata:
    def test_fields(self, srv, vfile):
        _send(srv, [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}])
        stage = _open_stage(vfile.read())
        meta = stage.GetRootLayer().customLayerData["openusdconnect"]
        assert meta["live"] is True
        assert meta["host"] == "127.0.0.1"
        assert meta["port"] == 7200
        assert meta["protocol_version"] == PROTOCOL_VERSION
        assert meta["scene_id"] == srv.scene_id
        assert "vfs_url" in meta
        assert meta["department"] == "layout"
        assert meta["requires_token"] is False
        assert meta["snapshot_seq"] == srv.get_snapshot_token()[1]
        assert meta["epoch"] == 0
        assert meta["generated_at"]

    def test_snapshot_seq_never_overclaims(self, srv, vfile):
        """The snapshot must contain everything up to its snapshot_seq."""
        _send(srv, [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}])
        stage = _open_stage(vfile.read())
        meta = stage.GetRootLayer().customLayerData["openusdconnect"]
        # seq 1 was /World — and /World is present
        assert meta["snapshot_seq"] == 1
        assert stage.GetPrimAtPath("/World")


# ---------------------------------------------------------------------------
# Cache / etag
# ---------------------------------------------------------------------------


class TestCache:
    def test_stable_when_unchanged(self, vfile):
        a, b = vfile.read(), vfile.read()
        assert a is b  # cached object, not just equal bytes
        assert vfile.stat().etag == vfile.stat().etag

    def test_invalidates_on_txn(self, srv, vfile):
        a = vfile.read()
        etag_a = vfile.stat().etag
        _send(srv, [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}])
        b = vfile.read()
        assert a != b
        assert vfile.stat().etag != etag_a

    def test_epoch_disambiguates_recycled_seq(self, srv, vfile):
        """After compaction, a numerically equal seq must not serve stale bytes."""
        _send(srv, [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}])
        etag_before = vfile.stat().etag
        seq_before = srv.get_snapshot_token()[1]
        srv.compact_log()
        # compaction rewrites the log; seq count is the same here (1 event)
        assert srv.get_snapshot_token()[1] == seq_before
        assert vfile.stat().etag != etag_before

    def test_purge_invalidates(self, srv, vfile):
        _send(srv, [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}])
        assert _open_stage(vfile.read()).GetPrimAtPath("/World")
        srv.purge()
        data = vfile.read()
        stage = _open_stage(data)
        assert not stage.GetPrimAtPath("/World")


# ---------------------------------------------------------------------------
# Write side
# ---------------------------------------------------------------------------


class TestVirtualDirectory:
    def test_root_members(self, vset):
        assert vset.get_member_names("") == [
            "scene.usd",
            "scene.live.usda",
            "openusdconnect.json",
            "_layers",
        ]
        assert vset.is_collection("")
        assert vset.is_collection("_layers")

    def test_manifest(self, srv, vset):
        _send(srv, [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}])
        manifest = json.loads(vset.get_file("openusdconnect.json").read().decode("utf-8"))
        assert manifest["openusdconnect"]["live"] is True
        assert manifest["openusdconnect"]["snapshot_seq"] == 1
        kinds = {entry["kind"] for entry in manifest["files"]}
        assert {"flattened_snapshot", "composition_root", "base_layer", "edit_layer"} <= kinds

    def test_layers_collection_exports_live_layers(self, srv, vset):
        _send(srv, [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}])
        members = vset.get_member_names("_layers")
        assert "base.usda" in members
        assert "server-edits.usda" in members
        layer = Sdf.Layer.CreateAnonymous(".usda")
        assert layer.ImportFromString(
            vset.get_file("_layers/server-edits.usda").read().decode("utf-8")
        )
        assert layer.GetPrimAtPath("/World")

    def test_composition_root_contains_metadata_and_sublayers(self, srv, vset):
        _send(srv, [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}])
        layer = Sdf.Layer.CreateAnonymous(".usda")
        assert layer.ImportFromString(vset.get_file("scene.live.usda").read().decode("utf-8"))
        meta = layer.customLayerData["openusdconnect"]
        assert meta["composition_preserving"] is True
        assert meta["flattened_fallback"] == "http://127.0.0.1:7280/usd/scene.usd"
        assert "_layers/server-edits.usda" in list(layer.subLayerPaths)


class TestWriteForbid:
    def test_default_write_is_forbidden(self, vfile):
        before = vfile.read()
        with pytest.raises(PermissionError):
            vfile.write(b'#usda 1.0\ndef Xform "Sneaky" {}\n')
        assert vfile.read() == before

    def test_default_sink_is_forbidden(self, vfile):
        with pytest.raises(PermissionError):
            vfile.open_write_sink()


class TestWriteDrop:
    def test_write_is_dropped(self, srv, drop_vfile):
        before = drop_vfile.read()
        count = srv.get_event_count()
        drop_vfile.write(b'#usda 1.0\ndef Xform "Sneaky" {}\n')
        assert srv.get_event_count() == count
        assert drop_vfile.read() == before

    def test_sink_discards(self, srv, drop_vfile):
        sink = drop_vfile.open_write_sink()
        data = _stage_bytes([("/ValidatedDrop", "Xform")])
        sink.write(data[:10])
        sink.write(data[10:])
        sink.close()
        assert sink.bytes_written == len(data)
        drop_vfile.finish_write(sink)
        assert srv.get_event_count() == 0

    def test_invalid_write_is_rejected_before_drop(self, srv, drop_vfile):
        before = drop_vfile.read()
        count = srv.get_event_count()

        with pytest.raises(InvalidVfsWriteError):
            drop_vfile.write(b"this is not usd")

        assert srv.get_event_count() == count
        assert drop_vfile.read() == before

    def test_invalid_sink_is_rejected_before_drop(self, srv, drop_vfile):
        sink = drop_vfile.open_write_sink()
        sink.write(b"this is not usd")

        with pytest.raises(InvalidVfsWriteError):
            drop_vfile.finish_write(sink)

        assert srv.get_event_count() == 0

    def test_validation_can_be_bypassed_for_drop_mode(self, srv, drop_vfile_without_validation):
        before = drop_vfile_without_validation.read()
        count = srv.get_event_count()

        drop_vfile_without_validation.write(b"this is not usd")

        assert srv.get_event_count() == count
        assert drop_vfile_without_validation.read() == before

    def test_unknown_format_rejected(self, srv):
        with pytest.raises(ValueError, match="usdc"):
            VirtualStageFile(
                srv,
                name="live.usd",
                advertise_host="127.0.0.1",
                sync_port=7200,
                fmt="usdc",
            )


class TestWriteTranslate:
    def test_write_replaces_live_state_with_uploaded_snapshot(self, srv, translate_vfile):
        _send(
            srv,
            [
                {"k": "ensure_prim", "prim": "/World", "typeName": "Xform"},
                {"k": "ensure_prim", "prim": "/World/Old", "typeName": "Cube"},
            ],
        )
        assert srv.stage.GetPrimAtPath("/World/Old")

        uploaded = _stage_bytes([
            ("/World", "Xform"),
            ("/World/New", "Sphere"),
        ])
        translate_vfile.write(uploaded)

        assert srv.get_event_count() > 0
        assert srv.stage.GetPrimAtPath("/World/New")
        assert not srv.stage.GetPrimAtPath("/World/Old")
        assert not srv.stage.GetPrimAtPath("/Root").IsActive()

        fetched = _open_stage(translate_vfile.read())
        assert fetched.GetPrimAtPath("/World/New")
        assert not fetched.GetPrimAtPath("/World/Old")

    def test_sink_translates_on_commit(self, srv, translate_vfile):
        sink = translate_vfile.open_write_sink()
        data = _stage_bytes([("/Fallback", "Xform")])
        sink.write(data[:10])
        sink.write(data[10:])
        translate_vfile.finish_write(sink)

        assert srv.stage.GetPrimAtPath("/Fallback")
        assert srv.get_event_count() > 0

    def test_invalid_upload_does_not_touch_state(self, srv, translate_vfile):
        before = translate_vfile.read()
        count = srv.get_event_count()

        with pytest.raises(InvalidVfsWriteError):
            translate_vfile.write(b"this is not usd")

        assert srv.get_event_count() == count
        assert translate_vfile.read() == before

    def test_invalid_upload_can_be_bypassed_and_dropped(
        self, srv, translate_vfile_without_validation
    ):
        before = translate_vfile_without_validation.read()
        count = srv.get_event_count()

        translate_vfile_without_validation.write(b"this is not usd")

        assert srv.get_event_count() == count
        assert translate_vfile_without_validation.read() == before
