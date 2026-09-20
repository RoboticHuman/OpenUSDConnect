"""Tests for UsdGeomCamera replication.

Cameras are a typed UsdGeom schema with a fixed set of float / token / float2
attributes. They flow through the same generic event machinery used by
meshes: ``ensure_prim`` for the typed prim, ``set_gprim_attrs`` for the
attribute values, and ``set_xform_trs`` for the transform. No
camera-specific event kind is introduced.
"""

import pathlib

import pytest

try:
    from pxr import Gf, Usd, UsdGeom  # noqa: F401

    PXR_AVAILABLE = True
except ImportError:
    PXR_AVAILABLE = False

pytestmark = pytest.mark.skipif(not PXR_AVAILABLE, reason="pxr not available")

from openusdconnect.adapters import MockAdapter
from openusdconnect.emitter import NoticeEmitter
from openusdconnect.event_apply import apply_event, apply_events
from openusdconnect.protocol_constants import (
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_SET_GPRIM_ATTRS,
    K_SET_XFORM_TRS,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
STANDARD_SHADER_BALL_CAMERA = (
    _REPO_ROOT / "assets/full_assets/StandardShaderBall/layers/camera.usda"
)


@pytest.fixture
def stage():
    return Usd.Stage.CreateInMemory()


class TestEnsurePrimCamera:
    def test_ensure_prim_creates_camera(self, stage):
        apply_event(stage, {"k": K_ENSURE_PRIM, "prim": "/Cam", "typeName": "Camera"})
        p = stage.GetPrimAtPath("/Cam")
        assert p.IsValid()
        assert p.GetTypeName() == "Camera"
        assert p.IsA(UsdGeom.Camera)

    def test_camera_is_xformable(self, stage):
        apply_event(stage, {"k": K_ENSURE_PRIM, "prim": "/Cam", "typeName": "Camera"})
        p = stage.GetPrimAtPath("/Cam")
        assert UsdGeom.Xformable(p)


class TestSetCameraAttrs:
    """Each core UsdGeomCamera attribute must round-trip through set_gprim_attrs."""

    def _make_camera(self, stage):
        apply_event(stage, {"k": K_ENSURE_PRIM, "prim": "/Cam", "typeName": "Camera"})
        return UsdGeom.Camera(stage.GetPrimAtPath("/Cam"))

    def test_focal_length(self, stage):
        cam = self._make_camera(stage)
        apply_event(
            stage,
            {"k": K_SET_GPRIM_ATTRS, "prim": "/Cam", "attrs": {"focalLength": 50.0}},
        )
        assert cam.GetFocalLengthAttr().Get() == pytest.approx(50.0)

    def test_horizontal_and_vertical_aperture(self, stage):
        cam = self._make_camera(stage)
        apply_event(
            stage,
            {
                "k": K_SET_GPRIM_ATTRS,
                "prim": "/Cam",
                "attrs": {"horizontalAperture": 20.955, "verticalAperture": 15.2908},
            },
        )
        assert cam.GetHorizontalApertureAttr().Get() == pytest.approx(20.955)
        assert cam.GetVerticalApertureAttr().Get() == pytest.approx(15.2908)

    def test_aperture_offsets(self, stage):
        cam = self._make_camera(stage)
        apply_event(
            stage,
            {
                "k": K_SET_GPRIM_ATTRS,
                "prim": "/Cam",
                "attrs": {
                    "horizontalApertureOffset": 0.25,
                    "verticalApertureOffset": -0.5,
                },
            },
        )
        assert cam.GetHorizontalApertureOffsetAttr().Get() == pytest.approx(0.25)
        assert cam.GetVerticalApertureOffsetAttr().Get() == pytest.approx(-0.5)

    def test_clipping_range(self, stage):
        cam = self._make_camera(stage)
        apply_event(
            stage,
            {
                "k": K_SET_GPRIM_ATTRS,
                "prim": "/Cam",
                "attrs": {"clippingRange": [0.1, 1000.0]},
            },
        )
        cr = cam.GetClippingRangeAttr().Get()
        assert isinstance(cr, Gf.Vec2f)
        assert cr[0] == pytest.approx(0.1)
        assert cr[1] == pytest.approx(1000.0)

    def test_focus_distance_and_fstop(self, stage):
        cam = self._make_camera(stage)
        apply_event(
            stage,
            {
                "k": K_SET_GPRIM_ATTRS,
                "prim": "/Cam",
                "attrs": {"focusDistance": 3.5, "fStop": 2.8},
            },
        )
        assert cam.GetFocusDistanceAttr().Get() == pytest.approx(3.5)
        assert cam.GetFStopAttr().Get() == pytest.approx(2.8)

    def test_exposure(self, stage):
        cam = self._make_camera(stage)
        apply_event(
            stage,
            {"k": K_SET_GPRIM_ATTRS, "prim": "/Cam", "attrs": {"exposure": 1.5}},
        )
        assert cam.GetExposureAttr().Get() == pytest.approx(1.5)

    @pytest.mark.parametrize("projection", ["perspective", "orthographic"])
    def test_projection_token(self, stage, projection):
        cam = self._make_camera(stage)
        apply_event(
            stage,
            {"k": K_SET_GPRIM_ATTRS, "prim": "/Cam", "attrs": {"projection": projection}},
        )
        assert cam.GetProjectionAttr().Get() == projection

    def test_all_core_attrs_together(self, stage):
        """One event carrying every core attr the typical first-emit payload."""
        cam = self._make_camera(stage)
        apply_event(
            stage,
            {
                "k": K_SET_GPRIM_ATTRS,
                "prim": "/Cam",
                "attrs": {
                    "projection": "perspective",
                    "focalLength": 35.0,
                    "horizontalAperture": 36.0,
                    "verticalAperture": 24.0,
                    "horizontalApertureOffset": 0.0,
                    "verticalApertureOffset": 0.0,
                    "clippingRange": [0.01, 10000.0],
                    "focusDistance": 5.0,
                    "fStop": 4.0,
                    "exposure": 0.0,
                },
            },
        )
        assert cam.GetProjectionAttr().Get() == "perspective"
        assert cam.GetFocalLengthAttr().Get() == pytest.approx(35.0)
        assert cam.GetHorizontalApertureAttr().Get() == pytest.approx(36.0)
        assert cam.GetVerticalApertureAttr().Get() == pytest.approx(24.0)
        cr = cam.GetClippingRangeAttr().Get()
        assert cr[0] == pytest.approx(0.01)
        assert cr[1] == pytest.approx(10000.0)
        assert cam.GetFocusDistanceAttr().Get() == pytest.approx(5.0)
        assert cam.GetFStopAttr().Get() == pytest.approx(4.0)


class TestCameraXform:
    """Cameras are UsdGeom.Xformable the existing TRS path must work on them."""

    def test_ensure_xform_ops_on_camera(self, stage):
        apply_events(
            stage,
            [
                {"k": K_ENSURE_PRIM, "prim": "/Cam", "typeName": "Camera"},
                {"k": K_ENSURE_XFORM_OPS, "prim": "/Cam"},
            ],
        )
        xf = UsdGeom.Xformable(stage.GetPrimAtPath("/Cam"))
        op_names = [op.GetAttr().GetName() for op in xf.GetOrderedXformOps()]
        assert "xformOp:translate" in op_names
        assert "xformOp:orient" in op_names
        assert "xformOp:scale" in op_names

    def test_set_xform_trs_on_camera(self, stage):
        apply_events(
            stage,
            [
                {"k": K_ENSURE_PRIM, "prim": "/Cam", "typeName": "Camera"},
                {"k": K_ENSURE_XFORM_OPS, "prim": "/Cam"},
                {
                    "k": K_SET_XFORM_TRS,
                    "prim": "/Cam",
                    "fields": ["t", "r", "s"],
                    "t": [10.0, 20.0, 30.0],
                    "r": [1.0, 0.0, 0.0, 0.0],
                    "s": [1.0, 1.0, 1.0],
                },
            ],
        )
        xf = UsdGeom.Xformable(stage.GetPrimAtPath("/Cam"))
        world = xf.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        t = world.ExtractTranslation()
        assert t[0] == pytest.approx(10.0)
        assert t[1] == pytest.approx(20.0)
        assert t[2] == pytest.approx(30.0)


class TestEmitterCameraDiff:
    """Authoring camera attrs must produce set_gprim_attrs events via generic attribute capture."""

    def test_emits_focal_length_change(self, stage):
        em = NoticeEmitter(stage)
        p = stage.DefinePrim("/Cam", "Camera")
        UsdGeom.Camera(p).CreateFocalLengthAttr(50.0)

        events = em.build_events_for_dirty()
        ensure = [e for e in events if e["k"] == K_ENSURE_PRIM and e["prim"] == "/Cam"]
        gprim = [e for e in events if e["k"] == K_SET_GPRIM_ATTRS and e["prim"] == "/Cam"]
        assert ensure
        assert ensure[0]["typeName"] == "Camera"
        assert gprim
        assert gprim[0]["attrs"]["focalLength"] == pytest.approx(50.0)

    def test_emits_multiple_attrs_together(self, stage):
        em = NoticeEmitter(stage)
        p = stage.DefinePrim("/Cam", "Camera")
        cam = UsdGeom.Camera(p)
        cam.CreateFocalLengthAttr(35.0)
        cam.CreateHorizontalApertureAttr(36.0)
        cam.CreateClippingRangeAttr(Gf.Vec2f(0.1, 1000.0))
        cam.CreateProjectionAttr("perspective")

        events = em.build_events_for_dirty()
        gprim = [e for e in events if e["k"] == K_SET_GPRIM_ATTRS and e["prim"] == "/Cam"]
        assert gprim
        attrs = gprim[0]["attrs"]
        assert attrs["focalLength"] == pytest.approx(35.0)
        assert attrs["horizontalAperture"] == pytest.approx(36.0)
        # clippingRange round-trips as a list of two floats via usd_value_to_python
        assert list(attrs["clippingRange"]) == [pytest.approx(0.1), pytest.approx(1000.0)]
        assert attrs["projection"] == "perspective"

    def test_second_emit_is_empty_when_unchanged(self, stage):
        em = NoticeEmitter(stage)
        p = stage.DefinePrim("/Cam", "Camera")
        UsdGeom.Camera(p).CreateFocalLengthAttr(50.0)
        em.build_events_for_dirty()  # consume first cycle
        # No mutation between cycles → no events.
        events = em.build_events_for_dirty()
        gprim = [e for e in events if e["k"] == K_SET_GPRIM_ATTRS and e["prim"] == "/Cam"]
        assert not gprim

    def test_incremental_change_emits_only_changed_attr(self, stage):
        em = NoticeEmitter(stage)
        p = stage.DefinePrim("/Cam", "Camera")
        cam = UsdGeom.Camera(p)
        cam.CreateFocalLengthAttr(50.0)
        cam.CreateHorizontalApertureAttr(36.0)
        em.build_events_for_dirty()  # consume first cycle

        # Change only focal length.
        cam.GetFocalLengthAttr().Set(85.0)
        events = em.build_events_for_dirty()
        gprim = [e for e in events if e["k"] == K_SET_GPRIM_ATTRS and e["prim"] == "/Cam"]
        assert gprim
        attrs = gprim[0]["attrs"]
        assert attrs == {"focalLength": pytest.approx(85.0)}

    def test_transform_first_encounter_includes_authored_camera_values(self, stage):
        cam = UsdGeom.Camera.Define(stage, "/Cam")
        cam.CreateFocalLengthAttr(35.0)
        cam.CreateHorizontalApertureAttr(36.0)
        translate = cam.AddTranslateOp()
        em = NoticeEmitter(stage)
        try:
            translate.Set((1, 2, 3))
            events = em.build_events_for_dirty()
            attrs = {
                name: value
                for event in events
                if event["k"] == K_SET_GPRIM_ATTRS and event.get("time") is None
                for name, value in event["attrs"].items()
            }
            assert attrs["focalLength"] == pytest.approx(35.0)
            assert attrs["horizontalAperture"] == pytest.approx(36.0)
            translate.Set((4, 5, 6))
            assert not any(event["k"] == K_SET_GPRIM_ATTRS for event in em.build_events_for_dirty())
        finally:
            em.cleanup()

    def test_camera_samples_roundtrip_and_clear(self, stage):
        cam = UsdGeom.Camera.Define(stage, "/Cam")
        focal = cam.CreateFocalLengthAttr(35.0)
        focal.Set(50.0, 1.0)
        focal.Set(85.0, 2.0)
        destination = Usd.Stage.CreateInMemory()
        em = NoticeEmitter(stage)
        try:
            apply_events(destination, em.snapshot_events())
            received = UsdGeom.Camera(destination.GetPrimAtPath("/Cam")).GetFocalLengthAttr()
            assert received.Get() == pytest.approx(35.0)
            assert received.GetTimeSamples() == [1.0, 2.0]
            assert received.Get(2.0) == pytest.approx(85.0)
            focal.ClearAtTime(2.0)
            apply_events(destination, em.build_events_for_dirty())
            assert received.GetTimeSamples() == [1.0]
            focal.ClearDefault()
            apply_events(destination, em.build_events_for_dirty())
            assert (
                not destination.GetRootLayer()
                .GetAttributeAtPath("/Cam.focalLength")
                .HasDefaultValue()
            )
        finally:
            em.cleanup()

    @pytest.mark.parametrize("type_name,attribute", [("Camera", "focalLength"), ("Cube", "size")])
    def test_schema_default_clear_before_first_snapshot(self, stage, type_name, attribute):
        prim = stage.DefinePrim("/P", type_name)
        attr = prim.GetAttribute(attribute)
        attr.Set(35.0)
        destination = Usd.Stage.CreateInMemory()
        destination.GetRootLayer().TransferContent(stage.GetRootLayer())
        em = NoticeEmitter(stage)
        try:
            attr.ClearDefault()
            apply_events(destination, em.build_events_for_dirty())
            spec = destination.GetRootLayer().GetAttributeAtPath(f"/P.{attribute}")
            assert not spec.HasDefaultValue()
        finally:
            em.cleanup()

    def test_seed_uses_variant_opinion_even_when_same_layer_masks_it(self, stage):
        cam = UsdGeom.Camera.Define(stage, "/Cam")
        cam.CreateFocalLengthAttr(50.0)
        variants = cam.GetPrim().GetVariantSets().AddVariantSet("lens")
        variants.AddVariant("wide")
        variants.SetVariantSelection("wide")
        stage.SetEditTarget(variants.GetVariantEditTarget())
        cam.GetFocalLengthAttr().Set(35.0)
        em = NoticeEmitter(stage)
        try:
            em.seed_prim_cache(stage, "/Cam")
            cam.GetFocalLengthAttr().Set(50.0)
            assert any(
                event["k"] == K_SET_GPRIM_ATTRS and event["attrs"].get("focalLength") == 50.0
                for event in em.build_events_for_dirty()
            )
        finally:
            em.cleanup()


class TestRoundtripEmitterToApplier:
    """End-to-end: source stage mutation → emitter event → fresh stage application."""

    def test_lens_and_transform_edits_round_trip(self, stage):
        camera = UsdGeom.Camera.Define(stage, "/Cam")
        focal_length = camera.CreateFocalLengthAttr(35.0)
        translate = camera.AddTranslateOp()
        translate.Set((1, 2, 3))
        emitter = NoticeEmitter(stage)
        destination = Usd.Stage.CreateInMemory()
        try:
            # A lens edit on first encounter must still include the transform.
            focal_length.Set(50.0)
            apply_events(destination, emitter.build_events_for_dirty())
            replica = UsdGeom.Camera(destination.GetPrimAtPath("/Cam"))
            assert replica.GetLocalTransformation() == camera.GetLocalTransformation()

            focal_length.Set(65.0)
            events = emitter.build_events_for_dirty()
            assert not any(event["k"] == "set_xform_trs" for event in events)
            apply_events(destination, events)
            assert replica.GetFocalLengthAttr().Get() == 65.0
            assert replica.GetLocalTransformation() == camera.GetLocalTransformation()

            # Mixed notices must retain both changes in the same build cycle.
            focal_length.Set(85.0)
            translate.Set((4, 5, 6))
            apply_events(destination, emitter.build_events_for_dirty())
            assert replica.GetFocalLengthAttr().Get() == 85.0
            assert replica.GetLocalTransformation() == camera.GetLocalTransformation()
        finally:
            emitter.cleanup()

    def test_full_camera_round_trip(self, stage):
        em = NoticeEmitter(stage)
        p = stage.DefinePrim("/Cam", "Camera")
        cam = UsdGeom.Camera(p)
        cam.CreateProjectionAttr("perspective")
        cam.CreateFocalLengthAttr(50.0)
        cam.CreateHorizontalApertureAttr(36.0)
        cam.CreateVerticalApertureAttr(24.0)
        cam.CreateClippingRangeAttr(Gf.Vec2f(0.1, 1000.0))
        cam.CreateFStopAttr(2.8)
        cam.CreateFocusDistanceAttr(3.5)

        events = em.build_events_for_dirty()

        dst = Usd.Stage.CreateInMemory()
        apply_events(dst, events)
        dst_cam = UsdGeom.Camera(dst.GetPrimAtPath("/Cam"))
        assert dst_cam.GetProjectionAttr().Get() == "perspective"
        assert dst_cam.GetFocalLengthAttr().Get() == pytest.approx(50.0)
        assert dst_cam.GetHorizontalApertureAttr().Get() == pytest.approx(36.0)
        assert dst_cam.GetVerticalApertureAttr().Get() == pytest.approx(24.0)
        dst_cr = dst_cam.GetClippingRangeAttr().Get()
        assert dst_cr[0] == pytest.approx(0.1)
        assert dst_cr[1] == pytest.approx(1000.0)
        assert dst_cam.GetFStopAttr().Get() == pytest.approx(2.8)
        assert dst_cam.GetFocusDistanceAttr().Get() == pytest.approx(3.5)


class TestNumpyArraysWireDecode:
    """Server-side wire decode uses numpy_arrays=True for the FloatArray fast
    path. Scalar Gf vector constructors (Vec2f/Vec3f/Vec3d) refuse numpy
    scalar dtypes, so the apply pipeline must convert before constructing.
    """

    def _round_trip(self, ev):
        from openusdconnect.codec import encode_message, message_to_dict

        raw = encode_message({"type": "event", "seq": 1, "event": ev})
        return message_to_dict(raw, numpy_arrays=True)["event"]

    def test_clipping_range_via_numpy_decode(self, stage):
        from openusdconnect.protocol_constants import K_ENSURE_PRIM, K_SET_GPRIM_ATTRS

        ev_create = {"k": K_ENSURE_PRIM, "prim": "/Cam", "typeName": "Camera"}
        ev_attrs = {
            "k": K_SET_GPRIM_ATTRS,
            "prim": "/Cam",
            "attrs": {"clippingRange": [0.1, 1000.0]},
        }
        apply_events(stage, [self._round_trip(ev_create), self._round_trip(ev_attrs)])
        cr = UsdGeom.Camera(stage.GetPrimAtPath("/Cam")).GetClippingRangeAttr().Get()
        assert isinstance(cr, Gf.Vec2f)
        assert cr[0] == pytest.approx(0.1)
        assert cr[1] == pytest.approx(1000.0)


class TestMockAdapterCamera:
    def test_records_camera_ensure_and_attrs(self):
        adapter = MockAdapter()
        adapter.apply_event({"k": K_ENSURE_PRIM, "prim": "/Cam", "typeName": "Camera"})
        adapter.apply_event(
            {
                "k": K_SET_GPRIM_ATTRS,
                "prim": "/Cam",
                "attrs": {"focalLength": 50.0, "projection": "perspective"},
            },
        )
        stored = adapter.get_prim("/Cam")
        assert stored["typeName"] == "Camera"
        assert stored["gprim_attrs"]["focalLength"] == 50.0
        assert stored["gprim_attrs"]["projection"] == "perspective"


@pytest.mark.skipif(
    not STANDARD_SHADER_BALL_CAMERA.is_file(),
    reason="USD WG assets submodule not present",
)
class TestRealCameraAsset:
    """E2E using the StandardShaderBall's camera layer a real authored
    UsdGeomCamera with focalLength, horizontalAperture, verticalAperture,
    clippingRange, and projection.
    """

    CAM_PATH = "/standard_shader_ball_scene/camera"

    def _open_src(self):
        return Usd.Stage.Open(str(STANDARD_SHADER_BALL_CAMERA))

    def test_asset_has_camera(self):
        s = self._open_src()
        p = s.GetPrimAtPath(self.CAM_PATH)
        assert p.IsValid()
        assert p.IsA(UsdGeom.Camera)

    def test_camera_attrs_replicate(self):
        src = self._open_src()
        em = NoticeEmitter(src)

        # Drive a notice by bumping focal length.
        cam = UsdGeom.Camera(src.GetPrimAtPath(self.CAM_PATH))
        cam.GetFocalLengthAttr().Set(85.0)

        events = em.build_events_for_dirty()

        ensure = [e for e in events if e["k"] == K_ENSURE_PRIM and e["prim"] == self.CAM_PATH]
        gprim = [e for e in events if e["k"] == K_SET_GPRIM_ATTRS and e["prim"] == self.CAM_PATH]
        assert ensure
        assert ensure[0]["typeName"] == "Camera"
        assert gprim
        attrs = gprim[0]["attrs"]
        # First-encounter emit carries every authored camera attr alongside the change.
        assert attrs["focalLength"] == pytest.approx(85.0)
        assert attrs["horizontalAperture"] == pytest.approx(20.955)
        assert attrs["verticalAperture"] == pytest.approx(20.955)
        assert list(attrs["clippingRange"]) == [pytest.approx(0.1), pytest.approx(1000.0)]
        assert attrs["projection"] == "perspective"

        # Replicate to a fresh destination stage and verify.
        dst = Usd.Stage.CreateInMemory()
        apply_events(dst, events)
        dst_cam = UsdGeom.Camera(dst.GetPrimAtPath(self.CAM_PATH))
        assert dst_cam.GetFocalLengthAttr().Get() == pytest.approx(85.0)
        assert dst_cam.GetHorizontalApertureAttr().Get() == pytest.approx(20.955)
        dst_cr = dst_cam.GetClippingRangeAttr().Get()
        assert dst_cr[0] == pytest.approx(0.1)
        assert dst_cr[1] == pytest.approx(1000.0)
        assert dst_cam.GetProjectionAttr().Get() == "perspective"
