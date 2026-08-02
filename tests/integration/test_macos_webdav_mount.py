"""Opt-in smoke test for the native macOS WebDAV filesystem client."""

from __future__ import annotations

import os
import platform

import pytest
from pxr import Usd

pytest.importorskip("wsgidav")

from openusdconnect import vfs_mount  # noqa: E402
from openusdconnect.server import UsdSyncServer  # noqa: E402
from openusdconnect.server.vfs import VirtualStageFile  # noqa: E402
from openusdconnect.server.vfs.webdav import run_vfs_server  # noqa: E402


@pytest.mark.skipif(platform.system() != "Darwin", reason="native mount is macOS-only")
@pytest.mark.skipif(
    os.environ.get("OUC_RUN_MACOS_WEBDAV_SMOKE") != "1",
    reason="set OUC_RUN_MACOS_WEBDAV_SMOKE=1 to run the native macOS mount smoke",
)
def test_macos_native_mount_reads_virtual_usd(tmp_path, free_port):
    server = UsdSyncServer(log_path=str(tmp_path / "test.db"))
    provider = VirtualStageFile(
        server,
        name="scene.usd",
        advertise_host="127.0.0.1",
        sync_port=7200,
        vfs_url=f"http://127.0.0.1:{free_port}/usd/scene.usd",
    )
    handle = run_vfs_server(provider, "127.0.0.1", free_port, share="usd")
    mount_point = tmp_path / "mount"
    try:
        server.process_txn(
            [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}],
            client_id="macos-mount-smoke",
            origin="macos-mount-smoke",
        )
        vfs_mount.mount_macos_share(
            host="127.0.0.1",
            port=free_port,
            share="usd",
            mount_point=mount_point,
            volume_name="OpenUSDConnectTest",
            read_only=True,
            force=False,
        )

        stage = Usd.Stage.Open(str(mount_point / "scene.usd"))

        assert stage is not None
        assert stage.GetPrimAtPath("/World")
        assert stage.GetRootLayer().customLayerData["openusdconnect"]["live"] is True
    finally:
        if os.path.ismount(mount_point):
            vfs_mount.unmount_macos_share(mount_point=mount_point)
        handle.stop()
        server.store.close()
