"""Runtime packaging contracts using synthetic installs, never a native USD build."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import package_usd_runtime as packaging

RUNTIME_ENV_KEYS = (
    "PYTHONPATH",
    "PYTHONHOME",
    "OPENUSDCONNECT_USD_ROOT",
    "OPENUSDCONNECT_BUNDLED_USD",
    "PXR_PLUGINPATH_NAME",
    "PXR_MTLX_STDLIB_SEARCH_PATHS",
    "PXR_MTLX_PLUGIN_SEARCH_PATHS",
)


def _write(root: Path, relative: str, content: str = "synthetic runtime resource\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _manifest(output: Path) -> dict:
    return json.loads((output / "usd-runtime.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def commands(monkeypatch):
    pin = SimpleNamespace(version="0.42.7", commit="a" * 40)
    real_run = packaging._run
    run = Mock(side_effect=AssertionError("Unexpected subprocess in a synthetic unit test"))
    managed = Mock(side_effect=AssertionError("A unit test must not build OpenUSD"))
    monkeypatch.setattr(packaging.build_openusd, "load_pin", lambda: pin)
    monkeypatch.setattr(packaging, "_run", run)
    monkeypatch.setattr(packaging, "managed_install", managed)
    return SimpleNamespace(pin=pin, run=run, managed=managed, real_run=real_run)


@pytest.fixture
def runtime_install(tmp_path, commands):
    root = tmp_path / "source runtime"
    version = tuple(map(int, commands.pin.version.split(".")))
    # This stub checks Python bootstrap plumbing, not native plugin loading or ABI.
    _write(
        root,
        "lib/python/pxr/__init__.py",
        "from types import SimpleNamespace\n"
        f"Usd = SimpleNamespace(GetVersion=lambda: {version})\n"
        "_plugins = {name: SimpleNamespace(name=name, Load=lambda: True)\n"
        "    for name in ['synthetic', 'external-synthetic']}\n"
        "Plug = SimpleNamespace(Registry=lambda: SimpleNamespace(\n"
        "    GetAllPlugins=lambda: list(_plugins.values()),\n"
        "    GetPluginWithName=_plugins.get))\n"
        "Sdf = SimpleNamespace(FileFormat=SimpleNamespace(\n"
        "    FindAllFileFormatExtensions=lambda: ['usda', 'mtlx', 'usd'],\n"
        "    FindByExtension=lambda ext: object() if ext == 'mtlx' else None))\n",
    )
    for relative in ("bin/usd_mock.dll", "lib/libusd_mock.so", "lib64/libusd_mock.so"):
        _write(root, relative)
    _write(root, "lib/usd/plugInfo.json", json.dumps({"Includes": ["mock/resources/"]}))
    _write(
        root,
        "lib/usd/mock/resources/plugInfo.json",
        json.dumps(
            {
                "Plugins": [
                    {
                        "Name": "synthetic",
                        "Type": "library",
                        "Root": "../../../..",
                        "LibraryPath": "lib/libusd_mock.so",
                        "ResourcePath": "lib/usd/mock/resources",
                    }
                ]
            }
        ),
    )
    _write(root, "lib/usd/mock/resources/shaders/include/nested/common.glsl")
    _write(root, "libraries/stdlib/genglsl/include/mx_math.glsl")
    _write(root, "LICENSE", "synthetic license\n")
    _write(
        root,
        "lib/cmake/MaterialX/MaterialXConfigVersion.cmake",
        'set(PACKAGE_VERSION "1.39.4")\n',
    )
    checkout = tmp_path / "synthetic checkout"
    dependencies = tmp_path / "synthetic dependencies"
    _write(checkout, "LICENSE.txt", "USD mock license\n")
    _write(checkout, "NOTICE.txt", "USD mock notice\n")
    _write(checkout, "README.md", "developer documentation\n")
    _write(dependencies, "MaterialX/LICENSE", "MaterialX mock license\n")
    _write(
        root,
        "openusdconnect-build.json",
        json.dumps({"paths": {"checkout": str(checkout), "dependency_sources": str(dependencies)}}),
    )
    return root


@pytest.fixture
def staging(tmp_path, commands, runtime_install):
    output = tmp_path / "staged package"
    info = {
        "version": commands.pin.version,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "architecture": "synthetic-arch",
        "file_formats": ["mtlx", "usd", "usda"],
        "materialx": True,
        "plugins": ["synthetic"],
        "pxr_path": str(output / "openusd/lib/python/pxr/__init__.py"),
    }
    commands.run.side_effect = lambda *_args, **_kwargs: "probe diagnostic\n" + json.dumps(info)
    commands.managed.side_effect = None
    commands.managed.return_value = runtime_install
    return SimpleNamespace(
        output=output, python=Path(sys.executable), source=runtime_install, info=info
    )


@pytest.mark.parametrize("profile", ["", "minimal", "FULL"])
def test_validate_options_rejects_unknown_profile(profile):
    with pytest.raises(ValueError, match="Unknown OpenUSD profile"):
        packaging.validate_options(profile, None, [], False)


@pytest.mark.parametrize("profile", ["core", "full"])
def test_validate_options_accepts_managed_profiles(profile):
    packaging.validate_options(profile, None, [], False)


@pytest.mark.parametrize("allow_unpinned", [False, True])
def test_validate_options_accepts_existing_external_install(tmp_path, allow_unpinned):
    root = tmp_path / "usd"
    plugin = tmp_path / "plugin"
    root.mkdir()
    plugin.mkdir()

    packaging.validate_options("external", root, [plugin], allow_unpinned)


@pytest.mark.parametrize("kind", ["unspecified", "missing", "file"])
def test_external_requires_an_existing_root_directory(tmp_path, kind, commands):
    root = None if kind == "unspecified" else tmp_path / "usd"
    if kind == "file":
        root.touch()
    output = tmp_path / "output"

    with pytest.raises(ValueError, match="external requires an existing --usd-root"):
        packaging.prepare_runtime(output, Path(sys.executable), profile="external", usd_root=root)

    assert not output.exists()
    commands.run.assert_not_called()
    commands.managed.assert_not_called()


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_external_rejects_invalid_plugin_directories(tmp_path, kind, commands):
    root = tmp_path / "usd"
    valid_plugin = tmp_path / "valid-plugin"
    root.mkdir()
    valid_plugin.mkdir()
    invalid_plugin = tmp_path / "invalid-plugin"
    if kind == "file":
        invalid_plugin.touch()

    with pytest.raises(ValueError, match="USD plugin directory does not exist"):
        packaging.prepare_runtime(
            tmp_path / "output",
            Path(sys.executable),
            profile="external",
            usd_root=root,
            plugin_paths=[valid_plugin, invalid_plugin],
        )

    assert not (tmp_path / "output").exists()
    commands.run.assert_not_called()


@pytest.mark.parametrize("profile", ["core", "full"])
@pytest.mark.parametrize("option", ["usd_root", "plugin_paths", "allow_unpinned"])
def test_custom_options_require_external_profile(tmp_path, profile, option, commands):
    options = {"usd_root": tmp_path, "plugin_paths": [tmp_path], "allow_unpinned": True}

    with pytest.raises(ValueError, match="require --usd-profile external"):
        packaging.prepare_runtime(
            tmp_path / "output", Path(sys.executable), profile=profile, **{option: options[option]}
        )

    assert not (tmp_path / "output").exists()
    commands.run.assert_not_called()
    commands.managed.assert_not_called()


@pytest.mark.parametrize("profile", ["external", "full"])
def test_prepare_rejects_install_without_python_bindings(tmp_path, commands, profile):
    root = tmp_path / "empty-install"
    root.mkdir()
    commands.managed.side_effect = None
    commands.managed.return_value = root

    with pytest.raises(RuntimeError, match="No OpenUSD Python bindings"):
        packaging.prepare_runtime(
            tmp_path / "output",
            Path(sys.executable),
            profile=profile,
            usd_root=root if profile == "external" else None,
        )

    commands.run.assert_not_called()


@pytest.mark.parametrize("profile", ["core", "full", "external"])
def test_prepare_rejects_pin_mismatch(staging, profile):
    staging.info["version"] = "0.42.6"

    with pytest.raises(RuntimeError, match="does not match pinned"):
        packaging.prepare_runtime(
            staging.output,
            staging.python,
            profile=profile,
            usd_root=staging.source if profile == "external" else None,
        )


def test_external_can_explicitly_allow_unpinned_version(staging, commands):
    staging.info["version"] = "0.42.6"
    staging.info["materialx"] = False
    staging.info["file_formats"] = ["usd", "usda"]

    info = packaging.prepare_runtime(
        staging.output,
        staging.python,
        profile="external",
        usd_root=staging.source,
        allow_unpinned=True,
    )

    assert info["version"] == "0.42.6"
    assert info["pinned"] is False
    assert info["source_commit"] is None
    assert info["materialx"] is False
    assert _manifest(staging.output)["capabilities"] == info
    commands.managed.assert_not_called()


@pytest.mark.parametrize("allow_unpinned", [False, True])
@pytest.mark.parametrize("location", ["source", "sibling", "other-staged-directory"])
def test_staged_probe_must_load_pxr_from_staged_openusd(staging, allow_unpinned, location):
    paths = {
        "source": staging.source / "lib/python/pxr/__init__.py",
        "sibling": staging.output / "openusd-other/lib/python/pxr/__init__.py",
        "other-staged-directory": staging.output / "plugins/0/pxr/__init__.py",
    }
    staging.info["pxr_path"] = str(paths[location])

    with pytest.raises(RuntimeError, match="outside the staged package"):
        packaging.prepare_runtime(
            staging.output,
            staging.python,
            profile="external",
            usd_root=staging.source,
            allow_unpinned=allow_unpinned,
        )


def test_full_requires_materialx_file_format(staging):
    staging.info["materialx"] = False
    staging.info["file_formats"] = ["usd", "usda"]

    with pytest.raises(RuntimeError, match="missing the MaterialX file-format plugin"):
        packaging.prepare_runtime(staging.output, staging.python, profile="full")


def test_full_records_pin_and_probes_staged_launcher(staging, commands):
    info = packaging.prepare_runtime(staging.output, staging.python, profile="full")

    commands.managed.assert_called_once_with(staging.python)
    commands.run.assert_called_once_with(
        [str(staging.python), "-I", str(staging.output / "_launch.py"), "--runtime-info"],
        capture=True,
    )
    assert (staging.output / "_launch.py").read_bytes() == packaging.LAUNCHER.read_bytes()
    assert info["pinned"] is True
    assert info["source_commit"] == commands.pin.commit
    assert info["materialx_version"] == "1.39.4"
    assert "pxr_path" not in info
    assert _manifest(staging.output)["capabilities"] == info
    licenses = staging.output / "openusd/licenses"
    assert (licenses / "OpenUSD/LICENSE.txt").read_text() == "USD mock license\n"
    assert (licenses / "OpenUSD/NOTICE.txt").read_text() == "USD mock notice\n"
    assert (licenses / "MaterialX/LICENSE").read_text() == "MaterialX mock license\n"
    assert not (licenses / "OpenUSD/README.md").exists()
    assert not (staging.output / "openusd/openusdconnect-build.json").exists()


def test_external_accepts_minimal_headless_install_without_plugin_resources(staging):
    source = staging.output.parent / "minimal headless"
    _write(source, "lib/python/pxr/__init__.py")
    staging.info["materialx"] = False
    staging.info["file_formats"] = ["usd", "usda"]

    info = packaging.prepare_runtime(
        staging.output, staging.python, profile="external", usd_root=source
    )

    config = _manifest(staging.output)
    assert config["python_path"] == "openusd/lib/python"
    assert config["library_dirs"] == ["openusd/lib"]
    assert config["plugin_paths"] == []
    assert config["required_plugins"] == []
    assert info["materialx"] is False
    assert info["materialx_version"] is None


def test_core_does_not_stage_or_configure_a_custom_root(staging, commands, monkeypatch):
    copy = Mock(side_effect=AssertionError("Core must not copy a custom USD install"))
    monkeypatch.setattr(packaging, "_copy_runtime", copy)
    staging.info["materialx"] = False
    staging.info["file_formats"] = ["usd", "usda"]
    staging.info["pxr_path"] = str(staging.output / "python/site-packages/pxr/__init__.py")

    info = packaging.prepare_runtime(staging.output, staging.python, profile="core")

    config = _manifest(staging.output)
    assert config["profile"] == "core"
    assert config["python_path"] is None
    assert config["library_dirs"] == []
    assert config["plugin_paths"] == []
    assert config["required_plugins"] == []
    assert info["materialx"] is False
    assert info["pinned"] is True
    assert info["source_commit"] is None
    assert info["materialx_version"] is None
    assert "pxr_path" not in info
    assert not (staging.output / "openusd").exists()
    commands.managed.assert_not_called()
    copy.assert_not_called()


def test_copy_preserves_nested_shader_includes_but_omits_developer_files(tmp_path, runtime_install):
    omitted = (
        "include/pxr/pxr.h",
        "cmake/FindUSD.cmake",
        "lib/cmake/MaterialX/MaterialXConfigVersion.cmake",
        "lib/pkgconfig/usd.pc",
        "lib64/cmake/USDConfig.cmake",
        "lib64/pkgconfig/usd.pc",
        "lib/libusd.a",
        "lib/usd.lib",
        "bin/usd.pdb",
        "lib/usd.exp",
        "lib/usd.la",
        "lib/python/pxr/cache.pyc",
        "lib/python/pxr/__pycache__/cache.txt",
        "openusdconnect-build.json",
    )
    for relative in omitted:
        _write(runtime_install, relative)
    expected = {
        p.relative_to(runtime_install).as_posix(): p.read_bytes()
        for p in runtime_install.rglob("*")
        if p.is_file() and p.relative_to(runtime_install).as_posix() not in omitted
    }
    destination = tmp_path / "copied"

    packaging._copy_runtime(runtime_install, destination)

    actual = {
        p.relative_to(destination).as_posix(): p.read_bytes()
        for p in destination.rglob("*")
        if p.is_file()
    }
    assert actual == expected
    assert "lib/usd/mock/resources/shaders/include/nested/common.glsl" in actual
    assert "libraries/stdlib/genglsl/include/mx_math.glsl" in actual
    assert not (destination / "include").exists()
    assert not (destination / "lib/cmake").exists()
    assert all((runtime_install / relative).is_file() for relative in omitted)


@pytest.fixture
def plugin_tree(tmp_path):
    root = tmp_path / "external plugins" / "custom plugin"
    _write(root, "lib/mock.so")
    _write(root, "resources/shaders/include/nested/common.glsl")
    plugin = {
        "Name": "external-synthetic",
        "Type": "library",
        "Root": "..",
        "LibraryPath": "lib/mock.so",
        "ResourcePath": "resources",
    }
    info = _write(root, "resources/plugInfo.json", json.dumps({"Plugins": [plugin]}))
    return SimpleNamespace(
        root=root,
        plugin=plugin,
        info=info,
        posix_root=root.as_posix().removeprefix(root.drive),
    )


def test_plugin_validation_accepts_relative_roots_comments_and_resource_only_plugins(plugin_tree):
    top = _write(
        plugin_tree.root,
        "plugInfo.json",
        '  # USD full-line comment\n{"Includes": ["resources/", "resources/*/"]}\n',
    )
    resource_only = {"Name": "resource#plugin", "Type": "resource", "ResourcePath": "."}
    plugin_tree.info.write_text(
        "# plugin manifest\n" + json.dumps({"Plugins": [plugin_tree.plugin, resource_only]}),
        encoding="utf-8",
    )

    assert packaging._validate_plugin_paths(plugin_tree.root) == sorted([top, plugin_tree.info])


@pytest.mark.parametrize(
    "include",
    [
        "registrations/vendor/nested/plugInfo.json",
        "registrations/*/*/",
        "registrations/*/*/plugInfo.json",
        "registrations/**/plugInfo.json",
    ],
)
def test_plugin_validation_follows_recursive_includes_globs_and_cycles(plugin_tree, include):
    top = _write(plugin_tree.root, "plugInfo.json", json.dumps({"Includes": [include]}))
    index = _write(
        plugin_tree.root,
        "registrations/vendor/nested/plugInfo.json",
        json.dumps({"Includes": ["../../../resources/", "../../../plugInfo.json"]}),
    )
    _write(plugin_tree.root, "resources/codegenTemplates/plugInfo.json", "{ @SCHEMA_TEMPLATE@ }")
    _write(plugin_tree.root, "unregistered/plugInfo.json", "{ @UNREGISTERED_TEMPLATE@ }")

    assert packaging._validate_plugin_paths(plugin_tree.root) == sorted(
        [top, index, plugin_tree.info]
    )


@pytest.mark.parametrize("include", ["resources/", "resources/plugInfo.json", "res*/plugInfo.json"])
def test_plugin_validation_checks_libraries_in_explicit_and_glob_includes(plugin_tree, include):
    _write(plugin_tree.root, "plugInfo.json", json.dumps({"Includes": [include]}))
    plugin_tree.plugin["LibraryPath"] = "lib/missing.so"
    plugin_tree.info.write_text(json.dumps({"Plugins": [plugin_tree.plugin]}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="Missing plugin LibraryPath"):
        packaging._validate_plugin_paths(plugin_tree.root)


def test_plugin_validation_rejects_escape_in_recursively_included_file(plugin_tree):
    _write(plugin_tree.root, "plugInfo.json", json.dumps({"Includes": ["resources/"]}))
    plugin_tree.info.write_text(
        json.dumps({"Includes": ["../../outside/*.json"], "Plugins": [plugin_tree.plugin]}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="Non-relocatable plugin include"):
        packaging._validate_plugin_paths(plugin_tree.root)


def test_prepare_preserves_invalid_codegen_templates_without_registering_them(staging, plugin_tree):
    template = "# Not a registered plugin\n{ @SCHEMA_TEMPLATE@ }\n"
    usd_template = "lib/usd/mock/resources/codegenTemplates/plugInfo.json"
    external_template = "resources/codegenTemplates/plugInfo.json"
    _write(staging.source, usd_template, template)
    _write(plugin_tree.root, external_template, template)

    packaging.prepare_runtime(
        staging.output,
        staging.python,
        profile="external",
        usd_root=staging.source,
        plugin_paths=[plugin_tree.root],
    )

    config = _manifest(staging.output)
    assert config["required_plugins"] == ["external-synthetic"]
    assert set(config["plugin_paths"]) == {
        "openusd/lib/usd/plugInfo.json",
        "openusd/lib/usd/mock/resources/plugInfo.json",
        "plugins/0/resources/plugInfo.json",
    }
    assert (staging.output / "openusd" / usd_template).read_text() == template
    assert (staging.output / "plugins/0" / external_template).read_text() == template


@pytest.mark.parametrize(
    "profile,source_kind", [("full", "usd"), ("external", "usd"), ("external", "plugin")]
)
@pytest.mark.parametrize("relation", ["same", "output-under-source", "source-under-output"])
def test_prepare_rejects_overlapping_output_and_sources_before_copying(
    staging, plugin_tree, commands, monkeypatch, profile, source_kind, relation
):
    source = staging.source if source_kind == "usd" else plugin_tree.root
    output = {
        "same": source,
        "output-under-source": source / "package",
        "source-under-output": source.parent,
    }[relation]
    copy = Mock(side_effect=AssertionError("Overlapping sources must be rejected before copying"))
    monkeypatch.setattr(packaging, "_copy_runtime", copy)
    original_files = {p: p.read_bytes() for p in source.rglob("*") if p.is_file()}

    with pytest.raises(ValueError, match="must be separate directories"):
        packaging.prepare_runtime(
            output,
            staging.python,
            profile=profile,
            usd_root=staging.source if profile == "external" else None,
            plugin_paths=[plugin_tree.root] if source_kind == "plugin" else None,
        )

    assert {p: p.read_bytes() for p in source.rglob("*") if p.is_file()} == original_files
    assert not (output / "_launch.py").exists()
    assert not (output / "usd-runtime.json").exists()
    copy.assert_not_called()
    commands.run.assert_not_called()


def test_prepare_allows_sibling_output_sharing_source_name_prefix(staging):
    output = staging.source.with_name(staging.source.name + "-package")
    staging.info["pxr_path"] = str(output / "openusd/lib/python/pxr/__init__.py")

    info = packaging.prepare_runtime(
        output, staging.python, profile="external", usd_root=staging.source
    )

    assert _manifest(output)["capabilities"] == info
    assert (output / "openusd/lib/python/pxr/__init__.py").is_file()


@pytest.mark.parametrize("field", ["LibraryPath", "ResourcePath"])
@pytest.mark.parametrize(
    "kind", ["absolute", "windows-absolute", "posix-absolute", "posix-absolute-inside", "escape"]
)
def test_plugin_validation_rejects_nonrelocatable_paths(plugin_tree, field, kind):
    paths = {
        "absolute": str(plugin_tree.root / plugin_tree.plugin[field]),
        "windows-absolute": "C:/nonrelocatable-usd/plugin.dll",
        "posix-absolute": "/nonrelocatable-usd/plugin.so",
        "posix-absolute-inside": f"{plugin_tree.posix_root}/{plugin_tree.plugin[field]}",
        "escape": "../outside/plugin.so",
    }
    plugin_tree.plugin[field] = paths[kind]
    plugin_tree.info.write_text(json.dumps({"Plugins": [plugin_tree.plugin]}), encoding="utf-8")

    with pytest.raises(RuntimeError, match=f"Non-relocatable {field}"):
        packaging._validate_plugin_paths(plugin_tree.root)


@pytest.mark.parametrize("field", ["LibraryPath", "ResourcePath"])
def test_plugin_validation_rejects_missing_library_or_resources(plugin_tree, field):
    plugin_tree.plugin[field] = "does-not-exist"
    plugin_tree.info.write_text(json.dumps({"Plugins": [plugin_tree.plugin]}), encoding="utf-8")

    with pytest.raises(RuntimeError, match=f"Missing plugin {field}"):
        packaging._validate_plugin_paths(plugin_tree.root)


@pytest.mark.parametrize(
    "kind", ["absolute", "windows-absolute", "posix-absolute", "posix-absolute-inside", "escape"]
)
def test_plugin_validation_rejects_nonrelocatable_includes(plugin_tree, kind):
    includes = {
        "absolute": str(plugin_tree.root / "resources"),
        "windows-absolute": "C:/nonrelocatable-usd/resources/",
        "posix-absolute": "/nonrelocatable-usd/resources/",
        "posix-absolute-inside": f"{plugin_tree.posix_root}/resources",
        "escape": "../outside/resources/",
    }
    _write(plugin_tree.root, "plugInfo.json", json.dumps({"Includes": [includes[kind]]}))

    with pytest.raises(RuntimeError, match="Non-relocatable plugin include"):
        packaging._validate_plugin_paths(plugin_tree.root)


@pytest.mark.parametrize(
    "kind", ["absolute-inside", "posix-absolute-inside", "escape-without-paths"]
)
def test_plugin_validation_rejects_nonrelocatable_root_itself(plugin_tree, kind):
    if kind == "absolute-inside":
        plugin_tree.plugin["Root"] = str(plugin_tree.root)
    elif kind == "posix-absolute-inside":
        plugin_tree.plugin["Root"] = plugin_tree.posix_root
    else:
        plugin_tree.plugin = {"Name": "synthetic", "Type": "resource", "Root": "../../outside"}
    plugin_tree.info.write_text(json.dumps({"Plugins": [plugin_tree.plugin]}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="Non-relocatable"):
        packaging._validate_plugin_paths(plugin_tree.root)


def test_external_manifest_contains_only_relative_runtime_paths(staging, plugin_tree):
    info = packaging.prepare_runtime(
        staging.output,
        staging.python,
        profile="external",
        usd_root=staging.source,
        plugin_paths=[plugin_tree.root],
    )

    config = _manifest(staging.output)
    assert config["schema"] == 1
    assert config["profile"] == "external"
    assert config["python_path"] == "openusd/lib/python"
    assert set(config["library_dirs"]) == {
        "openusd/bin",
        "openusd/lib",
        "openusd/lib64",
        "plugins/0/lib",
    }
    assert set(config["plugin_paths"]) == {
        "openusd/lib/usd/plugInfo.json",
        "openusd/lib/usd/mock/resources/plugInfo.json",
        "plugins/0/resources/plugInfo.json",
    }
    assert config["required_plugins"] == ["external-synthetic"]
    paths = [config["python_path"], *config["library_dirs"], *config["plugin_paths"]]
    for value in paths:
        assert not PurePosixPath(value).is_absolute()
        assert not PureWindowsPath(value).is_absolute()
        assert ".." not in PurePosixPath(value).parts
        assert "\\" not in value
        assert (staging.output / value).exists()
    serialized = json.dumps(config)
    for root in (staging.output, staging.source, plugin_tree.root):
        assert json.dumps(str(root))[1:-1] not in serialized
        assert root.as_posix() not in serialized
    assert "pxr_path" not in info
    assert config["capabilities"] == info
    assert info["source_commit"] is None
    assert (staging.output / "plugins/0/resources/shaders/include/nested/common.glsl").is_file()


def test_external_adds_nested_plugin_library_directory_to_loader_paths(staging, plugin_tree):
    _write(plugin_tree.root, "private/platform/mock.so")
    plugin_tree.plugin["LibraryPath"] = "private/platform/mock.so"
    plugin_tree.info.write_text(json.dumps({"Plugins": [plugin_tree.plugin]}), encoding="utf-8")

    packaging.prepare_runtime(
        staging.output,
        staging.python,
        profile="external",
        usd_root=staging.source,
        plugin_paths=[plugin_tree.root],
    )

    config = _manifest(staging.output)
    assert "plugins/0/private/platform" in config["library_dirs"]
    assert len(config["library_dirs"]) == len(set(config["library_dirs"]))
    assert config["required_plugins"] == ["external-synthetic"]


@pytest.mark.parametrize("profile", ["full", "external"])
def test_synthetic_runtime_can_be_probed_after_relocation(
    staging, commands, monkeypatch, plugin_tree, profile
):
    monkeypatch.setattr(packaging, "_run", commands.real_run)
    for key in RUNTIME_ENV_KEYS:
        monkeypatch.setenv(key, str(staging.source / "must-not-be-used"))

    info = packaging.prepare_runtime(
        staging.output,
        staging.python,
        profile=profile,
        usd_root=staging.source if profile == "external" else None,
        plugin_paths=[plugin_tree.root] if profile == "external" else None,
    )
    relocated = staging.output.parent / "relocated package"
    shutil.copytree(staging.output, relocated)
    text = packaging._run(
        [str(staging.python), "-I", str(relocated / "_launch.py"), "--runtime-info"],
        capture=True,
    )
    probe = json.loads(text.strip().splitlines()[-1])

    assert probe["profile"] == profile
    assert probe["version"] == commands.pin.version
    assert probe["materialx"] is True
    assert probe["file_formats"] == ["mtlx", "usd", "usda"]
    assert probe["validated_plugins"] == (["external-synthetic"] if profile == "external" else [])
    assert Path(probe["pxr_path"]) == (relocated / "openusd/lib/python/pxr/__init__.py").resolve()
    assert _manifest(relocated)["capabilities"] == info


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("_test_server_launcher", packaging.LAUNCHER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_configure_windows_dll_search", Mock())
    root = tmp_path / "launcher package"
    root.mkdir()
    monkeypatch.setattr(module, "__file__", str(root / "_launch.py"))
    # Local proxies prevent activation from changing pytest's imports, DLLs or process.
    monkeypatch.setattr(
        module,
        "os",
        SimpleNamespace(
            name="nt",
            environ={key: "outside-package" for key in RUNTIME_ENV_KEYS},
            pathsep=";",
            add_dll_directory=Mock(side_effect=lambda _directory: object()),
            execve=Mock(side_effect=AssertionError("Unexpected process replacement")),
        ),
    )
    monkeypatch.setattr(
        module,
        "sys",
        SimpleNamespace(
            executable=sys.executable,
            argv=[str(root / "_launch.py")],
            path=["existing-site-packages"],
            platform="win32",
            version_info=sys.version_info,
        ),
    )
    config = {
        "schema": 1,
        "profile": "full",
        "python_path": "openusd/lib/python",
        "library_dirs": ["openusd/bin", "openusd/lib"],
        "plugin_paths": ["openusd/lib/usd/plugInfo.json"],
    }
    for relative in (config["python_path"], *config["library_dirs"]):
        (root / relative).mkdir(parents=True, exist_ok=True)
    _write(root, config["plugin_paths"][0], "{}")
    _write(root, "usd-runtime.json", json.dumps(config))
    return SimpleNamespace(module=module, root=root.resolve(), config=config)


def test_launcher_activates_only_package_configured_paths_and_clears_runtime_environment(launcher):
    module, root = launcher.module, launcher.root

    assert module.activate() == launcher.config

    assert module.sys.path == [str(root / "openusd/lib/python"), "existing-site-packages"]
    assert module.os.environ["OPENUSDCONNECT_USD_ROOT"] == str(root / "openusd")
    assert module.os.environ["PXR_PLUGINPATH_NAME"] == str(root / "openusd/lib/usd/plugInfo.json")
    for key in set(RUNTIME_ENV_KEYS) - {"OPENUSDCONNECT_USD_ROOT", "PXR_PLUGINPATH_NAME"}:
        assert key not in module.os.environ
    assert [call.args[0] for call in module.os.add_dll_directory.call_args_list] == [
        str(root / "openusd/bin"),
        str(root / "openusd/lib"),
    ]
    assert len(module._DLL_HANDLES) == 2
    assert module.os.environ["PXR_USD_WINDOWS_DLL_PATH"] == ";".join(
        str(root / p) for p in launcher.config["library_dirs"]
    )
    module.os.execve.assert_not_called()


def test_launcher_configures_host_dll_dirs_before_windows_application_dispatch(launcher):
    module, root = launcher.module, launcher.root
    env_dir = root.parent / "renderer env"
    arg_dir = root.parent / "renderer arg"
    equals_dir = root.parent / "renderer equals"
    for directory in (env_dir, arg_dir, equals_dir):
        directory.mkdir()
    module.os.environ[module._PLUGIN_DLL_DIRS_ENV] = ";".join(
        [str(env_dir), str(arg_dir), str(env_dir)]
    )
    module.sys.argv += [
        "openusdconnect.server",
        "--plugin-dll-dir",
        str(arg_dir),
        f"--plugin-dll-dir={equals_dir}",
    ]

    assert module.activate() == launcher.config

    expected = [
        *(root / path for path in launcher.config["library_dirs"]),
        env_dir.resolve(),
        arg_dir.resolve(),
        equals_dir.resolve(),
    ]
    assert [call.args[0] for call in module.os.add_dll_directory.call_args_list] == list(
        map(str, expected)
    )
    assert module.os.environ["PXR_USD_WINDOWS_DLL_PATH"] == ";".join(map(str, expected))
    assert module.sys.argv[1:] == [
        "openusdconnect.server",
        "--plugin-dll-dir",
        str(arg_dir),
        f"--plugin-dll-dir={equals_dir}",
    ]


def test_launcher_ignores_missing_host_dll_dirs(launcher):
    missing = launcher.root.parent / "missing renderer"
    launcher.module.os.environ[launcher.module._PLUGIN_DLL_DIRS_ENV] = str(missing)

    assert launcher.module.activate() == launcher.config

    assert [call.args[0] for call in launcher.module.os.add_dll_directory.call_args_list] == [
        str(launcher.root / path) for path in launcher.config["library_dirs"]
    ]


def test_launcher_posix_reexec_includes_host_plugin_library_dirs(launcher):
    class Reexec(Exception):
        pass

    module, root = launcher.module, launcher.root
    arg_dir = root.parent / "renderer arg"
    arg_dir.mkdir()
    module.os.name = "posix"
    module.os.pathsep = ":"
    module.sys.platform = "linux"
    module.sys.argv += [
        "openusdconnect.server",
        "--plugin-dll-dir",
        str(arg_dir),
    ]
    module.os.execve.side_effect = Reexec

    with pytest.raises(Reexec):
        module.activate()

    _, _, env = module.os.execve.call_args.args
    expected = [
        *(root / path for path in launcher.config["library_dirs"]),
        arg_dir.resolve(),
    ]
    assert env["LD_LIBRARY_PATH"] == ":".join(map(str, expected))


def test_launcher_core_clears_custom_root_without_replacing_site_packages(launcher):
    config = {
        "schema": 1,
        "profile": "core",
        "python_path": None,
        "library_dirs": [],
        "plugin_paths": [],
    }
    _write(launcher.root, "usd-runtime.json", json.dumps(config))

    assert launcher.module.activate() == config

    assert launcher.module.sys.path == ["existing-site-packages"]
    assert not set(RUNTIME_ENV_KEYS).intersection(launcher.module.os.environ)
    launcher.module.os.add_dll_directory.assert_not_called()


@pytest.mark.parametrize("schema", [None, 0, 2])
def test_launcher_rejects_unsupported_manifest_schema(launcher, schema):
    launcher.config["schema"] = schema
    _write(launcher.root, "usd-runtime.json", json.dumps(launcher.config))

    with pytest.raises(RuntimeError, match="Unsupported packaged OpenUSD runtime manifest"):
        launcher.module.activate()

    launcher.module.os.add_dll_directory.assert_not_called()
    launcher.module.os.execve.assert_not_called()


@pytest.mark.parametrize("field", ["python_path", "library_dirs", "plugin_paths"])
@pytest.mark.parametrize("kind", ["parent", "absolute", "prefix-sibling"])
def test_launcher_rejects_manifest_paths_outside_package(launcher, field, kind):
    paths = {
        "parent": "../outside",
        "absolute": str(launcher.root.parent / "outside"),
        "prefix-sibling": str(launcher.root.with_name(launcher.root.name + "-other") / "lib"),
    }
    launcher.config[field] = paths[kind] if field == "python_path" else [paths[kind]]
    _write(launcher.root, "usd-runtime.json", json.dumps(launcher.config))

    with pytest.raises(RuntimeError, match="Runtime manifest path escapes the package"):
        launcher.module.activate()


@pytest.mark.parametrize(
    "platform,loader", [("linux", "LD_LIBRARY_PATH"), ("darwin", "DYLD_LIBRARY_PATH")]
)
def test_launcher_posix_reexec_uses_only_staged_libraries(launcher, platform, loader):
    class Reexec(Exception):
        pass

    module, root = launcher.module, launcher.root
    module.os.name = "posix"
    module.os.pathsep = ":"
    module.sys.platform = platform
    module.sys.argv += ["openusdconnect.server", "--port", "9000"]
    module.os.environ[loader] = "outside-libraries"
    module.os.execve.side_effect = Reexec

    with pytest.raises(Reexec):
        module.activate()

    executable, argv, env = module.os.execve.call_args.args
    assert executable == sys.executable
    assert argv == [
        sys.executable,
        "-I",
        module.__file__,
        "openusdconnect.server",
        "--port",
        "9000",
    ]
    assert env[loader] == ":".join(str(root / p) for p in launcher.config["library_dirs"])
    assert env["_OUC_PACKAGE_BOOTSTRAP"] == str(root)
    module.os.add_dll_directory.assert_not_called()
    module.os.environ.update(env)
    module.os.execve.reset_mock()

    assert module.activate() == launcher.config
    module.os.execve.assert_not_called()
    assert module.sys.path[0] == str(root / "openusd/lib/python")


@pytest.fixture
def pxr_probe(launcher, commands, monkeypatch):
    pxr = ModuleType("pxr")
    pxr.__file__ = str(launcher.root / "openusd/lib/python/pxr/__init__.py")
    extensions = ["usda", "usd", "mtlx"]
    find_format = Mock(return_value=object())
    pxr.Usd = SimpleNamespace(GetVersion=lambda: tuple(map(int, commands.pin.version.split("."))))
    pxr.Sdf = SimpleNamespace(
        FileFormat=SimpleNamespace(
            FindAllFileFormatExtensions=lambda: extensions,
            FindByExtension=find_format,
        )
    )
    plugins = {
        name: SimpleNamespace(name=name, Load=Mock(return_value=True))
        for name in ("z-plugin", "a-plugin")
    }
    registry = SimpleNamespace(
        GetAllPlugins=lambda: list(plugins.values()),
        GetPluginWithName=Mock(side_effect=plugins.get),
    )
    pxr.Plug = SimpleNamespace(Registry=lambda: registry)
    monkeypatch.setitem(sys.modules, "pxr", pxr)
    monkeypatch.setattr(
        launcher.module, "platform", SimpleNamespace(machine=lambda: "test-machine")
    )
    return SimpleNamespace(
        pxr=pxr, extensions=extensions, find_format=find_format, registry=registry, plugins=plugins
    )


@pytest.mark.parametrize("materialx", [False, True])
def test_launcher_runtime_info_reports_actual_mtlx_lookup(launcher, commands, pxr_probe, materialx):
    pxr_probe.find_format.return_value = object() if materialx else None
    if not materialx:
        pxr_probe.extensions.remove("mtlx")

    info = launcher.module.runtime_info()

    assert info["version"] == commands.pin.version
    assert info["materialx"] is materialx
    assert info["file_formats"] == sorted(pxr_probe.extensions)
    assert info["plugins"] == ["a-plugin", "z-plugin"]
    assert info["architecture"] == "test-machine"
    assert info["python_version"] == f"{sys.version_info.major}.{sys.version_info.minor}"
    assert info["pxr_path"] == str(Path(pxr_probe.pxr.__file__).resolve())
    assert info["validated_plugins"] == []
    pxr_probe.find_format.assert_called_once_with("mtlx")
    pxr_probe.registry.GetPluginWithName.assert_not_called()
    for plugin in pxr_probe.plugins.values():
        plugin.Load.assert_not_called()


def test_launcher_runtime_probe_explicitly_loads_required_plugins(launcher, pxr_probe):
    info = launcher.module.runtime_info(["z-plugin", "a-plugin"])

    assert info["validated_plugins"] == ["a-plugin", "z-plugin"]
    assert [call.args[0] for call in pxr_probe.registry.GetPluginWithName.call_args_list] == [
        "z-plugin",
        "a-plugin",
    ]
    for plugin in pxr_probe.plugins.values():
        plugin.Load.assert_called_once_with()


@pytest.mark.parametrize("failure", ["missing", "load-false"])
def test_launcher_runtime_probe_rejects_unloadable_required_plugins(launcher, pxr_probe, failure):
    name = "missing-plugin" if failure == "missing" else "a-plugin"
    pxr_probe.plugins["a-plugin"].Load.return_value = False

    with pytest.raises(RuntimeError, match=f"Packaged USD plugin could not be loaded: {name}"):
        launcher.module.runtime_info([name])

    pxr_probe.registry.GetPluginWithName.assert_called_once_with(name)
    pxr_probe.find_format.assert_not_called()
    if failure == "missing":
        pxr_probe.plugins["a-plugin"].Load.assert_not_called()
    else:
        pxr_probe.plugins["a-plugin"].Load.assert_called_once_with()


def test_launcher_runtime_info_command_emits_json_and_profile(launcher, monkeypatch, capsys):
    launcher.module.sys.argv += ["--runtime-info"]
    launcher.config["required_plugins"] = ["external-synthetic"]
    _write(launcher.root, "usd-runtime.json", json.dumps(launcher.config))
    probe = Mock(return_value={"version": "0.42.7"})
    monkeypatch.setattr(launcher.module, "runtime_info", probe)

    launcher.module.main()

    assert json.loads(capsys.readouterr().out) == {"version": "0.42.7", "profile": "full"}
    probe.assert_called_once_with(["external-synthetic"])


@pytest.mark.parametrize("mode", ["module", "script"])
def test_launcher_forwards_application_arguments(launcher, monkeypatch, mode):
    runpy = SimpleNamespace(run_module=Mock(), run_path=Mock())
    monkeypatch.setattr(launcher.module, "runpy", runpy)
    if mode == "module":
        launcher.module.sys.argv += ["openusdconnect.server", "--port", "9000"]
    else:
        launcher.module.sys.argv += ["--run-script", "smoke.py", "--check"]

    launcher.module.main()

    if mode == "module":
        assert launcher.module.sys.argv == ["openusdconnect.server", "--port", "9000"]
        runpy.run_module.assert_called_once_with(
            "openusdconnect.server", run_name="__main__", alter_sys=True
        )
        runpy.run_path.assert_not_called()
    else:
        assert launcher.module.sys.argv == ["smoke.py", "--check"]
        runpy.run_path.assert_called_once_with("smoke.py", run_name="__main__")
        runpy.run_module.assert_not_called()
