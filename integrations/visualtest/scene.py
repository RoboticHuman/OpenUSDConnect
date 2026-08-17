"""Camera framing and lighting helpers for visual-regression scenes."""

from __future__ import annotations

import math
from pathlib import Path

CAMERA_PATH = "/World/_TestCam"


def stinson_beach_hdr(install_root: str | Path | None = None) -> str:
    """usdview's default dome-light texture, resolved in the active USD install."""
    if install_root is None:
        from integrations.renderman import usd_install_root as active_usd_install_root

        install_root = active_usd_install_root()
    return str(Path(install_root) / "lib" / "usd" / "hdx" / "resources"
               / "textures" / "StinsonBeach.hdr")


def frame_front_camera(stage, root_path: str = "/World", *, focal: float = 35.0,
                       h_aperture: float = 36.0, v_aperture: float = 24.0,
                       margin: float = 1.25) -> str:
    """Author a front-facing camera at :data:`CAMERA_PATH` framed to the bounds.

    Looks down -Z (the conventional front view) from a distance that fits the
    world bound of ``root_path`` in the given lens. Returns the camera path.
    """
    from pxr import Gf, Usd, UsdGeom

    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(),
                             [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
    rng = cache.ComputeWorldBound(stage.GetPrimAtPath(root_path)).ComputeAlignedRange()
    center = (rng.GetMin() + rng.GetMax()) * 0.5
    size = rng.GetMax() - rng.GetMin()
    hfov = 2 * math.atan((h_aperture / 2) / focal)
    vfov = 2 * math.atan((v_aperture / 2) / focal)
    dist = max((size[0] / 2) / math.tan(hfov / 2), (size[1] / 2) / math.tan(vfov / 2))
    dist = dist * margin + size[2] / 2

    cam = UsdGeom.Camera.Define(stage, CAMERA_PATH)
    cam.CreateProjectionAttr(UsdGeom.Tokens.perspective)
    cam.CreateFocalLengthAttr(focal)
    cam.CreateHorizontalApertureAttr(h_aperture)
    cam.CreateVerticalApertureAttr(v_aperture)
    cam.CreateClippingRangeAttr(Gf.Vec2f(0.1, float(dist + size[2] + 100)))
    cam.AddTranslateOp().Set(Gf.Vec3d(center[0], center[1], center[2] + dist))
    return CAMERA_PATH


def apply_ibl_dome(stage, *, intensity: float = 1.0, texture: str | None = None):
    """Add a dome light with an HDR environment (StinsonBeach by default)."""
    from pxr import Sdf, UsdLux

    dome = UsdLux.DomeLight.Define(stage, "/World/_Dome")
    dome.CreateIntensityAttr(intensity)
    dome.CreateTextureFileAttr().Set(Sdf.AssetPath(texture or stinson_beach_hdr()))
    return dome
