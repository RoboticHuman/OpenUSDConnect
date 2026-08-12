"""Tests for the Blender live-file discovery helper.

The helper (integrations/blender/live_discovery.py) is bpy-free, so it runs
outside Blender. These tests exercise it against the real VFS server.
"""

import importlib.util
import os
from pathlib import Path

import pytest
from pxr import Sdf, Usd

pytest.importorskip("wsgidav")

from openusdconnect.server import UsdSyncServer  # noqa: E402
from openusdconnect.server.vfs import VirtualStageFile, VirtualStageFileSet  # noqa: E402
from openusdconnect.server.vfs.webdav import run_vfs_server  # noqa: E402

# Load live_discovery by file path: it is intentionally bpy-free, but the
# integrations.blender package __init__ eagerly imports bpy-dependent modules.
_LD_PATH = Path(__file__).parents[2] / "integrations" / "blender" / "live_discovery.py"
_spec = importlib.util.spec_from_file_location("ouc_live_discovery", _LD_PATH)
live_discovery = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(live_discovery)


@pytest.fixture
def vfs(tmp_path, free_port):
    srv = UsdSyncServer(log_path=str(tmp_path / "test.db"))
    provider = VirtualStageFile(
        srv,
        name="live.usd",
        advertise_host="127.0.0.1",
        sync_port=7200,
        department="layout",
        vfs_url=f"http://127.0.0.1:{free_port}/usd/live.usd",
    )
    handle = run_vfs_server(provider, "127.0.0.1", free_port, share="usd")
    try:
        yield srv, free_port
    finally:
        handle.stop()
        srv.store.close()


@pytest.fixture
def vfs_tree(tmp_path, free_port):
    srv = UsdSyncServer(log_path=str(tmp_path / "tree.db"))
    provider = VirtualStageFileSet(
        srv,
        flat_name="scene.usd",
        advertise_host="127.0.0.1",
        sync_port=7200,
        share="usd",
        vfs_base_url=f"http://127.0.0.1:{free_port}/usd",
    )
    handle = run_vfs_server(provider, "127.0.0.1", free_port, share="usd")
    try:
        yield srv, free_port
    finally:
        handle.stop()
        srv.store.close()


def _send(srv, events):
    srv._commit_events(events, client_id="test-client", origin="test-origin")


def test_is_remote():
    assert live_discovery.is_remote("http://127.0.0.1:7280/usd/live.usd")
    assert live_discovery.is_remote("https://example.test/usd/live.usd")
    assert not live_discovery.is_remote(r"C:\scenes\live.usd")


class TestResolveRemote:
    def test_http_url_fetches_and_reads_metadata(self, vfs):
        srv, port = vfs
        _send(srv, [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}])

        url = f"http://127.0.0.1:{port}/usd/live.usd"
        local, meta = live_discovery.resolve_import_source(url)
        try:
            assert os.path.isfile(local)
            stage = Usd.Stage.Open(local)
            assert stage.GetPrimAtPath("/World")

            assert meta is not None
            assert meta["live"] is True
            assert meta["host"] == "127.0.0.1"
            assert meta["port"] == 7200
            assert meta["scene_id"] == srv.scene_id
            assert meta["vfs_url"] == f"http://127.0.0.1:{port}/usd/live.usd"
            assert meta["department"] == "layout"
            assert meta["requires_token"] is False
            assert meta["snapshot_seq"] >= 1
        finally:
            os.remove(local)

    def test_remote_cache_reuses_etag_path_and_prunes_old(self, vfs, tmp_path, monkeypatch):
        srv, port = vfs
        monkeypatch.setattr(live_discovery, "_CACHE_DIR", str(tmp_path / "live-cache"))
        url = f"http://127.0.0.1:{port}/usd/live.usd"

        _send(srv, [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}])
        first = live_discovery.fetch_to_temp(url)
        second = live_discovery.fetch_to_temp(url)
        assert second == first
        assert os.path.exists(first)

        _send(srv, [{"k": "ensure_prim", "prim": "/World/Cube", "typeName": "Cube"}])
        third = live_discovery.fetch_to_temp(url)
        assert third != first
        assert os.path.exists(third)
        assert not os.path.exists(first)

    def test_remote_composition_root_falls_back_to_flattened_snapshot(
        self,
        vfs_tree,
        tmp_path,
        monkeypatch,
    ):
        srv, port = vfs_tree
        monkeypatch.setattr(live_discovery, "_CACHE_DIR", str(tmp_path / "live-cache"))
        _send(srv, [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}])

        url = f"http://127.0.0.1:{port}/usd/scene.live.usda"
        local, meta = live_discovery.resolve_import_source(url)

        assert local.endswith(".usd")
        assert meta is not None
        assert meta["composition_preserving"] is False
        assert meta["snapshot_seq"] == 1
        stage = Usd.Stage.Open(local)
        assert stage.GetPrimAtPath("/World")


class TestStaticFile:
    def test_plain_usd_has_no_metadata(self, tmp_path):
        path = tmp_path / "plain.usda"
        layer = Sdf.Layer.CreateNew(str(path))
        Sdf.PrimSpec(layer, "World", Sdf.SpecifierDef, "Xform")
        layer.Save()

        local, meta = live_discovery.resolve_import_source(str(path))
        assert local == str(path)
        assert meta is None

    def test_missing_file_returns_none(self, tmp_path):
        assert live_discovery.read_live_metadata(str(tmp_path / "nope.usda")) is None
