"""Headless reference renderer for the visual-regression harness.

Renders a USD stage from a named camera to an image file via a Hydra delegate
(RenderMan RIS by default). CPU-only and GL-free so it runs headless in CI.
Renderers are pluggable via :mod:`integrations.visualtest.renderers` -- each
supplies its delegate id plus any environment setup and per-renderer material
conditioning (e.g. hdPrman's OpenPBR->standard_surface translation).

The render config here (sample budget, the chosen renderer) is canonical for
reference goldens; changing it invalidates committed baselines.
"""

from __future__ import annotations

import os

# Canonical RIS sample budget for reproducible goldens. Override only for fast
# local previews, never when regenerating committed baselines. Set before the
# first engine is constructed. Requires USD >= 0.26.5, where the Hydra scene
# index renders UsdGeomCamera prims correctly under RenderMan.
os.environ.setdefault("HD_PRMAN_MAX_SAMPLES", "128")

DEFAULT_RENDERER = "renderman"


def is_available(renderer: str = DEFAULT_RENDERER) -> bool:
    """True when ``renderer`` can produce a headless render in this environment."""
    from integrations.visualtest.renderers import get_renderer

    return get_renderer(renderer).is_available()


def render(stage, camera_path: str, out_path: str, *, width: int = 512,
           renderer: str = DEFAULT_RENDERER, condition: bool = True) -> str:
    """Render ``stage`` from ``camera_path`` to ``out_path`` (PNG); return the path.

    ``stage`` may be a ``Usd.Stage`` or a path string. Applies the renderer's
    setup (e.g. RenderMan's ``RMAN_*`` env) and, when ``condition`` is set, its
    material conditioning (e.g. OpenPBR->standard_surface for hdPrman). Raises if
    the renderer is unavailable or the render fails.
    """
    from pxr import Usd, UsdAppUtils, UsdGeom

    from integrations.visualtest.renderers import get_renderer

    r = get_renderer(renderer)
    if r.setup is not None and not r.setup():
        raise RuntimeError(f"renderer {r.name!r} unavailable (e.g. RenderMan not installed)")

    if isinstance(stage, str):
        stage = Usd.Stage.Open(stage)

    if condition and r.condition is not None:
        r.condition(stage)

    cam_prim = stage.GetPrimAtPath(camera_path)
    if not cam_prim or not cam_prim.IsValid():
        raise ValueError(f"camera prim not found: {camera_path}")

    rec = UsdAppUtils.FrameRecorder(r.plugin_id, False)  # gpuEnabled=False -> CPU, no GL
    rec.SetImageWidth(width)
    ok = rec.Record(stage, UsdGeom.Camera(cam_prim), Usd.TimeCode.Default(), out_path)
    del rec  # release the delegate between successive renders
    if not ok or not os.path.exists(out_path):
        raise RuntimeError(f"render failed: {camera_path} -> {out_path}")
    return out_path
