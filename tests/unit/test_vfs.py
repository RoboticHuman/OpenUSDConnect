"""Tests for the VFS provider (VirtualStageFile) — in-process, no HTTP.

Exercises snapshot generation, embedded connection metadata, cache/etag
invalidation, write policy, and the browsable multi-file VFS directory.
"""

import io
import json
import threading

import pytest
from pxr import Sdf, Usd, UsdLux

from openusdconnect.codec import message_to_dict
from openusdconnect.framing import recv_framed_rfile
from openusdconnect.protocol_constants import PROTOCOL_VERSION
from openusdconnect.server import UsdSyncServer
from openusdconnect.server.types import (
    InvalidVfsWriteError,
    StaleVfsWriteError,
    UnsupportedVfsWriteError,
)
from openusdconnect.server.vfs import VirtualStageFile, VirtualStageFileSet, WriteMode
from openusdconnect.server.vfs.provider import VfsSnapshot, VfsStat
from openusdconnect.server.vfs.webdav import _StageFileResource


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
    srv._commit_events(events, client_id="test-client", origin="test-origin")


def _send_to_layer(srv, layer, events, client_id="test-client"):
    """Apply events to an explicit edit layer and persist them."""
    srv._commit_events(events, client_id=client_id, origin="test-origin", layer=layer)


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


def _stage_bytes_with_custom_properties() -> bytes:
    layer = Sdf.Layer.CreateAnonymous(".usda")
    stage = Usd.Stage.Open(layer)
    prim = stage.DefinePrim("/World", "Xform")
    prim.CreateAttribute("customFoo", Sdf.ValueTypeNames.String, custom=True).Set("bar")
    prim.CreateRelationship("customRel", custom=True).AddTarget(Sdf.Path("/World"))
    return layer.ExportToString().encode("utf-8")


def _stage_bytes_with_generic_spec_fields() -> bytes:
    layer = Sdf.Layer.CreateAnonymous(".usda")
    stage = Usd.Stage.Open(layer)
    prim = stage.DefinePrim("/Before", "Xform")
    prim.SetDocumentation("preserved documentation")
    prim.SetCustomData({"department": "layout"})
    stage.SetDefaultPrim(prim)
    layer.customLayerData = {"pipeline": "test"}
    class_spec = Sdf.CreatePrimInLayer(layer, "/_Class")
    class_spec.specifier = Sdf.SpecifierClass
    class_spec.typeName = "Scope"
    typed_over = Sdf.CreatePrimInLayer(layer, "/Before/TypedOver")
    typed_over.specifier = Sdf.SpecifierOver
    typed_over.typeName = "Scope"
    return layer.ExportToString().encode("utf-8")


def _stage_bytes_with_local_variant_definition() -> bytes:
    layer = Sdf.Layer.CreateAnonymous(".usda")
    stage = Usd.Stage.Open(layer)
    prim = stage.DefinePrim("/World", "Xform")
    variants = prim.GetVariantSets().AddVariantSet("look")
    for name in ("red", "blue"):
        variants.AddVariant(name)
        variants.SetVariantSelection(name)
        with variants.GetVariantEditContext():
            stage.DefinePrim("/World/Child", "Scope").SetDocumentation(name)
    variants.SetVariantSelection("red")
    return layer.ExportToString().encode("utf-8")


def _stage_bytes_with_sublayer() -> bytes:
    layer = Sdf.Layer.CreateAnonymous(".usda")
    layer.subLayerPaths = ["asset.usda"]
    return layer.ExportToString().encode("utf-8")


def _stage_bytes_with_supported_api_schema() -> bytes:
    layer = Sdf.Layer.CreateAnonymous(".usda")
    stage = Usd.Stage.Open(layer)
    light = UsdLux.SphereLight.Define(stage, "/World/Light")
    UsdLux.ShapingAPI.Apply(light.GetPrim())
    return layer.ExportToString().encode("utf-8")


def _stage_bytes_with_live_metadata(metadata) -> bytes:
    layer = Sdf.Layer.CreateAnonymous(".usda")
    stage = Usd.Stage.Open(layer)
    stage.DefinePrim("/World", "Xform")
    layer.customLayerData = {"openusdconnect": metadata}
    return layer.ExportToString().encode("utf-8")


def _with_current_live_metadata(srv, data: bytes) -> bytes:
    layer = Sdf.Layer.CreateAnonymous(".usda")
    assert layer.ImportFromString(data.decode("utf-8"))
    epoch, snapshot_seq = srv.get_snapshot_token()
    custom_data = dict(layer.customLayerData)
    custom_data["openusdconnect"] = {
        "scene_id": srv.scene_id,
        "epoch": epoch,
        "snapshot_seq": snapshot_seq,
    }
    layer.customLayerData = custom_data
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
        sink.write(b"x" * 1024)
        sink.write(b"y" * 1024)
        sink.close()
        assert sink.bytes_written == 2048
        drop_vfile.finish_write(sink)
        assert srv.get_event_count() == 0

    def test_invalid_write_is_dropped(self, srv, drop_vfile):
        before = drop_vfile.read()
        count = srv.get_event_count()

        drop_vfile.write(b"this is not usd")

        assert srv.get_event_count() == count
        assert drop_vfile.read() == before

    def test_invalid_sink_is_dropped(self, srv, drop_vfile):
        sink = drop_vfile.open_write_sink()
        sink.write(b"this is not usd")
        drop_vfile.finish_write(sink)

        assert srv.get_event_count() == 0

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

        uploaded = _with_current_live_metadata(
            srv,
            _stage_bytes(
                [
                    ("/World", "Xform"),
                    ("/World/New", "Sphere"),
                ]
            ),
        )
        translate_vfile.write(uploaded)

        assert srv.get_event_count() > 0
        assert srv.stage.GetPrimAtPath("/World/New")
        assert not srv.stage.GetPrimAtPath("/World/Old")
        assert not srv.stage.GetPrimAtPath("/Root").IsActive()

        fetched = _open_stage(translate_vfile.read())
        assert fetched.GetPrimAtPath("/World/New")
        assert not fetched.GetPrimAtPath("/World/Old")

    def test_write_broadcasts_one_complete_replacement_boundary(
        self,
        srv,
        translate_vfile,
    ):
        class CaptureRequest:
            def __init__(self):
                self.payloads = []

            def sendall(self, payload):
                self.payloads.append(payload)

        class CaptureReceiver:
            def __init__(self):
                self.request = CaptureRequest()
                self.client_address = ("vfs-capture", 1)
                self.send_lock = threading.Lock()
                self._layered_replay = False

        receiver = CaptureReceiver()
        srv.receivers.add(receiver)
        uploaded = _open_stage(translate_vfile.read())
        uploaded.DefinePrim("/Root/Replacement", "Sphere")

        translate_vfile.write(uploaded.GetRootLayer().ExportToString().encode("utf-8"))
        srv._broadcast_queue.join()

        assert len(receiver.request.payloads) == 1
        stream = io.BytesIO(receiver.request.payloads[0])
        messages = []
        while stream.tell() < len(stream.getvalue()):
            messages.append(message_to_dict(recv_framed_rfile(stream)))

        epoch, head_seq = srv.get_snapshot_token()
        assert messages[0]["type"] == "resync"
        assert all(message["type"] == "event" for message in messages[1:-1])
        assert messages[-1] == {
            "type": "replay_complete",
            "head_seq": head_seq,
            "epoch": epoch,
        }
        assert head_seq == len(messages) - 2

    def test_sink_translates_on_commit(self, srv, translate_vfile):
        sink = translate_vfile.open_write_sink()
        data = _with_current_live_metadata(
            srv,
            _stage_bytes([("/Fallback", "Xform")]),
        )
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

    @pytest.mark.parametrize(
        ("metadata", "message"),
        [
            ("invalid", "must be a dictionary"),
            ({"snapshot_seq": 0}, "'epoch'"),
            ({"epoch": "0", "snapshot_seq": 0}, "'epoch'"),
            ({"epoch": 0, "snapshot_seq": False}, "'snapshot_seq'"),
            ({"epoch": -1, "snapshot_seq": 0}, "'epoch'"),
        ],
    )
    def test_invalid_live_metadata_does_not_touch_state(
        self, srv, translate_vfile, metadata, message
    ):
        _send(srv, [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}])
        before = translate_vfile.read()
        count = srv.get_event_count()

        with pytest.raises(InvalidVfsWriteError, match=message):
            if isinstance(metadata, dict):
                metadata = {"scene_id": srv.scene_id, **metadata}
            translate_vfile.write(_stage_bytes_with_live_metadata(metadata))

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

    def test_missing_live_metadata_is_rejected(self, srv, translate_vfile):
        before = translate_vfile.read()

        with pytest.raises(InvalidVfsWriteError, match="missing openusdconnect metadata"):
            translate_vfile.write(_stage_bytes([("/World", "Xform")]))

        assert translate_vfile.read() == before

    def test_wrong_scene_id_is_rejected(self, srv, translate_vfile):
        epoch, snapshot_seq = srv.get_snapshot_token()
        data = _stage_bytes_with_live_metadata(
            {
                "scene_id": "another-scene",
                "epoch": epoch,
                "snapshot_seq": snapshot_seq,
            }
        )

        with pytest.raises(InvalidVfsWriteError, match="scene_id does not match"):
            translate_vfile.write(data)

        assert srv.last_vfs_write_analysis["status"] == "metadata_rejected"

    def test_future_snapshot_token_is_rejected(self, srv, translate_vfile):
        epoch, snapshot_seq = srv.get_snapshot_token()
        data = _stage_bytes_with_live_metadata(
            {
                "scene_id": srv.scene_id,
                "epoch": epoch + 1,
                "snapshot_seq": snapshot_seq,
            }
        )

        with pytest.raises(StaleVfsWriteError, match="token does not match"):
            translate_vfile.write(data)

        assert srv.last_vfs_write_analysis["status"] == "future_rejected"

    def test_stale_rejection_can_be_explicitly_disabled(self, srv, translate_vfile):
        stale_stage = _open_stage(translate_vfile.read())
        _send(srv, [{"k": "ensure_prim", "prim": "/Root/Fresh", "typeName": "Xform"}])

        event_count = srv.replace_from_stage_snapshot(stale_stage, reject_stale=False)

        assert event_count > 0
        assert not srv.stage.GetPrimAtPath("/Root/Fresh")
        assert "stale snapshot token accepted" in srv.last_vfs_write_analysis["notes"][0]

    def test_persistence_failure_leaves_authoritative_state_intact(
        self, srv, translate_vfile, monkeypatch
    ):
        _send(srv, [{"k": "ensure_prim", "prim": "/Before", "typeName": "Cube"}])
        before_bytes = translate_vfile.read()
        before_rows = srv.store.get_all_asc()
        before_token = srv.get_snapshot_token()

        uploaded_stage = _open_stage(before_bytes)
        uploaded_stage.DefinePrim("/After", "Sphere")
        uploaded = uploaded_stage.GetRootLayer().ExportToString().encode("utf-8")

        def fail_replacement(_records):
            raise RuntimeError("injected event-store failure")

        monkeypatch.setattr(srv.store, "clear_and_rewrite", fail_replacement)

        with pytest.raises(RuntimeError, match="injected event-store failure"):
            translate_vfile.write(uploaded)

        assert srv.store.get_all_asc() == before_rows
        assert srv.get_snapshot_token() == before_token
        assert srv.stage.GetPrimAtPath("/Before")
        assert not srv.stage.GetPrimAtPath("/After")
        assert translate_vfile.read() == before_bytes

    def test_translated_replacement_replays_after_restart(self, tmp_path):
        db_path = str(tmp_path / "restart.db")
        server = UsdSyncServer(log_path=db_path)
        provider_file = VirtualStageFile(
            server,
            name="live.usd",
            advertise_host="127.0.0.1",
            sync_port=7200,
            write_mode=WriteMode.TRANSLATE,
        )
        try:
            uploaded_stage = _open_stage(provider_file.read())
            uploaded_stage.DefinePrim("/Root/AfterRestart", "Sphere")

            event_count = server.replace_from_stage_snapshot(uploaded_stage)
            assert event_count > 0
            assert server.store.get_count() == event_count
        finally:
            server.shutdown()
            server.store.close()

        restored = UsdSyncServer(log_path=db_path)
        try:
            assert restored.get_event_count() == event_count
            assert restored.stage.GetPrimAtPath("/Root/AfterRestart")
        finally:
            restored.shutdown()
            restored.store.close()

    def test_custom_properties_are_translated(self, srv, translate_vfile):
        _send(srv, [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}])
        count = srv.get_event_count()

        translate_vfile.write(
            _with_current_live_metadata(srv, _stage_bytes_with_custom_properties())
        )

        assert srv.get_event_count() > count
        assert srv.last_vfs_write_analysis["status"] == "translated"
        assert srv.stage.GetAttributeAtPath("/World.customFoo").Get() == "bar"
        assert srv.stage.GetRelationshipAtPath("/World.customRel").GetTargets() == [
            Sdf.Path("/World")
        ]

        fetched = _open_stage(translate_vfile.read())
        assert fetched.GetAttributeAtPath("/World.customFoo").Get() == "bar"
        assert fetched.GetRelationshipAtPath("/World.customRel").GetTargets() == [
            Sdf.Path("/World")
        ]

    def test_generic_prim_and_layer_fields_are_translated(self, srv, translate_vfile):
        _send(srv, [{"k": "ensure_prim", "prim": "/Before", "typeName": "Xform"}])
        count = srv.get_event_count()

        translate_vfile.write(
            _with_current_live_metadata(srv, _stage_bytes_with_generic_spec_fields())
        )

        assert srv.get_event_count() > count
        assert srv.last_vfs_write_analysis["status"] == "translated"
        assert srv.edit_layer.defaultPrim == "Before"
        assert srv.edit_layer.customLayerData["pipeline"] == "test"
        before = srv.edit_layer.GetPrimAtPath("/Before")
        assert before.documentation == "preserved documentation"
        assert dict(before.customData) == {"department": "layout"}
        assert srv.edit_layer.GetPrimAtPath("/_Class").specifier == Sdf.SpecifierClass
        typed_over = srv.edit_layer.GetPrimAtPath("/Before/TypedOver")
        assert typed_over.specifier == Sdf.SpecifierOver
        assert typed_over.typeName == "Scope"

    def test_local_variant_definitions_are_translated(self, srv, translate_vfile):
        translate_vfile.write(
            _with_current_live_metadata(srv, _stage_bytes_with_local_variant_definition())
        )

        assert srv.last_vfs_write_analysis["status"] == "translated"
        assert srv.edit_layer.GetPrimAtPath("/World{look=red}Child").documentation == "red"
        assert srv.edit_layer.GetPrimAtPath("/World{look=blue}Child").documentation == "blue"
        assert srv.stage.GetPrimAtPath("/World/Child").GetDocumentation() == "red"

    def test_sublayer_topology_is_rejected(self, srv, translate_vfile):
        count = srv.get_event_count()

        with pytest.raises(UnsupportedVfsWriteError, match="sublayer topology"):
            translate_vfile.write(
                _with_current_live_metadata(srv, _stage_bytes_with_sublayer())
            )

        assert srv.get_event_count() == count
        assert srv.last_vfs_write_analysis["status"] == "unsupported_rejected"
        assert "sublayer topology" in srv.last_vfs_write_analysis["notes"][0]

    def test_supported_api_schema_is_translated(self, srv, translate_vfile):
        translate_vfile.write(
            _with_current_live_metadata(srv, _stage_bytes_with_supported_api_schema())
        )

        light = srv.stage.GetPrimAtPath("/World/Light")
        assert light.HasAPI(UsdLux.ShapingAPI)

    def test_department_layers_disable_translate(self, dept_srv):
        layer = dept_srv.get_or_create_client_layer("alice", "layout")
        _send_to_layer(
            dept_srv,
            layer,
            [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}],
            client_id="alice",
        )
        translate_vfile = VirtualStageFile(
            dept_srv,
            name="live.usd",
            advertise_host="127.0.0.1",
            sync_port=7200,
            write_mode=WriteMode.TRANSLATE,
        )
        count = dept_srv.get_event_count()

        with pytest.raises(UnsupportedVfsWriteError):
            translate_vfile.write(_stage_bytes([("/World", "Xform")]))

        assert dept_srv.get_event_count() == count
        assert dept_srv.last_vfs_write_analysis["status"] == "unsupported_rejected"
        assert "department layers" in dept_srv.last_vfs_write_analysis["notes"][0]

    def test_non_default_collaboration_layers_disable_translate(
        self, srv, translate_vfile
    ):
        with srv.stage_lock:
            srv.layer_stack.ensure_layer("review")

        with pytest.raises(UnsupportedVfsWriteError, match="non-default collaboration"):
            translate_vfile.write(translate_vfile.read())

        assert "collaboration layers: review" in srv.last_vfs_write_analysis["notes"][0]

class TestDavResourceLifecycle:
    def test_read_metadata_and_content_use_one_pinned_snapshot(self):
        class ChangingFile:
            name = "scene.usd"

            def __init__(self):
                self.calls = 0
                self.current = VfsSnapshot(
                    data=b"old",
                    stat=VfsStat(size=3, mtime=10.0, etag='"1-2"'),
                )

            def snapshot(self):
                self.calls += 1
                return self.current

        provider_file = ChangingFile()
        resource = object.__new__(_StageFileResource)
        resource._file = provider_file
        resource._snapshot = None

        assert resource.get_content_length() == 3
        provider_file.current = VfsSnapshot(
            data=b"new-content",
            stat=VfsStat(size=11, mtime=20.0, etag='"1-3"'),
        )

        assert resource.get_content().read() == b"old"
        assert resource.get_etag() == "1-2"
        assert resource.get_last_modified() == 10.0
        assert provider_file.calls == 1

    def test_aborted_translate_write_disposes_spooled_sink(self, srv, translate_vfile):
        sink = translate_vfile.open_write_sink()
        sink.write(b"partial upload")
        resource = object.__new__(_StageFileResource)
        resource._file = translate_vfile
        resource._sink = sink
        resource._snapshot = None

        resource.end_write(with_errors=True)

        assert sink._file.closed
        assert srv.get_event_count() == 0
