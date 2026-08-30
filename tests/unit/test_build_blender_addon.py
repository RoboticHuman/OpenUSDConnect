"""Tests for the Blender addon packager."""

import zipfile
from types import SimpleNamespace

import pytest

from scripts import build_blender_addon


def test_package_addon_vendors_exact_native_client(tmp_path):
    target = build_blender_addon.HostPython(
        executable=tmp_path / "python.exe",
        version=(3, 11, 13),
        cache_tag="cpython-311",
        extension_suffix=".cp311-win_amd64.pyd",
        platform_tag="windows-x64",
    )
    native_client = tmp_path / "_native_client.cp311-win_amd64.pyd"
    native_client.write_bytes(b"native-client")

    archive_path = build_blender_addon.package_addon(
        target,
        native_client,
        output_dir=tmp_path,
        compatibility_alias=False,
    )

    with zipfile.ZipFile(archive_path) as addon:
        native_path = "usd_connect/openusdconnect/_native_client.cp311-win_amd64.pyd"
        assert [name for name in addon.namelist() if "_native_client" in name] == [native_path]
        assert addon.read(native_path) == b"native-client"


def test_resolve_target_requires_host_abi_configuration(monkeypatch):
    monkeypatch.setattr(build_blender_addon, "configured_blender", lambda: None)
    args = SimpleNamespace(
        blender=None,
        python_executable=None,
        python_sdk=None,
        python_version=None,
    )

    with pytest.raises(RuntimeError, match="Pass --blender, --python-version"):
        build_blender_addon._resolve_target(args)
