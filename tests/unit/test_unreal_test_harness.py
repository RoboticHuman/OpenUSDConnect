"""Unit coverage for Unreal discovery and test-project provisioning."""

from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from pxr import Gf, Sdf, Usd

from integrations.unreal.test_harness import (
    EDITOR_DRIVER,
    UnrealTestError,
    _editor_command,
    _engine_fingerprint,
    _ensure_flatbuffers_headers,
    _plugin_fingerprint,
    _stage_plugin_source,
    create_scenario,
    create_test_project,
    discover_engines,
    inspect_engine,
    install_plugin_in_project,
)

MATERIALX_TEXTURE_DIR = (
    Path(__file__).parents[2] / "assets" / "test_assets" / "MaterialXTest" / "textures"
)
MATERIALX_TEXTURES_AVAILABLE = all(
    (MATERIALX_TEXTURE_DIR / name).is_file() for name in ("brass_color.jpg", "brass_roughness.jpg")
)


def _fake_macos_engine(root: Path, version=(5, 8, 0), *, installed=True) -> Path:
    (root / "Engine" / "Binaries" / "Mac").mkdir(parents=True)
    (root / "Engine" / "Build" / "BatchFiles").mkdir(parents=True)
    (root / "Engine" / "Build" / "Build.version").write_text(
        json.dumps(
            {
                "MajorVersion": version[0],
                "MinorVersion": version[1],
                "PatchVersion": version[2],
            }
        ),
        encoding="utf-8",
    )
    for name in ("UnrealEditor", "UnrealEditor-Cmd"):
        (root / "Engine" / "Binaries" / "Mac" / name).write_text("", encoding="utf-8")
    (root / "Engine" / "Build" / "BatchFiles" / "RunUAT.sh").write_text(
        "",
        encoding="utf-8",
    )
    if installed:
        (root / "Engine" / "Build" / "InstalledBuild.txt").write_text("", encoding="utf-8")
    return root


def test_inspect_engine_accepts_root_engine_dir_and_editor_path(tmp_path):
    root = _fake_macos_engine(tmp_path / "UE_5.8")
    paths = (
        root,
        root / "Engine",
        root / "Engine" / "Binaries" / "Mac" / "UnrealEditor-Cmd",
    )
    for path in paths:
        engine = inspect_engine(path, platform_name="darwin")
        assert engine.root == root
        assert engine.version == "5.8.0"
        assert engine.association == "5.8"
        assert engine.installed_build is True


@pytest.mark.parametrize(
    ("platform_name", "binary_dir", "editor_name", "command_name", "uat_name", "target"),
    (
        (
            "win32",
            "Win64",
            "UnrealEditor.exe",
            "UnrealEditor-Cmd.exe",
            "RunUAT.bat",
            "Win64",
        ),
        ("linux", "Linux", "UnrealEditor", "UnrealEditor-Cmd", "RunUAT.sh", "Linux"),
    ),
)
def test_inspect_engine_supports_windows_and_linux_source_layouts(
    tmp_path,
    platform_name,
    binary_dir,
    editor_name,
    command_name,
    uat_name,
    target,
):
    root = tmp_path / platform_name / "UnrealEngine"
    binaries = root / "Engine" / "Binaries" / binary_dir
    batch_files = root / "Engine" / "Build" / "BatchFiles"
    binaries.mkdir(parents=True)
    batch_files.mkdir(parents=True)
    (root / "Engine" / "Build" / "Build.version").write_text(
        json.dumps({"MajorVersion": 5, "MinorVersion": 8, "PatchVersion": 1}),
        encoding="utf-8",
    )
    (binaries / editor_name).write_text("", encoding="utf-8")
    (binaries / command_name).write_text("", encoding="utf-8")
    (batch_files / uat_name).write_text("", encoding="utf-8")

    engine = inspect_engine(root, platform_name=platform_name)

    assert engine.target_platform == target
    assert engine.version == "5.8.1"
    assert engine.installed_build is False


def test_inspect_engine_rejects_incomplete_tree(tmp_path):
    with pytest.raises(UnrealTestError, match="not a usable Unreal Engine root"):
        inspect_engine(tmp_path / "not-an-engine", platform_name="darwin")


def test_environment_override_precedes_launcher_detection(tmp_path):
    source_root = _fake_macos_engine(tmp_path / "source", installed=False)
    engines = discover_engines(
        env={"HOME": str(tmp_path), "OUC_UNREAL_ENGINE_ROOT": str(source_root)},
        config_path=tmp_path / "missing.cfg",
        manifest_dirs=(tmp_path / "manifests",),
        platform_name="darwin",
    )
    assert [engine.root for engine in engines] == [source_root]
    assert engines[0].source == "OUC_UNREAL_ENGINE_ROOT"
    assert engines[0].installed_build is False


def test_launcher_manifest_and_project_association_are_used(tmp_path):
    root = _fake_macos_engine(tmp_path / "UE_5.7", version=(5, 7, 2))
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    (manifests / "engine.item").write_text(
        json.dumps(
            {
                "AppName": "UE_5.7",
                "DisplayName": "Unreal Engine",
                "InstallLocation": str(root),
            }
        ),
        encoding="utf-8",
    )
    (manifests / "plugin.item").write_text(
        json.dumps(
            {
                "AppName": "FabPlugin_5.7",
                "DisplayName": "Fab UE Plugin",
                "InstallLocation": str(root),
            }
        ),
        encoding="utf-8",
    )
    project = tmp_path / "Probe.uproject"
    project.write_text(json.dumps({"EngineAssociation": "5.7"}), encoding="utf-8")

    engines = discover_engines(
        project=project,
        env={"HOME": str(tmp_path)},
        config_path=tmp_path / "missing.cfg",
        manifest_dirs=(manifests,),
        platform_name="darwin",
    )
    assert engines[0].root == root
    assert engines[0].association == "5.7"
    assert engines[0].source.startswith("Epic Launcher manifest")


def test_config_file_targets_source_build(tmp_path):
    root = _fake_macos_engine(tmp_path / "custom-source", installed=False)
    config = tmp_path / "unreal.test.cfg"
    config.write_text(str(root) + "\n", encoding="utf-8")
    engines = discover_engines(
        env={"HOME": str(tmp_path)},
        config_path=config,
        manifest_dirs=(),
        platform_name="darwin",
    )
    assert [engine.root for engine in engines] == [root]
    assert engines[0].source == "unreal.test.cfg"


def test_macos_registration_preserves_source_build_association(tmp_path):
    root = _fake_macos_engine(tmp_path / "custom-source", version=(99, 1, 0), installed=False)
    install_ini = (
        tmp_path / "Library" / "Application Support" / "Epic" / "UnrealEngine" / "Install.ini"
    )
    install_ini.parent.mkdir(parents=True)
    install_ini.write_text(
        f"[Installations]\n{{A1b2-C3d4}}={root}\n",
        encoding="utf-8",
    )

    engines = discover_engines(
        env={"HOME": str(tmp_path)},
        config_path=tmp_path / "missing.cfg",
        manifest_dirs=(tmp_path / "missing-manifests",),
        platform_name="darwin",
    )

    assert engines[0].root == root
    assert engines[0].association == "{A1b2-C3d4}"


def test_generated_project_installs_and_enables_plugin(tmp_path):
    engine = inspect_engine(
        _fake_macos_engine(tmp_path / "UE_5.8"),
        platform_name="darwin",
    )
    package = tmp_path / "package"
    package.mkdir()
    (package / "OpenUSDConnect.uplugin").write_text("{}", encoding="utf-8")
    project = create_test_project(
        tmp_path / "project",
        engine,
        package,
        port=17420,
        enable_substrate=True,
    )

    descriptor = json.loads(project.read_text(encoding="utf-8"))
    enabled = {item["Name"] for item in descriptor["Plugins"] if item["Enabled"]}
    assert enabled == {
        "EditorScriptingUtilities",
        "OpenUSDConnect",
        "PythonScriptPlugin",
        "USDImporter",
    }
    assert (project.parent / "Plugins" / "OpenUSDConnect" / "OpenUSDConnect.uplugin").is_file()
    settings = (project.parent / "Config" / "DefaultGame.ini").read_text(encoding="utf-8")
    assert "ServerPort=17420" in settings
    assert "bAutoConnect=False" in settings
    renderer_settings = (project.parent / "Config" / "DefaultEngine.ini").read_text(
        encoding="utf-8"
    )
    assert "r.Substrate=True" in renderer_settings
    usd_settings = (project.parent / "Config" / "DefaultUSDCore.ini").read_text(encoding="utf-8")
    assert "bShowCreateDefaultAssetCacheDialog=False" in usd_settings


def test_existing_project_install_enables_test_dependencies(tmp_path):
    project = tmp_path / "Existing.uproject"
    project.write_text(
        json.dumps(
            {
                "FileVersion": 3,
                "Plugins": [
                    {"Name": "OtherPlugin", "Enabled": False},
                    {"Name": "USDImporter", "Enabled": False, "Optional": True},
                ],
            }
        ),
        encoding="utf-8",
    )
    package = tmp_path / "package"
    package.mkdir()
    (package / "OpenUSDConnect.uplugin").write_text("{}\n", encoding="utf-8")

    destination = install_plugin_in_project(project, package)

    assert (destination / "OpenUSDConnect.uplugin").is_file()
    descriptor = json.loads(project.read_text(encoding="utf-8"))
    plugins = {item["Name"]: item for item in descriptor["Plugins"]}
    assert plugins["OtherPlugin"]["Enabled"] is False
    assert plugins["USDImporter"] == {
        "Name": "USDImporter",
        "Enabled": True,
        "Optional": True,
    }
    assert plugins["PythonScriptPlugin"]["Enabled"] is True
    assert plugins["EditorScriptingUtilities"]["Enabled"] is True
    assert plugins["OpenUSDConnect"]["Enabled"] is True


def test_plugin_fingerprint_ignores_non_build_documentation(tmp_path):
    plugin = tmp_path / "OpenUSDConnect"
    source = plugin / "Source" / "Module" / "Private" / "Module.cpp"
    source.parent.mkdir(parents=True)
    source.write_text("int value = 1;\n", encoding="utf-8")
    (plugin / "OpenUSDConnect.uplugin").write_text("{}\n", encoding="utf-8")
    readme = plugin / "README.md"
    readme.write_text("first\n", encoding="utf-8")

    fingerprint = _plugin_fingerprint(plugin)
    readme.write_text("second\n", encoding="utf-8")
    assert _plugin_fingerprint(plugin) == fingerprint

    source.write_text("int value = 2;\n", encoding="utf-8")
    assert _plugin_fingerprint(plugin) != fingerprint


def test_unreal_source_staging_vendors_canonical_client_core(tmp_path):
    plugin = tmp_path / "plugin"
    (plugin / "Source").mkdir(parents=True)
    (plugin / "OpenUSDConnect.uplugin").write_text("{}\n", encoding="utf-8")
    stale = (
        plugin
        / "Source"
        / "ThirdParty"
        / "OpenUSDConnectClientCore"
        / "include"
        / "openusdconnect"
        / "client"
        / "producer_session.h"
    )
    stale.parent.mkdir(parents=True)
    stale.write_text("// stale staged core\n", encoding="utf-8")
    core = tmp_path / "client_core"
    header = core / "include" / "openusdconnect" / "client" / "producer_session.h"
    header.parent.mkdir(parents=True)
    header.write_text("// canonical core\n", encoding="utf-8")

    staged = _stage_plugin_source(
        plugin,
        tmp_path / "staged",
        client_core_source=core,
    )

    staged_header = (
        staged
        / "Source"
        / "ThirdParty"
        / "OpenUSDConnectClientCore"
        / "include"
        / "openusdconnect"
        / "client"
        / "producer_session.h"
    )
    assert staged_header.read_text(encoding="utf-8") == "// canonical core\n"


def test_engine_fingerprint_distinguishes_builds_and_installations(tmp_path):
    first_root = _fake_macos_engine(tmp_path / "first")
    second_root = _fake_macos_engine(tmp_path / "second")
    first = inspect_engine(first_root, platform_name="darwin")
    second = inspect_engine(second_root, platform_name="darwin")

    fingerprint = _engine_fingerprint(first)
    assert _engine_fingerprint(second) != fingerprint

    modules = first.editor.parent / "UnrealEditor.modules"
    modules.write_text('{"BuildId":"new-build"}\n', encoding="utf-8")
    assert _engine_fingerprint(first) != fingerprint


def test_flatbuffers_headers_are_bootstrapped_once(tmp_path, monkeypatch):
    plugin = tmp_path / "OpenUSDConnect"
    setup_script = plugin / "setup_flatbuffers.py"
    setup_script.parent.mkdir(parents=True)
    setup_script.write_text("# test setup\n", encoding="utf-8")
    header = (
        plugin / "Source/OpenUSDConnectPXR/ThirdParty/flatbuffers/include/flatbuffers/flatbuffers.h"
    )
    calls = []

    def run_setup(command, **kwargs):
        calls.append((command, kwargs))
        header.parent.mkdir(parents=True)
        header.write_text("// flatbuffers\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "installed", "")

    monkeypatch.setattr("integrations.unreal.test_harness.subprocess.run", run_setup)

    _ensure_flatbuffers_headers(plugin)
    _ensure_flatbuffers_headers(plugin)

    assert len(calls) == 1
    assert calls[0][0][1] == str(setup_script)
    assert calls[0][1]["cwd"] == plugin


def test_flatbuffers_bootstrap_reports_missing_setup_script(tmp_path):
    with pytest.raises(UnrealTestError, match="setup script was not found"):
        _ensure_flatbuffers_headers(tmp_path / "OpenUSDConnect")


def test_unattended_editor_command_keeps_rendering_enabled(tmp_path):
    engine = inspect_engine(
        _fake_macos_engine(tmp_path / "UE_5.8"),
        platform_name="darwin",
    )
    project = tmp_path / "Project.uproject"
    unreal_log = tmp_path / "unreal.log"

    command = _editor_command(engine, project, unreal_log, interactive=False)

    assert command[0] == str(engine.editor_cmd)
    assert "-unattended" in command
    assert "-nullrhi" not in command
    assert f"-abslog={unreal_log}" in command


def test_interactive_editor_command_uses_gui_without_unattended(tmp_path):
    engine = inspect_engine(
        _fake_macos_engine(tmp_path / "UE_5.8"),
        platform_name="darwin",
    )

    command = _editor_command(
        engine,
        tmp_path / "Project.uproject",
        tmp_path / "unreal.log",
        interactive=True,
    )

    assert command[0] == str(engine.editor)
    assert "-unattended" not in command
    assert "-nullrhi" not in command


@pytest.mark.skipif(
    not MATERIALX_TEXTURES_AVAILABLE,
    reason="USD WG assets submodule not present",
)
def test_scenario_expected_layers_cover_material_updates(tmp_path):
    scenario = create_scenario(tmp_path, port=17421, python_executable=Path("/python"))
    baseline = Usd.Stage.Open(str(scenario.base_stage))
    initial = Usd.Stage.Open(str(scenario.expected_initial))
    final = Usd.Stage.Open(str(scenario.expected_final))

    baseline_preview = baseline.GetPrimAtPath("/World/Looks/Preview/Surface")
    initial_preview = initial.GetPrimAtPath("/World/Looks/Preview/Surface")
    final_preview = final.GetPrimAtPath("/World/Looks/Preview/Surface")
    assert baseline_preview.GetAttribute("inputs:diffuseColor").Get() == Gf.Vec3f(
        0.12,
        0.48,
        0.9,
    )
    assert initial_preview.GetAttribute("inputs:diffuseColor").Get() == Gf.Vec3f(
        0.82,
        0.08,
        0.2,
    )
    assert final_preview.GetAttribute("inputs:diffuseColor").Get() == Gf.Vec3f(
        0.18,
        0.62,
        0.95,
    )
    assert final_preview.GetAttribute("inputs:roughness").Get() == pytest.approx(0.24)

    texture = final.GetPrimAtPath("/World/Looks/Textured/Texture")
    asset = texture.GetAttribute("inputs:file").Get()
    assert isinstance(asset, Sdf.AssetPath)
    assert asset.path == "./textures/brass_roughness.jpg"
    assert texture.GetAttribute("inputs:st").GetTypeName() == Sdf.ValueTypeNames.Float2
    assert texture.GetAttribute("outputs:rgb").GetTypeName() == Sdf.ValueTypeNames.Float3

    primvar = final.GetPrimAtPath("/World/Looks/Textured/Primvar")
    assert primvar.GetAttribute("outputs:result").GetTypeName() == Sdf.ValueTypeNames.Float2

    textured_mesh = final.GetPrimAtPath("/World/TexturedPanel")
    assert textured_mesh.GetTypeName() == "Mesh"
    st = textured_mesh.GetAttribute("primvars:st")
    assert st.GetTypeName() == Sdf.ValueTypeNames.TexCoord2fArray
    assert st.GetMetadata("interpolation") == "faceVarying"
    assert len(st.Get()) == 4

    material_x = final.GetPrimAtPath("/World/Looks/MaterialX/Surface")
    assert material_x.GetAttribute("inputs:base_color").Get() == Gf.Vec3f(0.8, 0.42, 0.12)
    assert material_x.GetAttribute("inputs:specular_roughness").Get() == pytest.approx(0.18)

    live_sphere = final.GetPrimAtPath("/World/LiveSphere")
    assert live_sphere.GetTypeName() == "Sphere"
    assert live_sphere.GetAttribute("radius").Get() == pytest.approx(0.75)
    assert live_sphere.GetAttribute("xformOp:translate").Get() == Gf.Vec3d(2.25, 1.0, -0.5)
    assert list(live_sphere.GetAttribute("primvars:displayColor").Get())[0] == Gf.Vec3f(
        0.12,
        0.8,
        0.32,
    )

    live_surface = final.GetPrimAtPath("/World/Looks/Live/Surface")
    live_texture = final.GetPrimAtPath("/World/Looks/Live/Texture")
    live_primvar = final.GetPrimAtPath("/World/Looks/Live/Primvar")
    assert live_surface.GetAttribute("inputs:diffuseColor").GetTypeName() == (
        Sdf.ValueTypeNames.Color3f
    )
    assert live_texture.GetAttribute("inputs:st").GetTypeName() == Sdf.ValueTypeNames.Float2
    assert live_texture.GetAttribute("outputs:rgb").GetTypeName() == Sdf.ValueTypeNames.Float3
    assert live_primvar.GetAttribute("outputs:result").GetTypeName() == Sdf.ValueTypeNames.Float2
    assert scenario.initial_events
    assert scenario.update_events


@pytest.mark.skipif(
    not MATERIALX_TEXTURES_AVAILABLE,
    reason="USD WG assets submodule not present",
)
def test_create_scenario_discards_previous_run_state(tmp_path):
    stale_paths = [
        tmp_path / "events.db",
        tmp_path / "events.db-shm",
        tmp_path / "events.db-wal",
        tmp_path / "unreal-result.json",
    ]
    for path in stale_paths:
        path.write_text("stale", encoding="utf-8")

    create_scenario(tmp_path, port=17421, python_executable=Path("/python"))

    assert all(not path.exists() for path in stale_paths)


@pytest.mark.parametrize("has_events", [True, False])
def test_editor_sequence_poll_closes_connection(tmp_path, has_events):
    connection = sqlite3.connect(tmp_path / "events.db")
    if has_events:
        connection.execute("CREATE TABLE events (seq INTEGER)")
        connection.execute("INSERT INTO events VALUES (7)")
        connection.commit()
    # The driver imports Unreal and starts an editor scenario at module scope.
    # Compile only its stdlib polling helper so this contract needs no editor.
    tree = ast.parse(EDITOR_DRIVER.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_max_sequence"
    )
    namespace = {
        "CONFIG": {"database_path": str(tmp_path / "events.db")},
        "closing": closing,
        "sqlite3": SimpleNamespace(connect=lambda *args, **kwargs: connection, Error=sqlite3.Error),
    }
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), str(EDITOR_DRIVER), "exec"), namespace
    )

    assert namespace["_max_sequence"]() == (7 if has_events else 0)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
