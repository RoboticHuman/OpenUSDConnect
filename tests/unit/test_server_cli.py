"""Unit tests for server CLI helper behavior."""

import pytest

import openusdconnect.server.cli as server_cli
from openusdconnect.server.cli import (
    ServerConfig,
    VfsConfig,
    _create_resolver_context,
    _default_advertise_host,
    _host_for_url,
    _normalize_vfs_share,
    _validate_vfs_name,
)
from tests.openusd_pin import OPENUSD_VERSION, OPENUSD_VERSION_PARTS


def test_default_advertise_host_for_wildcard_binds():
    assert _default_advertise_host("0.0.0.0") == "127.0.0.1"
    assert _default_advertise_host("::") == "127.0.0.1"
    assert _default_advertise_host("") == "127.0.0.1"
    assert _default_advertise_host("10.0.0.5") == "10.0.0.5"


def test_host_for_url_wraps_ipv6():
    assert _host_for_url("127.0.0.1") == "127.0.0.1"
    assert _host_for_url("::1") == "[::1]"
    assert _host_for_url("[::1]") == "[::1]"


def test_normalize_vfs_share_accepts_single_segment():
    assert _normalize_vfs_share("/usd/") == "usd"


@pytest.mark.parametrize("share", ["", "/", ".", "..", "usd/live", r"usd\live"])
def test_normalize_vfs_share_rejects_paths(share):
    with pytest.raises(ValueError, match="vfs-share"):
        _normalize_vfs_share(share)


def test_validate_vfs_name_accepts_file_name():
    assert _validate_vfs_name("scene.usd") == "scene.usd"


@pytest.mark.parametrize("name", ["", ".", "..", "usd/scene.usd", r"usd\scene.usd"])
def test_validate_vfs_name_rejects_paths(name):
    with pytest.raises(ValueError, match="vfs-name"):
        _validate_vfs_name(name)


def test_create_resolver_context_combines_primary_and_uri_configuration(monkeypatch):
    class Resolver:
        def CreateContextFromStrings(self, configurations):
            return configurations

    monkeypatch.setattr(server_cli.Ar, "GetResolver", Resolver)

    result = _create_resolver_context(
        ["/show/config.json", "asset:/show/versions.json", r"C:\show\config.json"]
    )

    assert result == [
        ("", "/show/config.json"),
        ("asset", "/show/versions.json"),
        ("", r"C:\show\config.json"),
    ]


def test_create_resolver_context_rejects_empty_configuration():
    with pytest.raises(ValueError, match="non-empty"):
        _create_resolver_context([""])


def test_server_config_disables_vfs_by_default():
    config = ServerConfig()

    assert config.host == "127.0.0.1"
    assert config.port == 7200
    assert config.vfs is None
    assert config.txn_batch_size == 256
    assert config.txn_batch_delay_ms == 0.5
    assert config.preflight_plugins is True


def test_log_usd_runtime_reports_version_and_bindings(monkeypatch):
    calls = []
    monkeypatch.setattr(server_cli.Usd, "GetVersion", lambda: OPENUSD_VERSION_PARTS)
    monkeypatch.setattr(server_cli.pxr, "__file__", "/project/usd/pxr/__init__.py")
    monkeypatch.setattr(server_cli.LOG, "info", lambda *args: calls.append(args))

    server_cli._log_usd_runtime()

    assert calls == [
        (
            "OpenUSD runtime: %s; bindings: %s",
            OPENUSD_VERSION,
            "/project/usd/pxr/__init__.py",
        )
    ]


def test_shared_stage_mode_rejects_managed_outputs():
    with pytest.raises(ValueError, match="VFS"):
        server_cli.run_server(
            ServerConfig(layer_mode="shared_stage", vfs=VfsConfig(port=7280))
        )
    with pytest.raises(ValueError, match="export-diff"):
        server_cli.run_server(
            ServerConfig(layer_mode="shared_stage", export_diff="changes.usda")
        )


def test_plugin_preflight_runs_before_server_construction(monkeypatch):
    events = []

    def preflight(**_kwargs):
        events.append("preflight")
        raise RuntimeError("plugin initialization failed")

    def construct_server(**_kwargs):
        events.append("construct")

    monkeypatch.setattr(server_cli, "prepare_usd_plugin_environment", preflight)
    monkeypatch.setattr(server_cli, "UsdSyncServer", construct_server)

    with pytest.raises(RuntimeError, match="plugin initialization failed"):
        server_cli.run_server(ServerConfig())

    assert events == ["preflight"]


def test_main_maps_vfs_arguments_to_nested_config(monkeypatch):
    captured = []
    monkeypatch.setattr(server_cli, "run_server", captured.append)

    server_cli.main(
        [
            "--host",
            "0.0.0.0",
            "--port",
            "7210",
            "--departments",
            "lighting,layout",
            "--vfs-port",
            "7280",
            "--vfs-host",
            "127.0.0.1",
            "--vfs-name",
            "live.usd",
            "--vfs-write-mode",
            "translate",
            "--vfs-bypass-write-validation",
            "--no-vfs-prewarm",
            "--advertise-host",
            "workstation",
        ]
    )

    assert captured == [
        ServerConfig(
            host="0.0.0.0",
            port=7210,
            department_priority=["lighting", "layout"],
            vfs=VfsConfig(
                port=7280,
                host="127.0.0.1",
                name="live.usd",
                write_mode="translate",
                validate_writes=False,
                prewarm=False,
                advertise_host="workstation",
            ),
        )
    ]


def test_main_accepts_canonical_service_flags(monkeypatch):
    captured = []
    monkeypatch.setattr(server_cli, "run_server", captured.append)

    server_cli.main(["--event-log", "canonical.db", "--dashboard-port", "8080"])

    assert captured[0].log_path == "canonical.db"
    assert captured[0].dashboard_port == 8080


def test_main_maps_plugin_dll_directories(monkeypatch):
    captured = []
    monkeypatch.setattr(server_cli, "run_server", captured.append)

    server_cli.main(
        ["--plugin-dll-dir", r"C:\Renderer\bin", "--plugin-dll-dir", r"D:\Plugin\lib"]
    )

    assert captured[0].plugin_dll_dirs == [r"C:\Renderer\bin", r"D:\Plugin\lib"]


def test_main_reports_plugin_environment_failure_without_traceback(monkeypatch, caplog):
    def fail(_config):
        raise server_cli.PluginEnvironmentError("configure project plugins")

    monkeypatch.setattr(server_cli, "run_server", fail)

    assert server_cli.main([]) == 2
    assert "configure project plugins" in caplog.text


def test_main_maps_group_commit_configuration(monkeypatch):
    captured = []
    monkeypatch.setattr(server_cli, "run_server", captured.append)

    server_cli.main(["--txn-batch-size", "8", "--txn-batch-delay-ms", "0.25"])

    assert captured[0].txn_batch_size == 8
    assert captured[0].txn_batch_delay_ms == 0.25


def test_main_maps_shared_stage_mode(monkeypatch):
    captured = []
    monkeypatch.setattr(server_cli, "run_server", captured.append)

    server_cli.main(["--layer-mode", "shared_stage", "--base", "scene.usda"])

    assert captured[0].layer_mode == "shared_stage"


@pytest.mark.parametrize("args", [["--log", "legacy.db"], ["--dashboard", "8081"]])
def test_main_rejects_removed_service_aliases(args):
    with pytest.raises(SystemExit) as error:
        server_cli.main(args)

    assert error.value.code == 2


@pytest.mark.parametrize(
    "args",
    [
        ["--port", "0"],
        ["--vfs-port", "65536"],
        ["--dashboard-port", "-1"],
        ["--max-connections", "0"],
        ["--txn-batch-size", "0"],
        ["--txn-batch-delay-ms", "-0.1"],
        ["--compact-interval", "-1"],
    ],
)
def test_main_rejects_invalid_numeric_configuration(args):
    with pytest.raises(SystemExit) as error:
        server_cli.main(args)

    assert error.value.code == 2
