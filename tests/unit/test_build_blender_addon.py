"""Tests for the Blender addon packager."""

import zipfile

from scripts import build_blender_addon


def test_build_vendors_native_client(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    blender = root / "integrations" / "blender"
    blender.mkdir(parents=True)
    (blender / "__init__.py").write_text("", encoding="utf-8")
    (root / "integrations" / "openpbr_to_standard_surface.py").write_text("", encoding="utf-8")
    core = root / "openusdconnect"
    core.mkdir()
    (core / "__init__.py").write_text("", encoding="utf-8")
    native_client = tmp_path / "_native_client.pyd"
    native_client.write_bytes(b"native-client")
    dist = tmp_path / "dist"

    monkeypatch.setattr(build_blender_addon, "REPO_ROOT", root)
    monkeypatch.setattr(build_blender_addon, "DIST_DIR", dist)
    monkeypatch.setattr(build_blender_addon, "_validate_version", lambda: None)
    monkeypatch.setattr(build_blender_addon, "_native_client_path", lambda: native_client)

    build_blender_addon.build()

    with zipfile.ZipFile(dist / build_blender_addon.ZIP_NAME) as addon:
        assert "usd_connect/openusdconnect/_native_client.pyd" in addon.namelist()
        assert addon.read("usd_connect/openusdconnect/_native_client.pyd") == b"native-client"
    assert not (root / "build" / "usd_connect").exists()


def test_native_client_path_reports_missing_build(tmp_path, monkeypatch):
    monkeypatch.setattr(build_blender_addon.sys, "path", [str(tmp_path)])

    try:
        build_blender_addon._native_client_path()
    except RuntimeError as error:
        assert "Run `uv sync`" in str(error)
    else:
        raise AssertionError("missing native client extension should fail the addon build")
