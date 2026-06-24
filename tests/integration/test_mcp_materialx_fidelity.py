"""E2E: author UsdPreviewSurface + MaterialX shader networks over the wire and
assert the resulting topology in the mirror: the 'recreate real USD files' proof.

Input types come from the Sdr discovery API (the recommended author workflow),
so the authored graph matches what a real file would carry.
"""

import socket

import pytest

from integrations.mcp import discovery, introspection
from integrations.mcp.config import McpConfig
from integrations.mcp.session import ConnectionSession
from integrations.mcp.validation import validate_and_prepare
from tests.helpers import start_server, stop_server


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def server(tmp_path):
    port = _free_port()
    proc = start_server(tmp_path, port)
    yield port
    stop_server(proc)


def _connect(port):
    session = ConnectionSession(McpConfig(port=port, client_id="mcp-test"))
    session.connect()
    return session


def _author(session, events):
    prepared, _ = validate_and_prepare(
        events,
        stage=session.mirror_stage,
        auto_create_ancestors=True,
        node_exists=discovery.node_exists,
    )
    return session.send(prepared)


def _typed(info_id, values):
    """Resolve Sdf input_types from the Sdr registry, as Claude would."""
    types = {}
    for name in values:
        t = discovery.resolve_input_type(info_id, name)
        assert t, f"{info_id}.{name} has no Sdr type"
        types[name] = t
    return values, types


def test_usd_preview_surface_network_topology(server):
    session = _connect(server)
    try:
        surf_vals, surf_types = _typed("UsdPreviewSurface", {"roughness": 0.4, "metallic": 0.0})
        mat = "/World/Looks/Tex"
        events = [
            {"k": "ensure_prim", "prim": mat, "typeName": "Material"},
            {"k": "ensure_prim", "prim": f"{mat}/Surface", "typeName": "Shader"},
            {"k": "ensure_prim", "prim": f"{mat}/Diffuse", "typeName": "Shader"},
            {
                "k": "set_connectable_input",
                "prim": f"{mat}/Surface",
                "info_id": "UsdPreviewSurface",
                "inputs": surf_vals,
                "input_types": surf_types,
            },
            {
                "k": "set_connectable_input",
                "prim": f"{mat}/Diffuse",
                "info_id": "UsdUVTexture",
                "inputs": {},
                "input_types": {},
            },
            {
                "k": "set_connectable_connection",
                "prim": f"{mat}/Surface",
                "connections": {
                    "inputs:diffuseColor": {
                        "source_prim": f"{mat}/Diffuse",
                        "source_attr": "outputs:rgb",
                    }
                },
            },
            {
                "k": "set_connectable_connection",
                "prim": mat,
                "connections": {
                    "outputs:surface": {
                        "source_prim": f"{mat}/Surface",
                        "source_attr": "outputs:surface",
                    }
                },
            },
        ]
        result = _author(session, events)
        assert result["mirror_synced"]

        net = introspection.describe_shader_network(session.mirror_stage, mat)
        shaders = {s["path"]: s for s in net["shaders"]}

        surface = shaders[f"{mat}/Surface"]
        assert surface["info_id"] == "UsdPreviewSurface"
        assert surface["inputs"]["roughness"] == pytest.approx(0.4)
        assert surface["connections"]["inputs:diffuseColor"]["source_prim"] == f"{mat}/Diffuse"

        assert shaders[f"{mat}/Diffuse"]["info_id"] == "UsdUVTexture"

        material = shaders[mat]
        assert any("surface" in key for key in material["connections"]), material["connections"]
    finally:
        session.disconnect()


def test_materialx_standard_surface_topology(server):
    session = _connect(server)
    try:
        out_name = discovery.describe_shader_node("ND_standard_surface_surfaceshader")["outputs"][
            0
        ]["name"]
        vals, types = _typed(
            "ND_standard_surface_surfaceshader",
            {"base": 1.0, "metalness": 1.0, "base_color": [0.71, 0.65, 0.26]},
        )
        mat = "/World/Looks/MtlxBrass"
        events = [
            {"k": "ensure_prim", "prim": mat, "typeName": "Material"},
            {"k": "ensure_prim", "prim": f"{mat}/Surface", "typeName": "Shader"},
            {
                "k": "set_connectable_input",
                "prim": f"{mat}/Surface",
                "info_id": "ND_standard_surface_surfaceshader",
                "inputs": vals,
                "input_types": types,
            },
            {
                "k": "set_connectable_connection",
                "prim": mat,
                "connections": {
                    "outputs:mtlx:surface": {
                        "source_prim": f"{mat}/Surface",
                        "source_attr": f"outputs:{out_name}",
                    }
                },
            },
        ]
        result = _author(session, events)
        assert result["mirror_synced"]

        net = introspection.describe_shader_network(session.mirror_stage, mat)
        shaders = {s["path"]: s for s in net["shaders"]}

        surface = shaders[f"{mat}/Surface"]
        assert surface["info_id"] == "ND_standard_surface_surfaceshader"
        assert surface["inputs"]["base"] == pytest.approx(1.0)
        assert surface["inputs"]["base_color"] == pytest.approx([0.71, 0.65, 0.26])

        material = shaders[mat]
        assert any("surface" in key for key in material["connections"]), material["connections"]
    finally:
        session.disconnect()
