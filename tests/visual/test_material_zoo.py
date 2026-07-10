"""Visual regression: replay the material_zoo reference log and render it.

Exercises the full receive pipeline (codec encode/decode -> apply_events) on a
scene of hard materials -- UsdPreviewSurface, MaterialX standard_surface (chrome,
tiled-brass, triplanar + UV image texturing), OpenPBR (translated to
standard_surface for hdPrman), and a referenced chess-piece asset -- then frames
a front camera, lights it with the StinsonBeach IBL, renders with RenderMan, and
FLIP-compares to a committed golden. Catches regressions anywhere in
codec/apply/material handling that change the rendered result.

Assets resolve from the usd-wg/assets submodule (Bishop, tiled-brass MaterialX)
plus a vendored UV sphere; the log stores them as portable {REPO} tokens so the
committed fixture carries no machine paths. The event log is a JSONL fixture of
semantic events (decoupled from the binary wire format / event-store schema),
re-encoded through the current codec at replay time. Regenerate the golden -- not
the fixture -- with --update-baselines.

Run:    uv run pytest tests/visual --visual-tests -v
Golden: uv run pytest tests/visual --visual-tests --update-baselines
"""

import os
from pathlib import Path

import pytest

from integrations.visualtest import scene
from integrations.visualtest.harness import check_against_baseline
from integrations.visualtest.replay import load_events, reconstruct

_HERE = os.path.dirname(__file__)
_ROOT = os.path.dirname(os.path.dirname(_HERE))
BASE = os.path.join(_ROOT, "test_scene.usda")
LOG = os.path.join(_HERE, "fixtures", "material_zoo.jsonl")
# Asset paths in the log are stored as portable {REPO} tokens (the chess piece +
# tiled-brass MaterialX come from the usd-wg/assets submodule, the UV sphere from
# tests/visual/assets/); expand to this checkout's root at replay time.
REPO_TOKEN = {"{REPO}": Path(_ROOT).as_posix()}
REFERENCES = os.path.join(_HERE, "references")
WIDTH = 1280
# IBL + MaterialX golden runs noisier than the flat-lit diffuse scenes, so the
# threshold is looser than the default. Tuned against the renderer noise floor.
REGRESSION_MEAN = 0.04


def test_material_zoo_matches_baseline(visual_artifacts_dir, update_baselines):
    # RenderMan is bootstrapped by the autouse _visual_env fixture, so apply_events
    # can resolve shader port types through the Sdr registry.
    stage = reconstruct(BASE, load_events(LOG, subst=REPO_TOKEN))
    camera = scene.frame_front_camera(stage)
    scene.apply_ibl_dome(stage)
    baseline = os.path.join(REFERENCES, "material_zoo.png")
    check = check_against_baseline(stage, camera, baseline, name="material_zoo",
                                   artifacts_dir=visual_artifacts_dir, width=WIDTH,
                                   update=update_baselines)
    if check.status == "generated":
        pytest.skip(f"baseline (re)generated: {check.baseline_path}")
    if check.status == "missing":
        pytest.skip(f"no baseline yet; run --update-baselines to create {baseline}")
    assert check.result.mean < REGRESSION_MEAN, (
        f"material_zoo visual regression: FLIP mean={check.result.mean:.5f} "
        f"p99={check.result.p99:.5f}; error map: {check.result.error_map_path}")
