r"""Opt-in Windows WebClient smoke for the WebDAV UNC live-open path.

The normal WebDAV tests use a deterministic HTTP client and run everywhere.
This file validates the actual Windows redirector path:

    \\127.0.0.1@PORT\usd\scene.usd

It is intentionally opt-in because many CI and developer machines do not have
the WebClient service enabled or allowed to access custom localhost ports.
Set ``OUC_RUN_UNC_SMOKE=1`` on a configured Windows machine to make failures
hard failures during release validation.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pxr import Sdf, Usd

pytest.importorskip("wsgidav")

from openusdconnect.server import UsdSyncServer  # noqa: E402
from openusdconnect.server.vfs import VirtualStageFile  # noqa: E402
from openusdconnect.server.vfs.webdav import run_vfs_server  # noqa: E402


@pytest.mark.skipif(os.name != "nt", reason="Windows UNC WebDAV smoke is Windows-only")
@pytest.mark.skipif(
    os.environ.get("OUC_RUN_UNC_SMOKE") != "1",
    reason="set OUC_RUN_UNC_SMOKE=1 to run the real Windows WebClient smoke",
)
def test_windows_unc_path_reads_virtual_usd(tmp_path, free_port):
    srv = UsdSyncServer(log_path=str(tmp_path / "test.db"))
    provider = VirtualStageFile(
        srv,
        name="scene.usd",
        advertise_host="127.0.0.1",
        sync_port=7200,
        vfs_url=f"http://127.0.0.1:{free_port}/usd/scene.usd",
    )
    handle = run_vfs_server(provider, "127.0.0.1", free_port, share="usd")
    try:
        srv.process_txn(
            [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}],
            client_id="unc-smoke",
            origin="unc-smoke",
        )
        unc_path = f"\\\\127.0.0.1@{free_port}\\usd\\scene.usd"
        data = Path(unc_path).read_bytes()
        layer = Sdf.Layer.CreateAnonymous(".usda")
        assert layer.ImportFromString(data.decode("utf-8"))
        stage = Usd.Stage.Open(layer)
        assert stage.GetPrimAtPath("/World")
        meta = stage.GetRootLayer().customLayerData["openusdconnect"]
        assert meta["vfs_url"] == f"http://127.0.0.1:{free_port}/usd/scene.usd"
    finally:
        handle.stop()
        srv.store.close()
