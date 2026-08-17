"""Tests for the live Material Zoo runner helpers."""

from types import SimpleNamespace

import pytest

from scripts import run_material_zoo


def test_stinson_beach_comes_from_selected_usdview_install(tmp_path):
    executable = tmp_path / "OpenUSDInstall" / "bin" / "usdview.cmd"
    texture = (
        executable.parent.parent
        / "lib"
        / "usd"
        / "hdx"
        / "resources"
        / "textures"
        / "StinsonBeach.hdr"
    )
    executable.parent.mkdir(parents=True)
    texture.parent.mkdir(parents=True)
    executable.touch()
    texture.touch()

    assert run_material_zoo._stinson_beach_for_usdview(executable) == str(texture)


def test_viewer_failure_is_reported():
    process = SimpleNamespace(pid=1234, poll=lambda: 7)

    with pytest.raises(RuntimeError, match="viewer process 1234 exited early with code 7"):
        run_material_zoo._raise_if_viewer_failed([process])
