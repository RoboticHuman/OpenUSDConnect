"""Tests for public USD plugin process setup."""

import os

import pytest

import openusdconnect
import openusdconnect.plugin_environment as plugin_environment


def test_prepare_combines_explicit_and_environment_dll_dirs_and_preflights_sdr(
    monkeypatch, tmp_path
):
    explicit_dir = tmp_path / "explicit"
    environment_dir = tmp_path / "environment"
    missing_dir = tmp_path / "missing"
    explicit_dir.mkdir()
    environment_dir.mkdir()
    monkeypatch.setenv(
        plugin_environment.PLUGIN_DLL_DIRS_ENV,
        os.pathsep.join((str(environment_dir), str(missing_dir))),
    )
    prepended = []

    def prepend(dirs):
        prepended.extend(dirs)
        return [path for path in dirs if os.path.isdir(path)]

    requested_shader_ids = []

    class Registry:
        def GetShaderNodeByIdentifier(self, shader_id):
            requested_shader_ids.append(shader_id)
            return None if shader_id == "MissingSurface" else object()

    ticks = iter((10.0, 10.025))
    monkeypatch.setattr(plugin_environment, "prepend_dll_dirs", prepend)
    monkeypatch.setattr(plugin_environment.time, "perf_counter", lambda: next(ticks))

    result = plugin_environment.prepare_usd_plugin_environment(
        dll_dirs=(str(explicit_dir),),
        shader_ids=("UsdPreviewSurface", "MissingSurface", "UsdPreviewSurface", ""),
        registry_factory=Registry,
    )

    assert prepended == [str(explicit_dir), str(environment_dir), str(missing_dir)]
    assert result.added_dll_dirs == (str(explicit_dir), str(environment_dir))
    assert result.missing_dll_dirs == (str(missing_dir),)
    assert result.shader_ids == ("UsdPreviewSurface", "MissingSurface")
    assert result.unresolved_shader_ids == ("MissingSurface",)
    assert result.elapsed_ms == pytest.approx(25.0)
    assert requested_shader_ids == ["UsdPreviewSurface", "MissingSurface"]


def test_prepare_is_exported_from_public_package():
    assert (
        openusdconnect.prepare_usd_plugin_environment
        is plugin_environment.prepare_usd_plugin_environment
    )


def test_prepare_wraps_registry_load_failure_with_configuration_guidance(monkeypatch):
    monkeypatch.delenv(plugin_environment.PLUGIN_DLL_DIRS_ENV, raising=False)

    def fail_registry():
        raise RuntimeError("renderer DLL missing")

    with pytest.raises(
        plugin_environment.PluginEnvironmentError,
        match="--plugin-dll-dir or OPENUSDCONNECT_DLL_DIRS",
    ) as error:
        plugin_environment.prepare_usd_plugin_environment(
            registry_factory=fail_registry
        )

    assert isinstance(error.value.__cause__, RuntimeError)
