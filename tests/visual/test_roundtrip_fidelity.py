"""Mode-1: render a scene vs its openusdconnect round-trip and FLIP-compare.

The renderer-invariant fidelity test. Unlike the static baseline test, the
reference here is the scene *after* it has passed through the real emit -> wire
-> apply pipeline, so a regression in replication (a dropped attr, a mangled
value, lost colorspace/precision) shows up as a visible delta. No DCC required.
"""

import os

from integrations.visualtest import compare, render
from integrations.visualtest.roundtrip import roundtrip_stage

_HERE = os.path.dirname(__file__)
SCENE = os.path.join(_HERE, "scenes", "usdpreview_basic.usda")
CAMERA = "/World/_TestCam"
WIDTH = 256

# The round-trip should be near-lossless, so this sits just above the renderer
# noise floor (~0.003). A real replication gap pushes well past it.
ROUNDTRIP_MEAN = 0.02


def test_openusdconnect_roundtrip_preserves_render(visual_artifacts_dir):
    source, rebuilt, n_events = roundtrip_stage(SCENE)
    assert n_events > 0, "emitter snapshot produced no events"

    def _render(stage, name):
        return render.render(stage, CAMERA, os.path.join(visual_artifacts_dir, name), width=WIDTH)

    src_img = _render(source, "rt_source.png")
    rebuilt_img = _render(rebuilt, "rt_rebuilt.png")
    result = compare.compare(src_img, rebuilt_img,
                             error_map_path=os.path.join(visual_artifacts_dir, "rt.flip.png"))
    assert result.mean < ROUNDTRIP_MEAN, (
        f"openusdconnect round-trip altered the render: FLIP mean={result.mean:.5f} "
        f"p99={result.p99:.5f} over {n_events} events; error map: {result.error_map_path}")
