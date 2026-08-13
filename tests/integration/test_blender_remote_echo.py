"""End-to-end Blender feedback-loop regression tests."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys

import pytest

from openusdconnect.codec import message_to_dict
from tests.helpers import PROJECT_ROOT, run_blender, start_server, stop_server

SCRIPT = os.path.join(
    PROJECT_ROOT,
    "tests",
    "integration",
    "scripts",
    "remote_feedback_no_echo.py",
)
BASE_USD = os.path.join(PROJECT_ROOT, "test_scene.usda")


def test_remote_transform_and_materialx_changes_are_not_echoed(
    blender_exe,
    tmp_path,
    free_port,
):
    build = subprocess.run(
        [sys.executable, "scripts/build_blender_addon.py"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, f"add-on build failed:\n{build.stdout}\n{build.stderr}"

    server = start_server(tmp_path, free_port, base_path=BASE_USD)
    try:
        result = run_blender(
            blender_exe,
            SCRIPT,
            free_port,
            timeout=45,
            background=False,
        )
        print(result.stdout)
        if result.stderr:
            print(result.stderr)
        assert "SUCCESS" in result.stdout, (
            f"Blender scenario failed:\n{result.stdout}\n{result.stderr}"
        )

        db_path = tmp_path / f"events_{free_port}.db"
        with sqlite3.connect(db_path) as connection:
            rows = connection.execute(
                "SELECT seq, client_id, kind, prim, event_bin FROM events ORDER BY seq",
            ).fetchall()

        external = "remote-feedback-no-echo-external"
        shader_path = "/World/NoEchoLooks/Material/StandardSurface"
        assert [row[:3] for row in rows[:8]] == [
            (1, external, "ensure_prim"),
            (2, external, "ensure_xform_ops"),
            (3, external, "set_xform_trs"),
            (4, external, "ensure_prim"),
            (5, external, "ensure_prim"),
            (6, external, "ensure_prim"),
            (7, external, "set_connectable_input"),
            (8, external, "set_connectable_input"),
        ]
        assert len({row[3] for row in rows[:3]}) == 1
        assert rows[0][3].startswith("/World/")
        assert [row[3] for row in rows[3:]] == [
            "/World/NoEchoLooks",
            "/World/NoEchoLooks/Material",
            shader_path,
            shader_path,
            shader_path,
            "/World/NoEchoParametricParent",
            "/World/NoEchoParametricParent/Cube",
            "/World/NoEchoParametricParent/Cube",
            "/World/NoEchoParametricParent/Cube",
            "/World/NoEchoParametricParent/Cube",
            "/World/NoEchoParametricParent",
            "/World/NoEchoParametricParent/Cube",
            "/World/NoEchoParametricParent/Cube",
            "/World/NoEchoParametricParent/Cube",
        ]
        assert [row[2] for row in rows[8:13]] == [
            "ensure_prim",
            "ensure_prim",
            "set_gprim_attrs",
            "ensure_xform_ops",
            "set_xform_trs",
        ]
        assert [row[2] for row in rows[13:]] == [
            "ensure_prim",
            "ensure_prim",
            "ensure_xform_ops",
            "set_xform_trs",
        ]
        round_trip = message_to_dict(rows[-1][4])["event"]
        assert round_trip["s"] == pytest.approx([0.2, 0.4, 0.6])
    finally:
        stop_server(server)
