from types import SimpleNamespace

import pytest

from openusdconnect import __version__, vfs_mount


def test_default_unc():
    assert vfs_mount.default_unc("127.0.0.1", 7280, "usd") == r"\\127.0.0.1@7280\usd"
    assert vfs_mount.default_unc("host", 8080, "/usd/") == r"\\host@8080\usd"


def test_default_url_and_davwwwroot():
    assert vfs_mount.default_url("127.0.0.1", 7280, "usd") == "http://127.0.0.1:7280/usd"
    assert vfs_mount.default_davwwwroot_unc("host", 8080, "/usd/") == r"\\host@8080\DavWWWRoot\usd"


def test_candidate_targets_auto_order():
    assert vfs_mount.candidate_targets("host", 9000, "usd") == [
        "http://host:9000/usd",
        r"\\host@9000\DavWWWRoot\usd",
        r"\\host@9000\usd",
    ]


def test_native_backend_selection():
    assert vfs_mount.native_backend("Windows") == "windows"
    assert vfs_mount.native_backend("Darwin") == "macos"
    with pytest.raises(RuntimeError, match="local_vfs_bridge.py"):
        vfs_mount.native_backend("Linux")


def test_version_does_not_require_a_supported_mount_backend(monkeypatch, capsys):
    def fail_backend_detection():
        raise AssertionError("backend detection should not run")

    monkeypatch.setattr(vfs_mount, "native_backend", fail_backend_detection)

    with pytest.raises(SystemExit) as error:
        vfs_mount.main(["--version"])

    assert error.value.code == 0
    assert capsys.readouterr().out.endswith(f" {__version__}\n")


def test_default_macos_mount_point(tmp_path):
    assert vfs_mount.default_macos_mount_point("/usd/", home=tmp_path) == (
        tmp_path / ".openusdconnect" / "mounts" / "usd"
    )


def test_native_parsers_only_show_backend_options():
    windows_help = vfs_mount._build_native_parser("windows").format_help()
    macos_help = vfs_mount._build_native_parser("macos").format_help()

    assert "--drive" in windows_help
    assert "--persistent" in windows_help
    assert "--start-webclient" in windows_help
    assert "--mount-point" not in windows_help
    assert "--read-write" not in windows_help
    assert "--mount-point" in macos_help
    assert "--volume-name" in macos_help
    assert "--read-write" in macos_help
    assert "--drive" not in macos_help
    assert "--persistent" not in macos_help
    assert "webclient" not in macos_help.lower()


def test_native_configs_are_backend_specific(tmp_path):
    windows = vfs_mount._parse_native_config("windows", ["--drive", "p"])
    macos = vfs_mount._parse_native_config(
        "macos",
        ["--mount-point", str(tmp_path), "--read-write"],
    )

    assert isinstance(windows, vfs_mount.WindowsMountConfig)
    assert windows.drive == "P:"
    assert windows.start_webclient is True
    assert isinstance(macos, vfs_mount.MacOSMountConfig)
    assert macos.mount_point == tmp_path
    assert macos.read_only is False


def test_native_parsers_reject_other_backend_options():
    with pytest.raises(SystemExit):
        vfs_mount._parse_native_config("macos", ["--persistent"])
    with pytest.raises(SystemExit):
        vfs_mount._parse_native_config("windows", ["--read-write"])


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


def test_macos_mount_command_defaults_to_read_only(tmp_path):
    assert vfs_mount.macos_mount_command(
        url="http://host:9000/usd",
        mount_point=tmp_path / "usd",
        volume_name="OpenUSDConnect",
        read_only=True,
    ) == [
        "/sbin/mount_webdav",
        "-S",
        "-o",
        "rdonly",
        "-v",
        "OpenUSDConnect",
        "http://host:9000/usd",
        str(tmp_path / "usd"),
    ]


def test_macos_read_write_command_does_not_request_read_only(tmp_path):
    command = vfs_mount.macos_mount_command(
        url="http://host:9000/usd",
        mount_point=tmp_path / "usd",
        volume_name="OpenUSDConnect",
        read_only=False,
    )

    assert "rdonly" not in command
    assert command[-2:] == ["http://host:9000/usd", str(tmp_path / "usd")]


def test_macos_mount_refuses_nonempty_directory(tmp_path):
    mount_point = tmp_path / "usd"
    mount_point.mkdir()
    (mount_point / "user-file").write_text("keep", encoding="utf-8")

    with pytest.raises(RuntimeError, match="non-empty"):
        vfs_mount._prepare_macos_mount_point(mount_point, force=False)

    assert (mount_point / "user-file").read_text(encoding="utf-8") == "keep"


def test_mount_and_unmount_macos_share(tmp_path, monkeypatch):
    calls = []
    mounted = False

    def fake_ismount(_path):
        return mounted

    def fake_run(command):
        nonlocal mounted
        calls.append(command)
        mounted = command[0] == "/sbin/mount_webdav"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vfs_mount.os.path, "ismount", fake_ismount)
    monkeypatch.setattr(vfs_mount, "_run", fake_run)
    mount_point = tmp_path / "usd"

    root, source = vfs_mount.mount_macos_share(
        host="host",
        port=9000,
        share="usd",
        mount_point=mount_point,
        volume_name="Test",
        read_only=True,
        force=False,
    )

    assert root == mount_point
    assert source == "http://host:9000/usd"
    assert calls[0][-2:] == ["http://host:9000/usd", str(mount_point)]
    assert vfs_mount.unmount_macos_share(mount_point=mount_point) == mount_point
    assert calls[1] == ["/sbin/umount", str(mount_point)]


def test_windows_print_only(monkeypatch, capsys):
    monkeypatch.setattr(vfs_mount, "native_backend", lambda: "windows")
    assert (
        vfs_mount.main(
            [
                "--host",
                "host",
                "--port",
                "9000",
                "--share",
                "usd",
                "--drive",
                "P:",
                "--print-only",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "HTTP share: http://host:9000/usd" in out
    assert r"\\host@9000\usd" in out
    assert r"\\host@9000\DavWWWRoot\usd" in out
    assert "Preferred map command: net use P: http://host:9000/usd /persistent:no" in out
    assert r"P:\scene.usd" in out


def test_macos_print_only_defaults_to_read_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(vfs_mount, "native_backend", lambda: "macos")

    assert (
        vfs_mount.main(
            [
                "--host",
                "host",
                "--port",
                "9000",
                "--mount-point",
                str(tmp_path / "usd"),
                "--print-only",
            ]
        )
        == 0
    )

    out = capsys.readouterr().out
    assert "/sbin/mount_webdav -S -o rdonly" in out
    assert f"Live USD file: {tmp_path / 'usd' / 'scene.usd'}" in out
    assert "Access: read-only" in out
