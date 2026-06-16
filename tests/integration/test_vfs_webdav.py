"""Integration tests for the VFS WebDAV frontend — a dummy HTTP/WebDAV client.

Starts the real WebDAV server (wsgidav + cheroot) on a free port and
exercises it the way the Windows WebClient redirector would: OPTIONS,
PROPFIND, GET/HEAD, PUT, LOCK/UNLOCK. Never depends on the WebClient
service itself, so it runs on any platform/CI.
"""

import http.client
import json
import socket
import threading
import time

import pytest
from pxr import Sdf, Usd

pytest.importorskip("wsgidav")

from openusdconnect.codec import message_to_dict  # noqa: E402
from openusdconnect.receiver import ReceiverThread  # noqa: E402
from openusdconnect.sender import EventSender  # noqa: E402
from openusdconnect.server import UsdSyncServer  # noqa: E402
from openusdconnect.server.connection import ConnectionHandler, ThreadedTCPServer  # noqa: E402
from openusdconnect.server.vfs import VirtualStageFile, VirtualStageFileSet, WriteMode  # noqa: E402
from openusdconnect.server.vfs.webdav import run_vfs_server  # noqa: E402

SHARE = "usd"
FILE_NAME = "live.usd"


class DavClient:
    """Minimal WebDAV client on http.client — the 'dummy test client'."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port

    def _request(self, method: str, path: str, body=None, headers=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, dict(resp.getheaders()), data
        finally:
            conn.close()

    def options(self, path="/"):
        return self._request("OPTIONS", path)

    def propfind(self, path, depth=1):
        return self._request("PROPFIND", path, headers={"Depth": str(depth)})

    def get(self, path):
        return self._request("GET", path)

    def head(self, path):
        return self._request("HEAD", path)

    def put(self, path, body: bytes):
        return self._request("PUT", path, body=body, headers={"Content-Length": str(len(body))})

    def lock(self, path):
        body = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b"<D:lockinfo xmlns:D='DAV:'>"
            b"<D:lockscope><D:exclusive/></D:lockscope>"
            b"<D:locktype><D:write/></D:locktype>"
            b"<D:owner>test</D:owner>"
            b"</D:lockinfo>"
        )
        return self._request(
            "LOCK",
            path,
            body=body,
            headers={"Timeout": "Second-60", "Content-Type": "application/xml"},
        )

    def unlock(self, path, token: str):
        return self._request("UNLOCK", path, headers={"Lock-Token": f"<{token}>"})


@pytest.fixture
def vfs(tmp_path, free_port):
    """Live UsdSyncServer + WebDAV frontend on a free port."""
    srv = UsdSyncServer(log_path=str(tmp_path / "test.db"))
    provider = VirtualStageFileSet(
        srv,
        flat_name=FILE_NAME,
        advertise_host="127.0.0.1",
        sync_port=7200,
        share=SHARE,
        vfs_base_url=f"http://127.0.0.1:{free_port}/{SHARE}",
    )
    handle = run_vfs_server(provider, "127.0.0.1", free_port, share=SHARE)
    client = DavClient("127.0.0.1", free_port)
    try:
        yield srv, client
    finally:
        handle.stop()
        srv.store.close()


@pytest.fixture
def vfs_drop(tmp_path, free_port):
    srv = UsdSyncServer(log_path=str(tmp_path / "test-drop.db"))
    provider = VirtualStageFileSet(
        srv,
        flat_name=FILE_NAME,
        advertise_host="127.0.0.1",
        sync_port=7200,
        share=SHARE,
        vfs_base_url=f"http://127.0.0.1:{free_port}/{SHARE}",
        write_mode=WriteMode.DROP,
    )
    handle = run_vfs_server(provider, "127.0.0.1", free_port, share=SHARE)
    client = DavClient("127.0.0.1", free_port)
    try:
        yield srv, client
    finally:
        handle.stop()
        srv.store.close()


@pytest.fixture
def vfs_drop_without_validation(tmp_path, free_port):
    srv = UsdSyncServer(log_path=str(tmp_path / "test-drop-bypass.db"))
    provider = VirtualStageFileSet(
        srv,
        flat_name=FILE_NAME,
        advertise_host="127.0.0.1",
        sync_port=7200,
        share=SHARE,
        vfs_base_url=f"http://127.0.0.1:{free_port}/{SHARE}",
        write_mode=WriteMode.DROP,
        validate_writes=False,
    )
    handle = run_vfs_server(provider, "127.0.0.1", free_port, share=SHARE)
    client = DavClient("127.0.0.1", free_port)
    try:
        yield srv, client
    finally:
        handle.stop()
        srv.store.close()


@pytest.fixture
def vfs_translate(tmp_path, free_port):
    srv = UsdSyncServer(log_path=str(tmp_path / "test-translate.db"))
    provider = VirtualStageFileSet(
        srv,
        flat_name=FILE_NAME,
        advertise_host="127.0.0.1",
        sync_port=7200,
        share=SHARE,
        vfs_base_url=f"http://127.0.0.1:{free_port}/{SHARE}",
        write_mode=WriteMode.TRANSLATE,
    )
    handle = run_vfs_server(provider, "127.0.0.1", free_port, share=SHARE)
    client = DavClient("127.0.0.1", free_port)
    try:
        yield srv, client
    finally:
        handle.stop()
        srv.store.close()


@pytest.fixture
def vfs_translate_without_validation(tmp_path, free_port):
    srv = UsdSyncServer(log_path=str(tmp_path / "test-translate-bypass.db"))
    provider = VirtualStageFileSet(
        srv,
        flat_name=FILE_NAME,
        advertise_host="127.0.0.1",
        sync_port=7200,
        share=SHARE,
        vfs_base_url=f"http://127.0.0.1:{free_port}/{SHARE}",
        write_mode=WriteMode.TRANSLATE,
        validate_writes=False,
    )
    handle = run_vfs_server(provider, "127.0.0.1", free_port, share=SHARE)
    client = DavClient("127.0.0.1", free_port)
    try:
        yield srv, client
    finally:
        handle.stop()
        srv.store.close()


def _file_path():
    return f"/{SHARE}/{FILE_NAME}"


def _send(srv, events):
    srv.process_txn(events, client_id="test-client", origin="test-origin")


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


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    return predicate()


class TestDavCompliance:
    def test_options_advertises_class2(self, vfs):
        _, client = vfs
        status, headers, _ = client.options(_file_path())
        assert status == 200
        dav = {h.strip() for h in headers.get("DAV", "").split(",")}
        assert "1" in dav and "2" in dav
        assert headers.get("MS-Author-Via") == "DAV"

    def test_propfind_lists_file(self, vfs):
        _, client = vfs
        status, _, body = client.propfind(f"/{SHARE}/", depth=1)
        assert status == 207
        text = body.decode("utf-8")
        assert FILE_NAME in text
        assert "live.live.usda" in text
        assert "openusdconnect.json" in text
        assert "_layers" in text
        # advertised size must match what GET serves
        get_status, _, data = client.get(_file_path())
        assert get_status == 200
        assert f">{len(data)}</" in text  # getcontentlength element

    def test_propfind_lists_layer_directory(self, vfs):
        _, client = vfs
        status, _, body = client.propfind(f"/{SHARE}/_layers/", depth=1)
        assert status == 207
        text = body.decode("utf-8")
        assert "base.usda" in text
        assert "server-edits.usda" in text

    def test_lock_unlock_roundtrip(self, vfs):
        _, client = vfs
        status, headers, _ = client.lock(_file_path())
        assert status == 200
        token = headers.get("Lock-Token", "").strip("<>")
        assert token
        status, _, _ = client.unlock(_file_path(), token)
        assert status == 204

    def test_unknown_path_404(self, vfs):
        _, client = vfs
        status, _, _ = client.get(f"/{SHARE}/nonexistent.usd")
        assert status == 404


class TestRead:
    def test_get_parses_as_stock_usd_file(self, vfs, tmp_path):
        srv, client = vfs
        _send(srv, [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}])
        status, headers, data = client.get(_file_path())
        assert status == 200
        assert headers.get("ETag")

        # The fetched bytes, saved under a .usd name, open in stock USD.
        path = tmp_path / "fetched.usd"
        path.write_bytes(data)
        stage = Usd.Stage.Open(str(path))
        assert stage
        assert stage.GetPrimAtPath("/World")
        meta = stage.GetRootLayer().customLayerData["openusdconnect"]
        assert meta["live"] is True
        assert meta["host"] == "127.0.0.1"
        assert meta["port"] == 7200
        assert meta["snapshot_seq"] == 1

    def test_content_and_etag_track_changes(self, vfs):
        srv, client = vfs
        _, h1, d1 = client.get(_file_path())
        _send(srv, [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}])
        _, h2, d2 = client.get(_file_path())
        assert d1 != d2
        assert h1.get("ETag") != h2.get("ETag")

    def test_head_matches_get(self, vfs):
        _, client = vfs
        h_status, h_headers, h_body = client.head(_file_path())
        g_status, _, g_body = client.get(_file_path())
        assert h_status == g_status == 200
        assert h_body == b""
        assert int(h_headers["Content-Length"]) == len(g_body)

    def test_no_cache_header(self, vfs):
        _, client = vfs
        _, headers, _ = client.get(_file_path())
        assert headers.get("Cache-Control") == "no-cache"

    def test_manifest_json(self, vfs):
        srv, client = vfs
        _send(srv, [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}])
        status, headers, data = client.get(f"/{SHARE}/openusdconnect.json")
        assert status == 200
        assert headers["Content-Type"].startswith("application/json")
        manifest = json.loads(data.decode("utf-8"))
        assert manifest["openusdconnect"]["snapshot_seq"] == 1
        assert manifest["write_validation"] is True
        assert any(entry["kind"] == "composition_root" for entry in manifest["files"])

    def test_composition_root_and_layer_file(self, vfs):
        srv, client = vfs
        _send(srv, [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}])
        status, _, data = client.get(f"/{SHARE}/live.live.usda")
        assert status == 200
        layer = Sdf.Layer.CreateAnonymous(".usda")
        assert layer.ImportFromString(data.decode("utf-8"))
        assert "_layers/server-edits.usda" in list(layer.subLayerPaths)
        assert layer.customLayerData["openusdconnect"]["composition_preserving"] is True

        status, _, data = client.get(f"/{SHARE}/_layers/server-edits.usda")
        assert status == 200
        layer = Sdf.Layer.CreateAnonymous(".usda")
        assert layer.ImportFromString(data.decode("utf-8"))
        assert layer.GetPrimAtPath("/World")

    def test_concurrent_gets_identical(self, vfs):
        srv, client = vfs
        _send(srv, [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}])
        results = [None, None]

        def fetch(i):
            results[i] = client.get(_file_path())

        threads = [threading.Thread(target=fetch, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert all(r is not None for r in results)
        assert results[0][0] == results[1][0] == 200
        assert results[0][2] == results[1][2]


class TestWriteDrop:
    def test_put_forbidden_by_default(self, vfs):
        srv, client = vfs
        before_count = srv.get_event_count()
        _, _, before_data = client.get(_file_path())

        status, _, _ = client.put(_file_path(), b'#usda 1.0\ndef Xform "Sneaky" {}\n')
        assert status == 403

        assert srv.get_event_count() == before_count
        _, _, after_data = client.get(_file_path())
        assert after_data == before_data

    def test_put_accepted_but_dropped_in_drop_mode(self, vfs_drop):
        srv, client = vfs_drop
        before_count = srv.get_event_count()
        _, _, before_data = client.get(_file_path())

        evil = b'#usda 1.0\ndef Xform "Sneaky" {}\n'
        status, _, _ = client.put(_file_path(), evil)
        assert 200 <= status < 300

        assert srv.get_event_count() == before_count
        _, _, after_data = client.get(_file_path())
        assert after_data == before_data

    def test_put_large_body_dropped_in_drop_mode(self, vfs_drop):
        srv, client = vfs_drop
        body = b"#usda 1.0\n" + b"# padding\n" * 100_000  # ~1 MB
        status, _, _ = client.put(_file_path(), body)
        assert 200 <= status < 300
        assert srv.get_event_count() == 0

    def test_put_invalid_usd_rejected_in_drop_mode(self, vfs_drop):
        srv, client = vfs_drop
        before_count = srv.get_event_count()
        _, _, before_data = client.get(_file_path())

        status, _, _ = client.put(_file_path(), b"this is not usd")
        assert status == 409

        assert srv.get_event_count() == before_count
        _, _, after_data = client.get(_file_path())
        assert after_data == before_data

    def test_put_invalid_usd_can_bypass_validation_in_drop_mode(self, vfs_drop_without_validation):
        srv, client = vfs_drop_without_validation
        before_count = srv.get_event_count()
        _, _, before_data = client.get(_file_path())

        status, _, _ = client.put(_file_path(), b"this is not usd")
        assert 200 <= status < 300

        assert srv.get_event_count() == before_count
        _, _, after_data = client.get(_file_path())
        assert after_data == before_data

    def test_put_translates_full_usd_snapshot_in_translate_mode(self, vfs_translate):
        srv, client = vfs_translate
        _send(
            srv,
            [
                {"k": "ensure_prim", "prim": "/World", "typeName": "Xform"},
                {"k": "ensure_prim", "prim": "/World/Old", "typeName": "Cube"},
            ],
        )
        records = []
        srv.add_event_listener(records.append)

        body = _stage_bytes([
            ("/World", "Xform"),
            ("/World/New", "Sphere"),
        ])
        status, _, _ = client.put(_file_path(), body)
        assert 200 <= status < 300

        assert srv.get_event_count() > 0
        assert any(rec.get("event", {}).get("prim") == "/World/New" for rec in records)
        assert srv.stage.GetPrimAtPath("/World/New")
        assert not srv.stage.GetPrimAtPath("/World/Old")
        assert srv.last_vfs_write_analysis["status"] == "translated"
        assert "/World/New" in srv.last_vfs_write_analysis["created_prims"]
        assert "/World/Old" in srv.last_vfs_write_analysis["removed_prims"]
        assert srv.last_vfs_write_analysis["event_counts"]["ensure_prim"] >= 1

        status, _, data = client.get(_file_path())
        assert status == 200
        stage = _open_stage(data)
        assert stage.GetPrimAtPath("/World/New")
        assert not stage.GetPrimAtPath("/World/Old")

    def test_put_rejects_stale_live_snapshot_in_translate_mode(self, vfs_translate):
        srv, client = vfs_translate
        status, _, stale_data = client.get(_file_path())
        assert status == 200

        _send(srv, [{"k": "ensure_prim", "prim": "/World/Fresh", "typeName": "Xform"}])
        status, _, _ = client.put(_file_path(), stale_data)
        assert status == 409

        assert srv.stage.GetPrimAtPath("/World/Fresh")
        assert srv.last_vfs_write_analysis["status"] == "stale_rejected"
        assert (
            srv.last_vfs_write_analysis["uploaded_seq"]
            < srv.last_vfs_write_analysis["current_seq"]
        )

    def test_put_rejects_ambiguous_root_removal_in_translate_mode(self, vfs_translate):
        srv, client = vfs_translate
        _send(
            srv,
            [
                {"k": "ensure_prim", "prim": "/World", "typeName": "Xform"},
                {"k": "ensure_prim", "prim": "/World/A", "typeName": "Xform"},
                {"k": "ensure_prim", "prim": "/World/B", "typeName": "Xform"},
            ],
        )

        body = _stage_bytes([("/Other", "Xform")])
        status, _, _ = client.put(_file_path(), body)
        assert status == 409

        assert srv.stage.GetPrimAtPath("/World")
        assert srv.last_vfs_write_analysis["status"] == "ambiguous_rejected"

    def test_put_invalid_usd_rejected_in_translate_mode(self, vfs_translate):
        srv, client = vfs_translate
        before_count = srv.get_event_count()
        _, _, before_data = client.get(_file_path())

        status, _, _ = client.put(_file_path(), b"this is not usd")
        assert status == 409

        assert srv.get_event_count() == before_count
        _, _, after_data = client.get(_file_path())
        assert after_data == before_data

    def test_put_invalid_usd_can_bypass_validation_in_translate_mode(
        self, vfs_translate_without_validation
    ):
        srv, client = vfs_translate_without_validation
        before_count = srv.get_event_count()
        _, _, before_data = client.get(_file_path())

        status, _, _ = client.put(_file_path(), b"this is not usd")
        assert 200 <= status < 300

        assert srv.get_event_count() == before_count
        _, _, after_data = client.get(_file_path())
        assert after_data == before_data

    def test_delete_forbidden(self, vfs):
        _, client = vfs
        status, _, _ = client._request("DELETE", _file_path())
        assert status == 403

    def test_mkcol_forbidden(self, vfs):
        _, client = vfs
        status, _, _ = client._request("MKCOL", f"/{SHARE}/new-folder")
        assert status == 403

    def test_copy_forbidden(self, vfs):
        _, client = vfs
        status, _, _ = client._request(
            "COPY",
            _file_path(),
            headers={"Destination": f"http://{client.host}:{client.port}/{SHARE}/copy.usd"},
        )
        assert status == 403

    def test_move_forbidden(self, vfs):
        _, client = vfs
        status, _, _ = client._request(
            "MOVE",
            _file_path(),
            headers={"Destination": f"http://{client.host}:{client.port}/{SHARE}/moved.usd"},
        )
        assert status == 403


class TestUsdLayerImport:
    def test_fetched_bytes_import_into_anonymous_layer(self, vfs):
        """No-disk variant: GET bytes import directly into an Sdf layer."""
        srv, client = vfs
        _send(
            srv,
            [
                {"k": "ensure_prim", "prim": "/World", "typeName": "Xform"},
                {"k": "ensure_prim", "prim": "/World/Cube", "typeName": "Cube"},
            ],
        )
        _, _, data = client.get(_file_path())
        layer = Sdf.Layer.CreateAnonymous(".usda")
        assert layer.ImportFromString(data.decode("utf-8"))
        stage = Usd.Stage.Open(layer)
        prim = stage.GetPrimAtPath("/World/Cube")
        assert prim and prim.GetTypeName() == "Cube"


class TestSnapshotReplayContract:
    def test_cli_does_not_start_vfs_when_sync_bind_fails(self, tmp_path):
        from openusdconnect.server.cli import run_server

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as guard:
            guard.bind(("127.0.0.1", 0))
            guard.listen(1)
            sync_port = guard.getsockname()[1]
            vfs_port = _free_port()

            with pytest.raises(OSError):
                run_server(
                    host="127.0.0.1",
                    port=sync_port,
                    log_path=str(tmp_path / "bind-fail.db"),
                    vfs_port=vfs_port,
                    vfs_host="127.0.0.1",
                )

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            assert probe.connect_ex(("127.0.0.1", vfs_port)) != 0

    def test_receiver_from_snapshot_seq_gets_only_post_snapshot_events(self, tmp_path):
        srv = UsdSyncServer(log_path=str(tmp_path / "test.db"))
        sync_port = _free_port()
        provider = VirtualStageFile(
            srv,
            name=FILE_NAME,
            advertise_host="127.0.0.1",
            sync_port=sync_port,
            vfs_url=f"http://127.0.0.1:0/{SHARE}/{FILE_NAME}",
        )
        tcp_server = ThreadedTCPServer(("127.0.0.1", sync_port), ConnectionHandler, srv)
        thread = threading.Thread(target=tcp_server.serve_forever, daemon=True)
        thread.start()

        receiver = None
        sender = None
        try:
            _send(srv, [{"k": "ensure_prim", "prim": "/World", "typeName": "Xform"}])
            stage = _open_stage(provider.read())
            meta = stage.GetRootLayer().customLayerData["openusdconnect"]
            assert meta["snapshot_seq"] == 1

            receiver = ReceiverThread(
                host="127.0.0.1",
                port=sync_port,
                sync_from=meta["snapshot_seq"] + 1,
                reconnect=False,
                client_id="snapshot-receiver",
                origin="snapshot-receiver-origin",
            )
            receiver.start()
            assert _wait_until(lambda: receiver.connected)

            sender = EventSender(
                "127.0.0.1",
                sync_port,
                client_id="snapshot-emitter",
                origin="snapshot-emitter-origin",
            )
            assert sender.connect()
            assert sender.send_events(
                [{"k": "ensure_prim", "prim": "/World/PostSnapshot", "typeName": "Cube"}]
            )

            def _received_events():
                events = []
                for raw in receiver.drain_queue():
                    msg = message_to_dict(raw)
                    if msg.get("type") == "event":
                        events.append(msg["event"])
                return events

            events = []

            def _poll_events():
                events.extend(_received_events())
                return events

            assert _wait_until(_poll_events)
            assert [ev["prim"] for ev in events] == ["/World/PostSnapshot"]
        finally:
            if sender is not None:
                sender.disconnect()
            if receiver is not None:
                receiver.stop()
                receiver.join(timeout=2)
            tcp_server.shutdown()
            tcp_server.server_close()
            srv.shutdown()
            srv.store.close()
