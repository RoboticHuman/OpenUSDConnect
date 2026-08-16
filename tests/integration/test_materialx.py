"""Pytest wrappers for MaterialX integration tests.

Skipped if Blender is not configured (see conftest.py for options).
"""

import json
import os
import struct
import subprocess
import sys
import zlib

import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade

from openusdconnect.server import UsdSyncServer
from openusdconnect.server.vfs import VirtualStageFile
from tests.helpers import PROJECT_ROOT, read_results, run_blender, start_server, stop_server

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "scripts")
TEST_SCRIPT = os.path.join(SCRIPTS_DIR, "materialx_test_script.py")
MTLX_REF_EMITTER = os.path.join(SCRIPTS_DIR, "mtlx_ref_emitter_script.py")
MTLX_REF_RECEIVER = os.path.join(SCRIPTS_DIR, "mtlx_ref_receiver_script.py")
MTLX_VFS_IMPORT = os.path.join(SCRIPTS_DIR, "materialx_vfs_import_script.py")
PACKAGED_OPENPBR = os.path.join(SCRIPTS_DIR, "packaged_openpbr_test_script.py")
PREVIEW_ENRICHMENT = os.path.join(SCRIPTS_DIR, "blender_preview_enrichment_script.py")
ADDON_ZIP = os.path.join(PROJECT_ROOT, "dist", "usd_connect_blender.zip")
TEAPOT_ASSET = os.path.join(
    PROJECT_ROOT,
    "assets",
    "intent-vfx",
    "assets",
    "teapot",
    "teapot.usd",
)
REFERENCED_TEXTURE_ASSET = os.path.join(
    PROJECT_ROOT,
    "tests",
    "fixtures",
    "materialx_vfs",
    "basic_textured.usda",
)
DIRECT_TEXTURE_ASSET = os.path.join(
    PROJECT_ROOT,
    "assets",
    "test_assets",
    "MaterialXTest",
    "basicTextured_flatten.usda",
)


def _write_rgb_png(path):
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    width = height = 2
    rows = b"".join(
        b"\x00" + bytes((255, 32, 16, 32, 255, 16))
        for _ in range(height)
    )
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def _write_preview_texture_scene(path, texture_path):
    stage = Usd.Stage.CreateNew(str(path))
    world = UsdGeom.Xform.Define(stage, "/World")
    stage.SetDefaultPrim(world.GetPrim())
    mesh = UsdGeom.Mesh.Define(stage, "/World/Mesh")
    mesh.CreatePointsAttr(
        [Gf.Vec3f(-1, 0, 0), Gf.Vec3f(1, 0, 0), Gf.Vec3f(0, 0, 1)]
    )
    mesh.CreateFaceVertexCountsAttr([3])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2])
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    UsdGeom.PrimvarsAPI(mesh).CreatePrimvar(
        "st",
        Sdf.ValueTypeNames.TexCoord2fArray,
        UsdGeom.Tokens.faceVarying,
    ).Set([Gf.Vec2f(0, 0), Gf.Vec2f(1, 0), Gf.Vec2f(0.5, 1)])
    material = UsdShade.Material.Define(stage, "/World/Looks/PreviewMat")
    surface = UsdShade.Shader.Define(stage, "/World/Looks/PreviewMat/Surface")
    surface.CreateIdAttr("UsdPreviewSurface")
    surface.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.35)
    surface_output = surface.CreateOutput("surface", Sdf.ValueTypeNames.Token)
    material.CreateSurfaceOutput().ConnectToSource(surface_output)

    texture = UsdShade.Shader.Define(stage, "/World/Looks/PreviewMat/Texture")
    texture.CreateIdAttr("UsdUVTexture")
    texture.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(
        Sdf.AssetPath(texture_path.name)
    )
    texture.CreateInput("fallback", Sdf.ValueTypeNames.Float4).Set(
        Gf.Vec4f(1.0, 0.0, 0.0, 1.0)
    )
    texture_output = texture.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
    surface.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
        texture_output
    )
    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)
    stage.GetRootLayer().Save()


@pytest.mark.materialx
def test_materialx_standard_surface(blender_exe):
    """Run MaterialX mapper tests inside headless Blender."""
    env = os.environ.copy()
    env["BLENDER_USER_RESOURCES"] = os.path.join(
        PROJECT_ROOT,
        ".blender",
        "user_data",
    )
    result = subprocess.run(
        [blender_exe, "--background", "--python", TEST_SCRIPT],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)
    assert result.returncode == 0, f"MaterialX tests failed:\n{result.stdout}\n{result.stderr}"


@pytest.mark.materialx
def test_packaged_addon_contains_openpbr_translation(blender_exe, tmp_path):
    build = subprocess.run(
        [sys.executable, "scripts/build_blender_addon.py"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stderr
    result_path = tmp_path / "packaged-openpbr.json"
    env = os.environ.copy()
    env["BLENDER_USER_RESOURCES"] = os.path.join(PROJECT_ROOT, ".blender", "user_data")
    result = subprocess.run(
        [
            blender_exe,
            "--factory-startup",
            "--background",
            "--python",
            PACKAGED_OPENPBR,
            "--",
            "--addon",
            ADDON_ZIP,
            "--out",
            str(result_path),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["nodes"] >= 1
    assert "base_color" in payload["inputs"]


@pytest.mark.materialx
def test_materialx_reference_pipeline(blender_exe, tmp_path, free_port):
    """Full pipeline: emitter sends teapot reference → server → receiver.

    Verifies hierarchy collapse (no redundant root) and MaterialX
    enrichment (the shader mapper builds the Standard Surface network).
    """
    port = free_port
    server_proc = start_server(tmp_path, port)
    try:
        # Send teapot reference
        emitter_result = subprocess.run(
            [
                sys.executable,
                MTLX_REF_EMITTER,
                "--port",
                str(port),
                "--asset-path",
                TEAPOT_ASSET,
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        print("=== Emitter ===")
        print(emitter_result.stdout)
        assert emitter_result.returncode == 0, emitter_result.stderr

        # Receive and verify in Blender
        results_path = str(tmp_path / "mtlx_ref_results.json")
        blender_result = run_blender(
            blender_exe,
            MTLX_REF_RECEIVER,
            port,
            extra_args=["--out", results_path],
            timeout=30,
        )
        print("=== Receiver ===")
        print(blender_result.stdout)
        if blender_result.stderr:
            print(blender_result.stderr)

        results = read_results(results_path, "mtlx_ref")
        failures = {k: v for k, v in results.items() if v.startswith("FAIL")}
        assert not failures, f"MaterialX reference test failures: {failures}"
    finally:
        stop_server(server_proc)


@pytest.mark.materialx
def test_materialx_vfs_import_resolves_textures_and_renders(
    blender_exe,
    tmp_path,
    free_port,
):
    """The supported live-open operator preserves valid referenced MaterialX textures."""
    build = subprocess.run(
        [sys.executable, "scripts/build_blender_addon.py"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stderr

    server = UsdSyncServer(
        base_usd_path=REFERENCED_TEXTURE_ASSET,
        log_path=str(tmp_path / "materialx-vfs.db"),
    )
    try:
        virtual_file = VirtualStageFile(
            server,
            name="scene.usd",
            advertise_host="127.0.0.1",
            sync_port=free_port,
        )
        scene_path = tmp_path / "scene.usd"
        scene_path.write_bytes(virtual_file.read())
    finally:
        server.shutdown()
        server.store.close()

    result_path = tmp_path / "materialx-vfs-result.json"
    render_path = tmp_path / "materialx-vfs.png"
    env = os.environ.copy()
    env["BLENDER_USER_RESOURCES"] = os.path.join(PROJECT_ROOT, ".blender", "user_data")
    result = subprocess.run(
        [
            blender_exe,
            "--factory-startup",
            "--background",
            "--python",
            MTLX_VFS_IMPORT,
            "--",
            "--scene",
            str(scene_path),
            "--addon",
            ADDON_ZIP,
            "--out",
            str(result_path),
            "--render",
            str(render_path),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr)
    assert result.returncode == 0, result.stderr
    metrics = json.loads(result_path.read_text(encoding="utf-8"))
    assert metrics["status"] == "PASS", metrics
    assert render_path.stat().st_size > 10_000

    direct_result_path = tmp_path / "materialx-direct-result.json"
    direct_render_path = tmp_path / "materialx-direct.png"
    direct = subprocess.run(
        [
            blender_exe,
            "--factory-startup",
            "--background",
            "--python",
            MTLX_VFS_IMPORT,
            "--",
            "--scene",
            DIRECT_TEXTURE_ASSET,
            "--addon",
            ADDON_ZIP,
            "--out",
            str(direct_result_path),
            "--render",
            str(direct_render_path),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    print(direct.stdout)
    if direct.stderr:
        print(direct.stderr)
    assert direct.returncode == 0, direct.stderr
    direct_metrics = json.loads(direct_result_path.read_text(encoding="utf-8"))
    assert direct_metrics["status"] == "PASS", direct_metrics
    assert metrics["node_count"] == direct_metrics["node_count"]
    assert metrics["link_count"] == direct_metrics["link_count"]
    assert metrics["pixel_digest"] == direct_metrics["pixel_digest"]


@pytest.mark.materialx
def test_tagged_import_preserves_native_preview_surface_graph(blender_exe, tmp_path):
    """Post-import MaterialX enrichment must not duplicate native preview nodes."""
    build = subprocess.run(
        [sys.executable, "scripts/build_blender_addon.py"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stderr

    texture_path = tmp_path / "preview.png"
    scene_path = tmp_path / "preview.usda"
    result_path = tmp_path / "preview-enrichment.json"
    _write_rgb_png(texture_path)
    _write_preview_texture_scene(scene_path, texture_path)

    env = os.environ.copy()
    env["BLENDER_USER_RESOURCES"] = str(tmp_path / "blender-user")
    result = subprocess.run(
        [
            blender_exe,
            "--factory-startup",
            "--background",
            "--python",
            PREVIEW_ENRICHMENT,
            "--",
            "--scene",
            str(scene_path),
            "--addon",
            ADDON_ZIP,
            "--out",
            str(result_path),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["equal"] is True
    assert payload["native"]["image_nodes"] == 1
