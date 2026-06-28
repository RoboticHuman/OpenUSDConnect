"""Visual-regression tier: instrument self-checks + reference baseline compare.

Run:    uv run pytest tests/visual --visual-tests -v
Golden: uv run pytest tests/visual --visual-tests --update-baselines
"""

import os

import pytest
from pxr import Gf, Usd

from integrations.visualtest import compare, render
from integrations.visualtest.harness import check_against_baseline

_HERE = os.path.dirname(__file__)
SCENE = os.path.join(_HERE, "scenes", "usdpreview_basic.usda")
REFERENCES = os.path.join(_HERE, "references")
CAMERA = "/World/_TestCam"
WIDTH = 256

# RIS sampling leaves a small noise floor between identical renders (~0.003 at
# this sample budget). Thresholds sit above that floor but well below any real
# change (a bare material-color swap measures ~0.09).
DETERMINISM_FLOOR = 0.01
REGRESSION_MEAN = 0.02


def _render(stage, name, artifacts_dir):
    return render.render(stage, CAMERA, os.path.join(artifacts_dir, name), width=WIDTH)


def test_instrument_is_deterministic(visual_artifacts_dir):
    """Two identical renders must FLIP-compare near zero, or goldens can't be trusted."""
    a = _render(SCENE, "det_a.png", visual_artifacts_dir)
    b = _render(SCENE, "det_b.png", visual_artifacts_dir)
    result = compare.compare(a, b)
    assert result.mean < DETERMINISM_FLOOR, (
        f"reference renderer not deterministic: FLIP mean={result.mean:.5f}")


def test_flip_discriminates_material_change(visual_artifacts_dir):
    """Guard the guard: a material change must register a clear FLIP delta."""
    ref = _render(SCENE, "disc_ref.png", visual_artifacts_dir)
    stage = Usd.Stage.Open(SCENE)
    stage.GetPrimAtPath("/World/Materials/RedPreview/Surface").GetAttribute(
        "inputs:diffuseColor").Set(Gf.Vec3f(0.1, 0.7, 0.2))
    changed = _render(stage, "disc_changed.png", visual_artifacts_dir)
    result = compare.compare(ref, changed)
    assert result.mean > REGRESSION_MEAN, (
        f"FLIP failed to register a material change: mean={result.mean:.5f}")


def test_usdpreview_basic_matches_baseline(visual_artifacts_dir, update_baselines):
    baseline = os.path.join(REFERENCES, "usdpreview_basic.png")
    check = check_against_baseline(
        Usd.Stage.Open(SCENE), CAMERA, baseline, name="usdpreview_basic",
        artifacts_dir=visual_artifacts_dir, width=WIDTH, update=update_baselines)
    if check.status == "generated":
        pytest.skip(f"baseline (re)generated: {check.baseline_path}")
    if check.status == "missing":
        pytest.skip(f"no baseline yet; run --update-baselines to create {baseline}")
    assert check.result.mean < REGRESSION_MEAN, (
        f"visual regression: FLIP mean={check.result.mean:.5f} p99={check.result.p99:.5f}; "
        f"error map: {check.result.error_map_path}")
