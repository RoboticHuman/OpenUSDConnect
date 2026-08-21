from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tests.openusd_pin import (
    OPENUSD_COMMIT,
    OPENUSD_CORE_VERSION,
    OPENUSD_TAG,
    OPENUSD_VERSION,
)
from tests.python_version import PYTHON_VERSION_PARTS


def _load_script():
    path = Path(__file__).parents[2] / "scripts" / "build_openusd.py"
    spec = importlib.util.spec_from_file_location("build_openusd", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


build_openusd = _load_script()


def test_required_python_comes_from_version_file():
    assert build_openusd.REQUIRED_PYTHON == PYTHON_VERSION_PARTS


def _args(tmp_path: Path, *arguments: str):
    pin = build_openusd.load_pin()
    parser = build_openusd._parser(pin)
    return pin, parser.parse_args(["--root", str(tmp_path), *arguments])


def test_pin_matches_openusd_lock():
    pin = build_openusd.load_pin()

    assert pin.version == OPENUSD_VERSION
    assert pin.usd_core == OPENUSD_CORE_VERSION
    assert pin.tag == OPENUSD_TAG
    assert pin.commit == OPENUSD_COMMIT


def test_runtime_profile_builds_headless_python_runtime(tmp_path):
    pin, args = _args(tmp_path)
    plan = build_openusd.create_plan(args, pin)
    command = build_openusd.upstream_command(plan)

    assert plan.features.python
    assert plan.features.tools
    assert not plan.features.imaging
    assert "--no-imaging" in command
    assert "--no-materialx" in command
    assert "--no-usdview" in command
    assert "--no-prman" in command


def test_default_parallelism_is_conservatively_capped(tmp_path):
    pin, args = _args(tmp_path)
    plan = build_openusd.create_plan(args, pin)

    assert 1 <= plan.jobs <= 8


def test_usdview_profile_enables_python_imaging_and_materialx(tmp_path):
    pin, args = _args(tmp_path, "--profile", "usdview", "--embree")
    plan = build_openusd.create_plan(args, pin)
    command = build_openusd.upstream_command(plan)

    assert plan.features.python
    assert plan.features.usdview
    assert plan.features.materialx
    assert plan.features.embree
    assert "--usd-imaging" in command
    assert "--usdview" in command
    assert "--materialx" in command
    assert "--embree" in command


def test_renderman_implies_usdview_and_forwards_install_path(tmp_path):
    renderman = tmp_path / "RenderManProServer"
    pin, args = _args(tmp_path / "usd", "--renderman", str(renderman))
    plan = build_openusd.create_plan(args, pin)
    command = build_openusd.upstream_command(plan)

    assert plan.features.usdview
    assert plan.features.renderman == renderman.resolve()
    assert command[command.index("--prman-location") + 1] == str(renderman.resolve())


def test_renderman_rejects_explicitly_disabled_usdview(tmp_path):
    pin, args = _args(tmp_path, "--renderman", str(tmp_path / "rman"), "--no-usdview")

    with pytest.raises(ValueError, match="--no-usdview"):
        build_openusd.create_plan(args, pin)


def test_layout_overrides_are_forwarded_to_upstream(tmp_path):
    install = tmp_path / "custom-install"
    build = tmp_path / "custom-build"
    sources = tmp_path / "custom-sources"
    pin, args = _args(
        tmp_path / "root",
        "--install-dir",
        str(install),
        "--build-dir",
        str(build),
        "--dependency-source-dir",
        str(sources),
    )
    plan = build_openusd.create_plan(args, pin)
    command = build_openusd.upstream_command(plan)

    assert command[command.index("--build") + 1] == str(build.resolve())
    assert command[command.index("--src") + 1] == str(sources.resolve())
    assert command[-1] == str(install.resolve())


def test_dry_run_does_not_create_managed_directories(tmp_path, capsys):
    root = tmp_path / "managed"

    assert build_openusd.main(["--root", str(root), "--dry-run"]) == 0
    output = capsys.readouterr().out

    assert "git clone" in output
    assert "build_usd.py" in output
    assert OPENUSD_TAG in output
    assert not root.exists()


def test_python_build_registers_project_runtime(tmp_path):
    pin, args = _args(tmp_path / "managed")
    plan = build_openusd.create_plan(args, pin)
    python_path = plan.layout.install / "Lib" / "site-packages"
    (python_path / "pxr").mkdir(parents=True)
    (python_path / "pxr" / "__init__.py").touch()
    config_path = tmp_path / "project" / ".openusd" / "active.json"

    written = build_openusd.write_runtime_config(plan, config_path)

    assert written == config_path
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["usd_root"] == str(plan.layout.install)
    assert config["python_path"] == str(python_path)
    assert config["version"] == OPENUSD_VERSION


def test_build_without_python_is_not_registered(tmp_path):
    pin, args = _args(tmp_path / "managed", "--no-python")
    plan = build_openusd.create_plan(args, pin)
    config_path = tmp_path / "active.json"

    assert build_openusd.write_runtime_config(plan, config_path) is None
    assert not config_path.exists()
