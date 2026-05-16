"""Tests for the PrimChannel architecture in emitter.py.

Covers:
- needs_read gating fires on real notice payloads (not just synthetic hints)
- Mixed changes in one cycle: a primvar change plus a visibility/light-input
  change does not cause the channel to skip its read
- Cache-key uniqueness validated at NoticeEmitter construction
- extra_channels append to the built-ins; built-ins always run
"""

import pytest

try:
    from pxr import Sdf, Usd, UsdGeom, UsdLux  # noqa: F401

    PXR_AVAILABLE = True
except ImportError:
    PXR_AVAILABLE = False

pytestmark = pytest.mark.skipif(not PXR_AVAILABLE, reason="pxr not available")

from openusdconnect.emitter import (
    _BUILTIN_PRIM_CHANNELS,
    CameraAttrsChannel,
    ConnectableChannel,
    NoticeEmitter,
    PrimChannel,
    VisibilityChannel,
)
from openusdconnect.protocol_constants import (
    K_SET_CONNECTABLE_INPUT,
    K_SET_GPRIM_ATTRS,
    K_SET_VISIBILITY,
)


@pytest.fixture
def stage():
    return Usd.Stage.CreateInMemory()


class TestNoticeHandlerTracksAllAttrs:
    """The notice handler records every changed attr name in _dirty_attrs,
    even ones the gprim scan filters out — channels need them for gating."""

    def test_xform_change_appears_in_dirty_attrs(self, stage):
        mesh = stage.DefinePrim("/Mesh", "Mesh")
        UsdGeom.Xformable(mesh).AddTranslateOp().Set((0, 0, 0))
        em = NoticeEmitter(stage)
        mesh.GetAttribute("xformOp:translate").Set((1, 0, 0))
        assert "xformOp:translate" in em._dirty_attrs["/Mesh"]

    def test_connectable_input_change_appears_in_dirty_attrs(self, stage):
        p = stage.DefinePrim("/Light", "SphereLight")
        UsdLux.SphereLight(p).CreateIntensityAttr(1.0)
        em = NoticeEmitter(stage)
        UsdLux.SphereLight(p).GetIntensityAttr().Set(2.0)
        assert "inputs:intensity" in em._dirty_attrs["/Light"]

    def test_visibility_change_appears_in_dirty_attrs(self, stage):
        mesh = stage.DefinePrim("/Mesh", "Mesh")
        UsdGeom.Imageable(mesh).GetVisibilityAttr().Set("inherited")
        em = NoticeEmitter(stage)
        UsdGeom.Imageable(mesh).GetVisibilityAttr().Set("invisible")
        assert "visibility" in em._dirty_attrs["/Mesh"]


class TestGprimScanFilters:
    """The gprim scan filters dedicated-channel attrs at iteration time so
    xformOps / inputs:* / visibility never leak into set_gprim_attrs events."""

    def test_xformop_attr_does_not_leak_into_gprim_event(self, stage):
        mesh = stage.DefinePrim("/Mesh", "Mesh")
        UsdGeom.Xformable(mesh).AddTranslateOp().Set((0, 0, 0))
        em = NoticeEmitter(stage)
        mesh.GetAttribute("xformOp:translate").Set((1.0, 2.0, 3.0))
        events = em.build_events_for_dirty(include_matrices=False)
        gprim = [e for e in events if e["k"] == K_SET_GPRIM_ATTRS]
        for e in gprim:
            assert "xformOp:translate" not in e.get("attrs", {})

    def test_connectable_input_does_not_leak_into_gprim_event(self, stage):
        p = stage.DefinePrim("/Light", "SphereLight")
        UsdLux.SphereLight(p).CreateIntensityAttr(1.0)
        em = NoticeEmitter(stage)
        UsdLux.SphereLight(p).GetIntensityAttr().Set(2.0)
        events = em.build_events_for_dirty(include_matrices=False)
        gprim = [e for e in events if e["k"] == K_SET_GPRIM_ATTRS]
        for e in gprim:
            assert "inputs:intensity" not in e.get("attrs", {})


class TestNeedsReadGating:
    """needs_read short-circuits the read when the dirty hint contains nothing
    a channel watches. Verified on real notice-driven dirty_attrs payloads."""

    def test_connectable_skips_when_only_transform_changed(self, stage):
        p = stage.DefinePrim("/Light", "SphereLight")
        UsdLux.SphereLight(p).CreateIntensityAttr(1.0)
        # Pretend the notice handler logged a transform-only change.
        ch = ConnectableChannel()
        assert ch.needs_read({"xformOp:translate"}) is False

    def test_connectable_reads_when_input_changed(self):
        ch = ConnectableChannel()
        assert ch.needs_read({"inputs:intensity"}) is True

    def test_connectable_reads_on_no_hint(self):
        ch = ConnectableChannel()
        assert ch.needs_read(None) is True
        assert ch.needs_read(set()) is True

    def test_visibility_skips_when_only_transform_changed(self):
        assert VisibilityChannel().needs_read({"xformOp:translate"}) is False

    def test_visibility_reads_when_visibility_changed(self):
        assert VisibilityChannel().needs_read({"visibility"}) is True

    def test_camera_reads_when_focal_length_changed(self):
        assert CameraAttrsChannel().needs_read({"focalLength"}) is True

    def test_camera_skips_when_only_gprim_attr_changed(self):
        assert CameraAttrsChannel().needs_read({"primvars:displayColor"}) is False


class TestMixedChangesPreserveEmissions:
    """The original gating bug: a primvar change in the same cycle as a
    connectable input / visibility change must not cause those events to be
    dropped. Verifies that with real notice-driven dirty_attrs (which now
    contains the channel-relevant names), both events emit."""

    def test_visibility_emits_alongside_primvar_change(self, stage):
        mesh = stage.DefinePrim("/Mesh", "Mesh")
        UsdGeom.Imageable(mesh).GetVisibilityAttr().Set("inherited")
        mesh.CreateAttribute("primvars:foo", Sdf.ValueTypeNames.FloatArray).Set([1.0, 2.0])
        em = NoticeEmitter(stage)
        # Both change in one cycle.
        UsdGeom.Imageable(mesh).GetVisibilityAttr().Set("invisible")
        mesh.GetAttribute("primvars:foo").Set([3.0, 4.0])

        events = em.build_events_for_dirty(include_matrices=False)
        vis = [e for e in events if e["k"] == K_SET_VISIBILITY]
        gprim = [e for e in events if e["k"] == K_SET_GPRIM_ATTRS]
        assert vis, "visibility event missing alongside primvar change"
        assert gprim, "gprim event missing"
        assert vis[0]["visible"] is False

    def test_light_intensity_emits_alongside_primvar_change(self, stage):
        # MeshLightAPI on a Mesh — exactly the kind of prim where the
        # original bug bit: it carries both light inputs and primvars.
        mesh = stage.DefinePrim("/MeshLight", "Mesh")
        UsdLux.MeshLightAPI.Apply(mesh)
        intensity = mesh.CreateAttribute("inputs:intensity", Sdf.ValueTypeNames.Float)
        intensity.Set(1.0)
        primvar = mesh.CreateAttribute("primvars:foo", Sdf.ValueTypeNames.FloatArray)
        primvar.Set([1.0])
        em = NoticeEmitter(stage)

        intensity.Set(2.0)
        primvar.Set([2.0])

        events = em.build_events_for_dirty(include_matrices=False)
        sci = [e for e in events if e["k"] == K_SET_CONNECTABLE_INPUT and e["prim"] == "/MeshLight"]
        gprim = [e for e in events if e["k"] == K_SET_GPRIM_ATTRS and e["prim"] == "/MeshLight"]
        assert sci, "connectable input event missing alongside primvar change"
        assert sci[0]["inputs"].get("intensity") == pytest.approx(2.0)
        assert gprim, "gprim event missing"


class TestFirstEncounterReadsAllChannels:
    """First encounter is a full-state sync, even if the triggering notice
    only names an unrelated attr such as xformOp:translate."""

    def test_camera_attrs_emit_when_first_notice_is_transform_only(self, stage):
        p = stage.DefinePrim("/Cam", "Camera")
        cam = UsdGeom.Camera(p)
        cam.CreateFocalLengthAttr(35.0)
        UsdGeom.Xformable(p).AddTranslateOp().Set((0, 0, 0))

        em = NoticeEmitter(stage)
        p.GetAttribute("xformOp:translate").Set((1, 0, 0))

        events = em.build_events_for_dirty(include_matrices=False)
        gprim = [e for e in events if e["k"] == K_SET_GPRIM_ATTRS and e["prim"] == "/Cam"]
        assert gprim
        assert gprim[0]["attrs"]["focalLength"] == pytest.approx(35.0)

    def test_connectable_inputs_emit_when_first_notice_is_transform_only(self, stage):
        p = stage.DefinePrim("/Light", "SphereLight")
        UsdLux.SphereLight(p).CreateIntensityAttr(42.0)
        UsdGeom.Xformable(p).AddTranslateOp().Set((0, 0, 0))

        em = NoticeEmitter(stage)
        p.GetAttribute("xformOp:translate").Set((1, 0, 0))

        events = em.build_events_for_dirty(include_matrices=False)
        inputs = [
            e for e in events
            if e["k"] == K_SET_CONNECTABLE_INPUT and e["prim"] == "/Light"
        ]
        assert inputs
        assert inputs[0]["inputs"]["intensity"] == pytest.approx(42.0)


class TestMaterialBindingRebind:
    """Subsequent material rebinds fire info-only on ``material:binding``
    only, not resyncs. MaterialBindingChannel must catch them via its
    watched_attrs declaration."""

    def test_rebind_to_different_material_replicates(self, stage):
        mesh = stage.DefinePrim("/Mesh", "Mesh")
        mat_a = stage.DefinePrim("/MatA", "Material")
        mat_b = stage.DefinePrim("/MatB", "Material")
        from pxr import UsdShade

        # First bind — fires resync + info-only (Apply is structural).
        UsdShade.MaterialBindingAPI.Apply(mesh).Bind(UsdShade.Material(mat_a))

        em = NoticeEmitter(stage)
        em.build_events_for_dirty(include_matrices=False)  # consume initial

        # Rebind to a different material — fires info-only only.
        UsdShade.MaterialBindingAPI(mesh).Bind(UsdShade.Material(mat_b))

        events = em.build_events_for_dirty(include_matrices=False)
        binding_events = [
            e for e in events
            if e["k"] == "set_material_binding" and e["prim"] == "/Mesh"
        ]
        assert binding_events, (
            "Material rebind must replicate even though USD fires info-only "
            "(not resync) for subsequent rebinds"
        )
        assert binding_events[0]["material_path"] == "/MatB"


class TestGateMutualExclusion:
    """A channel can opt into resync-only gating OR named-attr gating,
    not both — the validation should reject the combination."""

    def test_both_resync_and_watched_attrs_rejected(self, stage):
        class BothChannel(PrimChannel):
            cache_key = "both_channel"
            reads_on_resync_only = True
            watched_attrs = ("foo",)

        with pytest.raises(ValueError, match="reads_on_resync_only"):
            NoticeEmitter(stage, extra_channels=[BothChannel()])

    def test_both_resync_and_watched_prefixes_rejected(self, stage):
        class BothChannel(PrimChannel):
            cache_key = "both_prefix_channel"
            reads_on_resync_only = True
            watched_prefixes = ("foo:",)

        with pytest.raises(ValueError, match="reads_on_resync_only"):
            NoticeEmitter(stage, extra_channels=[BothChannel()])


class TestDefaultIsAlwaysRead:
    """A channel that declares neither gate must always read on dirty
    prims — the safe default for integrators who don't know what USD
    notice path their state changes arrive on."""

    def test_no_declarations_means_always_read(self):
        class PlainChannel(PrimChannel):
            cache_key = "plain"

        ch = PlainChannel()
        assert ch.needs_read({"unrelated:attr"}) is True
        assert ch.needs_read(None) is True
        assert ch.needs_read(set()) is True


class TestExtraChannels:
    """extra_channels= appends to the built-ins; the built-ins stay active."""

    def test_default_channels_are_the_builtins(self, stage):
        em = NoticeEmitter(stage)
        assert em._channels == _BUILTIN_PRIM_CHANNELS

    def test_extra_channels_appended_after_builtins(self, stage):
        class MyChannel(PrimChannel):
            cache_key = "my_custom_thing"

            def read(self, stage, prim_path):
                return None

            def to_event(self, prim_path, diff):
                return None

        extra = MyChannel()
        em = NoticeEmitter(stage, extra_channels=[extra])
        assert em._channels[:-1] == _BUILTIN_PRIM_CHANNELS
        assert em._channels[-1] is extra

    def test_extra_channels_with_duplicate_cache_key_rejected(self, stage):
        class ClashChannel(PrimChannel):
            cache_key = "trs"  # collides with _C_TRS

        with pytest.raises(ValueError, match="Duplicate PrimChannel cache_key"):
            NoticeEmitter(stage, extra_channels=[ClashChannel()])

    def test_extra_channels_must_be_prim_channel_instances(self, stage):
        with pytest.raises(TypeError, match="PrimChannel instances"):
            NoticeEmitter(stage, extra_channels=["not a channel"])

    def test_extra_channels_with_same_key_as_each_other_rejected(self, stage):
        class A(PrimChannel):
            cache_key = "same"

        class B(PrimChannel):
            cache_key = "same"

        with pytest.raises(ValueError, match="Duplicate"):
            NoticeEmitter(stage, extra_channels=[A(), B()])
