"""Opt-in end-to-end verification against a real Unreal Editor process."""

from pathlib import Path

import pytest

from integrations.unreal.test_harness import resolve_engine, run_unreal_e2e

pytestmark = pytest.mark.unreal


def test_unreal_stage_material_and_reverse_parity(request, tmp_path):
    project_value = request.config.getoption("--unreal-project")
    project = Path(project_value) if project_value else None
    engine = resolve_engine(
        explicit=request.config.getoption("--unreal-engine"),
        project=project,
    )
    package_value = request.config.getoption("--unreal-plugin-package")
    plugin_package = Path(package_value).expanduser().resolve() if package_value else None
    if plugin_package is not None:
        assert (plugin_package / "OpenUSDConnect.uplugin").is_file(), (
            f"invalid Unreal plugin package: {plugin_package}"
        )

    run = run_unreal_e2e(
        engine,
        tmp_path / "unreal-e2e",
        project=project,
        plugin_package=plugin_package,
        rebuild_plugin=request.config.getoption("--unreal-rebuild-plugin"),
    )

    assert run.unreal_log.is_file()
    assert run.unreal_log.stat().st_size > 0
    assert run.unreal_console_log.is_file()
    assert not list(run.work_dir.glob("unreal_*.log"))
    assert run.result["layer_parity"] is True
    assert run.result["live_created_component"] == "StaticMeshComponent"
    assert run.result["reverse_edits_emitted"] is True
    assert run.result["outage_reconnected"] is True
    assert run.result["offline_edits_emitted"] is True
    reverse = run.result["server_reverse_state"]
    assert reverse["roughness"] == pytest.approx(0.07)
    assert reverse["translate"] == pytest.approx([9.0, 3.0, 2.0])

    for phase in ("initial_materials", "final_materials"):
        materials = run.result[phase]
        assert set(materials) == {
            "/World/MaterialXBall",
            "/World/PreviewBall",
            "/World/TexturedPanel",
        }
        assert all(item["component_class"] == "StaticMeshComponent" for item in materials.values())
        assert not materials["/World/MaterialXBall"]["material_name"].startswith("MI_DisplayColor")

    initial = run.result["initial_materials"]["/World/PreviewBall"]
    assert initial["scalar_parameters"]["Metallic"] == pytest.approx(0.7)
    assert initial["scalar_parameters"]["Roughness"] == pytest.approx(0.16)
    assert initial["vector_parameters"]["BaseColor"][:3] == pytest.approx([0.82, 0.08, 0.2])

    final = run.result["final_materials"]
    preview = final["/World/PreviewBall"]
    assert preview["scalar_parameters"]["Metallic"] == pytest.approx(0.45)
    assert preview["scalar_parameters"]["Roughness"] == pytest.approx(0.24)
    assert preview["vector_parameters"]["BaseColor"][:3] == pytest.approx([0.18, 0.62, 0.95])

    textured = final["/World/TexturedPanel"]
    assert textured["scalar_parameters"]["UseBaseColorTexture"] == pytest.approx(1.0)
    assert "brass_roughness" in textured["texture_parameters"]["BaseColorTexture"]
    assert (
        final["/World/MaterialXBall"]["material_name"]
        != run.result["initial_materials"]["/World/MaterialXBall"]["material_name"]
    )
