"""Blender shader socket edits travel through capture and reach the server."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pxr import Gf, Sdf, Usd, UsdShade

from openusdconnect.codec import message_to_dict
from openusdconnect.protocol_constants import K_SET_CONNECTABLE_INPUT
from tests.helpers import run_blender, start_server, stop_server

SCRIPT = Path(__file__).with_name("scripts") / "blender_shader_reverse_script.py"
SINGLE_PATH = "/World/Looks/Single/Surface"
MULTI_PATH = "/World/Looks/Multi/Surface"


def _create_base(path: Path) -> None:
    stage = Usd.Stage.CreateNew(str(path))
    stage.DefinePrim("/World", "Xform")
    stage.DefinePrim("/World/Looks", "Scope")

    single_material = UsdShade.Material.Define(stage, "/World/Looks/Single")
    single = UsdShade.Shader.Define(stage, SINGLE_PATH)
    single.CreateIdAttr("UsdPreviewSurface")
    single.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.2)
    single.CreateOutput("surface", Sdf.ValueTypeNames.Token)
    single_material.CreateSurfaceOutput().ConnectToSource(single.ConnectableAPI(), "surface")

    multi_material = UsdShade.Material.Define(stage, "/World/Looks/Multi")
    multi = UsdShade.Shader.Define(stage, MULTI_PATH)
    multi.CreateIdAttr("ND_standard_surface_surfaceshader")
    multi.CreateInput("base_color", Sdf.ValueTypeNames.Color3f).Set(
        Gf.Vec3f(0.2, 0.3, 0.4)
    )
    multi.CreateInput("metalness", Sdf.ValueTypeNames.Float).Set(0.15)
    multi.CreateInput("specular_roughness", Sdf.ValueTypeNames.Float).Set(0.35)
    multi.CreateOutput("out", Sdf.ValueTypeNames.Token)
    multi_material.CreateSurfaceOutput("mtlx").ConnectToSource(
        multi.ConnectableAPI(), "out"
    )
    stage.GetRootLayer().Save()


def _server_events(db_path: Path) -> list[dict]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT event_bin FROM events ORDER BY seq").fetchall()
    records = [message_to_dict(row[0]) for row in rows]
    return [record["event"] for record in records if record.get("type") == "event"]


def test_blender_shader_node_edits_reach_server(blender_exe, tmp_path, free_port):
    base = tmp_path / "shader_reverse_base.usda"
    _create_base(base)
    server = start_server(tmp_path, free_port, base_path=base)
    try:
        result = run_blender(
            blender_exe,
            str(SCRIPT),
            free_port,
            ["--base", str(base)],
            timeout=60,
        )
    finally:
        stop_server(server)

    print(result.stdout)
    if result.stderr:
        print(result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SHADER_REVERSE_SYNC_OK" in result.stdout

    shader_events = [
        event
        for event in _server_events(tmp_path / f"events_{free_port}.db")
        if event["k"] == K_SET_CONNECTABLE_INPUT
    ]
    by_path = {event["prim"]: event for event in shader_events}

    assert by_path[SINGLE_PATH]["inputs"] == {"roughness": pytest.approx(0.61)}
    assert by_path[SINGLE_PATH]["input_types"] == {"roughness": "float"}

    assert by_path[MULTI_PATH]["inputs"]["base_color"] == pytest.approx(
        [0.8, 0.25, 0.1]
    )
    assert by_path[MULTI_PATH]["input_types"]["base_color"] == "color3f"
