import pytest

from openusdconnect import vfs_mount


def test_default_unc():
    assert vfs_mount.default_unc("127.0.0.1", 7280, "usd") == r"\\127.0.0.1@7280\usd"
    assert vfs_mount.default_unc("host", 8080, "/usd/") == r"\\host@8080\usd"


def test_default_url_and_davwwwroot():
    assert vfs_mount.default_url("127.0.0.1", 7280, "usd") == "http://127.0.0.1:7280/usd"
    assert (
        vfs_mount.default_davwwwroot_unc("host", 8080, "/usd/")
        == r"\\host@8080\DavWWWRoot\usd"
    )


def test_candidate_targets_auto_order():
    assert vfs_mount.candidate_targets("host", 9000, "usd") == [
        "http://host:9000/usd",
        r"\\host@9000\DavWWWRoot\usd",
        r"\\host@9000\usd",
    ]


def test_normalize_drive():
    assert vfs_mount.normalize_drive("o") == "O:"
    assert vfs_mount.normalize_drive("O:") == "O:"
    with pytest.raises(ValueError):
        vfs_mount.normalize_drive("not-a-drive")


def _http_connection_with_close_error(error):
    class Response:
        status = 200

        @staticmethod
        def read(_size):
            return b""

    class Connection:
        def __init__(self, _host, _port, timeout):
            assert timeout == 5

        @staticmethod
        def request(_method, _path):
            pass

        @staticmethod
        def getresponse():
            return Response()

        @staticmethod
        def close():
            raise error

    return Connection


def test_http_preflight_ignores_socket_close_error(monkeypatch):
    monkeypatch.setattr(
        vfs_mount.http.client,
        "HTTPConnection",
        _http_connection_with_close_error(OSError("socket already closed")),
    )

    ok, _message = vfs_mount.check_http_endpoint("host", 9000, "usd", "scene.usd")

    assert ok


def test_http_preflight_does_not_hide_unexpected_close_error(monkeypatch):
    monkeypatch.setattr(
        vfs_mount.http.client,
        "HTTPConnection",
        _http_connection_with_close_error(RuntimeError("unexpected close failure")),
    )

    with pytest.raises(RuntimeError, match="unexpected close failure"):
        vfs_mount.check_http_endpoint("host", 9000, "usd", "scene.usd")


def test_print_only(capsys):
    assert (
        vfs_mount.main([
            "--host",
            "host",
            "--port",
            "9000",
            "--share",
            "usd",
            "--drive",
            "P:",
            "--print-only",
        ])
        == 0
    )
    out = capsys.readouterr().out
    assert "HTTP share: http://host:9000/usd" in out
    assert r"\\host@9000\usd" in out
    assert r"\\host@9000\DavWWWRoot\usd" in out
    assert "Preferred map command: net use P: http://host:9000/usd /persistent:no" in out
    assert r"P:\scene.usd" in out
