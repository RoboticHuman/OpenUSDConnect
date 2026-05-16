"""Tests for UsdLux light replication.

Lights are UsdShade.ConnectableAPI containers — their parameters are
``inputs:*`` attributes routed through the renamed ``set_connectable_input``
event. The typed schema (SphereLight, RectLight, …) is carried by
``ensure_prim``; ShapingAPI / ShadowAPI are applied via the new ``api_schemas``
field.
"""

import pathlib

import pytest

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdShade  # noqa: F401

    PXR_AVAILABLE = True
except ImportError:
    PXR_AVAILABLE = False

pytestmark = pytest.mark.skipif(not PXR_AVAILABLE, reason="pxr not available")

from openusdconnect.adapters import MockAdapter
from openusdconnect.emitter import (
    NoticeEmitter,
    decompose_trs_from_matrix,
    read_usdshade_connectable,
)
from openusdconnect.event_apply import apply_event, apply_events
from openusdconnect.protocol_constants import (
    K_ENSURE_PRIM,
    K_SET_CONNECTABLE_INPUT,
    K_SET_XFORM_TRS,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
STANDARD_SHADER_BALL = (
    _REPO_ROOT / "assets/full_assets/StandardShaderBall/standard_shader_ball_scene.usda"
)
REFERENCES_ENVIRONMENT = _REPO_ROOT / "assets/test_assets/References/utils/Environment.usda"

LIGHT_TYPES = ["DistantLight", "SphereLight", "RectLight", "DiskLight", "DomeLight"]


@pytest.fixture
def stage():
    return Usd.Stage.CreateInMemory()


class TestEnsurePrimLightTypes:
    @pytest.mark.parametrize("type_name", LIGHT_TYPES)
    def test_creates_light_type(self, stage, type_name):
        apply_event(
            stage,
            {"k": K_ENSURE_PRIM, "prim": "/L", "typeName": type_name},
        )
        p = stage.GetPrimAtPath("/L")
        assert p.IsValid()
        assert p.GetTypeName() == type_name
        # All UsdLux typed lights have LightAPI built in.
        assert p.HasAPI(UsdLux.LightAPI)

    def test_ensure_prim_with_shaping_api(self, stage):
        apply_event(
            stage,
            {
                "k": K_ENSURE_PRIM,
                "prim": "/L",
                "typeName": "SphereLight",
                "api_schemas": ["ShapingAPI"],
            },
        )
        p = stage.GetPrimAtPath("/L")
        assert p.HasAPI(UsdLux.ShapingAPI)


class TestConnectableInputOnLights:
    def test_sphere_light_intensity_color_radius(self, stage):
        apply_events(
            stage,
            [
                {"k": K_ENSURE_PRIM, "prim": "/L", "typeName": "SphereLight"},
                {
                    "k": K_SET_CONNECTABLE_INPUT,
                    "prim": "/L",
                    "info_id": "",
                    "inputs": {
                        "intensity": 5.5,
                        "color": [1.0, 0.5, 0.25],
                        "radius": 0.75,
                    },
                    "input_types": {
                        "intensity": "float",
                        "color": "color3f",
                        "radius": "float",
                    },
                },
            ],
        )
        p = stage.GetPrimAtPath("/L")
        light = UsdLux.SphereLight(p)
        assert light.GetIntensityAttr().Get() == 5.5
        c = light.GetColorAttr().Get()
        assert (float(c[0]), float(c[1]), float(c[2])) == (1.0, 0.5, 0.25)
        assert light.GetRadiusAttr().Get() == 0.75

    def test_spot_light_with_shaping_cone(self, stage):
        apply_events(
            stage,
            [
                {
                    "k": K_ENSURE_PRIM,
                    "prim": "/L",
                    "typeName": "SphereLight",
                    "api_schemas": ["ShapingAPI"],
                },
                {
                    "k": K_SET_CONNECTABLE_INPUT,
                    "prim": "/L",
                    "info_id": "",
                    "inputs": {"shaping:cone:angle": 30.0},
                    "input_types": {"shaping:cone:angle": "float"},
                },
            ],
        )
        p = stage.GetPrimAtPath("/L")
        assert p.HasAPI(UsdLux.ShapingAPI)
        assert UsdLux.ShapingAPI(p).GetShapingConeAngleAttr().Get() == 30.0

    def test_dome_light_with_asset_texture(self, stage):
        apply_events(
            stage,
            [
                {"k": K_ENSURE_PRIM, "prim": "/Dome", "typeName": "DomeLight"},
                {
                    "k": K_SET_CONNECTABLE_INPUT,
                    "prim": "/Dome",
                    "info_id": "",
                    "inputs": {"texture:file": "studio.hdr"},
                    "input_types": {"texture:file": "asset"},
                },
            ],
        )
        p = stage.GetPrimAtPath("/Dome")
        dome = UsdLux.DomeLight(p)
        asset = dome.GetTextureFileAttr().Get()
        assert isinstance(asset, Sdf.AssetPath)
        assert asset.path == "studio.hdr"

    def test_dome_light_empty_asset_clears(self, stage):
        apply_events(
            stage,
            [
                {"k": K_ENSURE_PRIM, "prim": "/Dome", "typeName": "DomeLight"},
                {
                    "k": K_SET_CONNECTABLE_INPUT,
                    "prim": "/Dome",
                    "info_id": "",
                    "inputs": {"texture:file": ""},
                    "input_types": {"texture:file": "asset"},
                },
            ],
        )
        p = stage.GetPrimAtPath("/Dome")
        asset = UsdLux.DomeLight(p).GetTextureFileAttr().Get()
        # Empty asset path — round-trips through Sdf.AssetPath() default.
        assert isinstance(asset, Sdf.AssetPath)
        assert asset.path == ""


class TestSafetyFix:
    """B9: an empty info_id with a missing prim must NOT author a phantom Shader."""

    def test_missing_prim_skips_safely(self, stage):
        apply_event(
            stage,
            {
                "k": K_SET_CONNECTABLE_INPUT,
                "prim": "/Missing",
                "info_id": "",
                "inputs": {"intensity": 1.0},
                "input_types": {"intensity": "float"},
            },
        )
        # No phantom def authored.
        p = stage.GetPrimAtPath("/Missing")
        assert not p or not p.IsValid()

    def test_missing_prim_with_info_id_creates_shader(self, stage):
        """Backwards compat: legacy shader fallback path still works when
        info_id is non-empty."""
        apply_event(
            stage,
            {
                "k": K_SET_CONNECTABLE_INPUT,
                "prim": "/S",
                "info_id": "UsdPreviewSurface",
                "inputs": {},
                "input_types": {},
            },
        )
        p = stage.GetPrimAtPath("/S")
        assert p.IsValid()
        assert p.IsA(UsdShade.Shader)
        assert UsdShade.Shader(p).GetIdAttr().Get() == "UsdPreviewSurface"


class TestReadUsdShadeConnectable:
    """The light branch in read_usdshade_connectable covers typed lights and
    MeshLightAPI / VolumeLightAPI applied to non-light prims."""

    @pytest.mark.parametrize("type_name", LIGHT_TYPES)
    def test_returns_light_kind_for_typed_lights(self, stage, type_name):
        stage.DefinePrim("/L", type_name)
        kind, info_id, _inputs, _types, _conns = read_usdshade_connectable(stage, "/L")
        assert kind == "light"
        assert info_id == ""

    def test_returns_light_kind_for_mesh_with_lightapi(self, stage):
        p = stage.DefinePrim("/MeshLight", "Mesh")
        UsdLux.MeshLightAPI.Apply(p)
        kind, info_id, _i, _t, _c = read_usdshade_connectable(stage, "/MeshLight")
        assert kind == "light"
        assert info_id == ""

    def test_returns_empty_for_non_light_non_shader(self, stage):
        stage.DefinePrim("/X", "Xform")
        kind, *_ = read_usdshade_connectable(stage, "/X")
        assert kind == ""


class TestEmitterLightDiff:
    def test_emits_set_connectable_input_for_light_intensity(self, stage):
        em = NoticeEmitter(stage)
        p = stage.DefinePrim("/L", "SphereLight")
        UsdLux.SphereLight(p).CreateIntensityAttr(2.5)

        events = em.build_events_for_dirty(include_matrices=False)
        ensure = [e for e in events if e["k"] == K_ENSURE_PRIM and e["prim"] == "/L"]
        sci = [e for e in events if e["k"] == K_SET_CONNECTABLE_INPUT and e["prim"] == "/L"]
        assert ensure
        assert ensure[0]["typeName"] == "SphereLight"
        assert sci
        assert sci[0]["info_id"] == ""
        assert sci[0]["inputs"]["intensity"] == 2.5

    def test_emits_shaping_change_via_re_emit(self, stage):
        em = NoticeEmitter(stage)
        p = stage.DefinePrim("/L", "SphereLight")
        UsdLux.SphereLight(p).CreateIntensityAttr(1.0)
        em.build_events_for_dirty(include_matrices=False)  # consume first cycle

        # Now apply ShapingAPI.
        UsdLux.ShapingAPI.Apply(p).CreateShapingConeAngleAttr(45.0)
        events = em.build_events_for_dirty(include_matrices=False)
        ensure = [e for e in events if e["k"] == K_ENSURE_PRIM and e["prim"] == "/L"]
        assert ensure
        assert "ShapingAPI" in ensure[0]["api_schemas"]
        sci = [e for e in events if e["k"] == K_SET_CONNECTABLE_INPUT and e["prim"] == "/L"]
        assert sci
        assert sci[0]["inputs"]["shaping:cone:angle"] == 45.0


class TestStructuralOrderingLight:
    def test_inputs_after_ensure_prim_with_api_schema(self, stage):
        """Shuffled events still apply in dependency order — light type and
        ShapingAPI must be present before inputs:shaping:cone:angle is written."""
        events = [
            # Out-of-order on purpose.
            {
                "k": K_SET_CONNECTABLE_INPUT,
                "prim": "/L",
                "info_id": "",
                "inputs": {"intensity": 3.0, "shaping:cone:angle": 60.0},
                "input_types": {"intensity": "float", "shaping:cone:angle": "float"},
            },
            {
                "k": K_ENSURE_PRIM,
                "prim": "/L",
                "typeName": "SphereLight",
                "api_schemas": ["ShapingAPI"],
            },
        ]
        apply_events(stage, events)
        p = stage.GetPrimAtPath("/L")
        assert p.GetTypeName() == "SphereLight"
        assert p.HasAPI(UsdLux.ShapingAPI)
        assert UsdLux.SphereLight(p).GetIntensityAttr().Get() == 3.0
        assert UsdLux.ShapingAPI(p).GetShapingConeAngleAttr().Get() == 60.0


class TestMockAdapterLight:
    def test_stores_lux_api_schemas(self):
        adapter = MockAdapter()
        adapter.apply_event(
            {
                "k": K_ENSURE_PRIM,
                "prim": "/L",
                "typeName": "SphereLight",
                "api_schemas": ["ShapingAPI", "ShadowAPI"],
            },
        )
        stored = adapter.get_prim("/L")
        assert stored["typeName"] == "SphereLight"
        assert sorted(stored["api_schemas"]) == ["ShadowAPI", "ShapingAPI"]


class TestRealAssetReplication:
    """E2E using the USD-WG StandardShaderBall asset (5 RectLights in a Y-up
    studio rig). Verifies that mutating a real light's intensity and translating
    its parent Xform replicates faithfully to an empty destination stage."""

    LIGHT_PATH = "/standard_shader_ball_scene/lights/emitterLeft/light"
    EMITTER_XFORM_PATH = "/standard_shader_ball_scene/lights/emitterLeft"

    def _open_src(self):
        return Usd.Stage.Open(str(STANDARD_SHADER_BALL))

    def test_asset_has_five_rect_lights(self):
        s = self._open_src()
        lights = [p for p in s.Traverse() if p.IsA(UsdLux.RectLight)]
        assert len(lights) == 5

    def test_intensity_change_replicates(self):
        src = self._open_src()
        em = NoticeEmitter(src)

        light = UsdLux.RectLight(src.GetPrimAtPath(self.LIGHT_PATH))
        original_intensity = light.GetIntensityAttr().Get()
        light.GetIntensityAttr().Set(original_intensity + 6.0)  # 9 -> 15

        events = em.build_events_for_dirty(include_matrices=False)

        ensure = [
            e for e in events if e["k"] == K_ENSURE_PRIM and e["prim"] == self.LIGHT_PATH
        ]
        sci = [
            e for e in events
            if e["k"] == K_SET_CONNECTABLE_INPUT and e["prim"] == self.LIGHT_PATH
        ]
        assert ensure
        assert ensure[0]["typeName"] == "RectLight"
        assert sci
        assert sci[0]["info_id"] == ""
        assert sci[0]["inputs"]["intensity"] == pytest.approx(15.0)

        # Apply to a fresh destination stage and verify.
        dst = Usd.Stage.CreateInMemory()
        apply_events(dst, events)
        dst_light = UsdLux.RectLight(dst.GetPrimAtPath(self.LIGHT_PATH))
        assert dst_light.GetIntensityAttr().Get() == pytest.approx(15.0)

    def test_xform_translation_replicates(self):
        """Mutating the parent Xform's matrix should replicate the new world
        translation to the destination — exact value doesn't matter, what
        matters is src world translation == dst world translation."""
        src = self._open_src()
        em = NoticeEmitter(src)

        em_prim = src.GetPrimAtPath(self.EMITTER_XFORM_PATH)
        op_attr = em_prim.GetAttribute("xformOp:transform")
        old_mat = op_attr.Get()
        new_mat = Gf.Matrix4d(old_mat)
        # Shift the matrix's translation by (+5, +2, 0). The composed world
        # transform also has a tweak op, but that applies equally on both
        # sides — replication just needs src.world == dst.world.
        new_mat[3] = Gf.Vec4d(
            old_mat[3][0] + 5.0, old_mat[3][1] + 2.0, old_mat[3][2], 1.0,
        )
        op_attr.Set(new_mat)

        events = em.build_events_for_dirty(include_matrices=False)

        trs = [
            e for e in events
            if e["k"] == K_SET_XFORM_TRS and e["prim"] == self.EMITTER_XFORM_PATH
        ]
        assert trs
        assert "t" in trs[0].get("fields", [])

        src_xf = UsdGeom.Xformable(em_prim)
        src_world = src_xf.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        src_t, _, _ = decompose_trs_from_matrix(src_world)

        # Apply to a fresh destination stage and verify identical world TRS.
        dst = Usd.Stage.CreateInMemory()
        apply_events(dst, events)
        dst_xf = UsdGeom.Xformable(dst.GetPrimAtPath(self.EMITTER_XFORM_PATH))
        dst_world = dst_xf.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        dst_t, _, _ = decompose_trs_from_matrix(dst_world)

        for axis in (0, 1, 2):
            assert dst_t[axis] == pytest.approx(src_t[axis], abs=1e-3)

    def test_user_applied_shaping_api_replicates(self):
        """Apply ShapingAPI to a real asset RectLight (turning it into a spot)
        and verify both the schema and its cone-angle input land on the
        destination — exercises the api_schemas wire field end-to-end on a
        real asset, distinct from typed-schema built-ins."""
        src = self._open_src()
        em = NoticeEmitter(src)

        light_prim = src.GetPrimAtPath(self.LIGHT_PATH)
        # The asset's RectLight has only built-in schemas; ShapingAPI is
        # NOT applied. Apply it and author a cone angle.
        UsdLux.ShapingAPI.Apply(light_prim).CreateShapingConeAngleAttr(30.0)

        events = em.build_events_for_dirty(include_matrices=False)

        # The ensure_prim event must carry ShapingAPI in api_schemas.
        ensure = [
            e for e in events if e["k"] == K_ENSURE_PRIM and e["prim"] == self.LIGHT_PATH
        ]
        assert ensure
        assert "ShapingAPI" in ensure[0]["api_schemas"]

        # The set_connectable_input event must carry the shaping cone angle.
        sci = [
            e for e in events
            if e["k"] == K_SET_CONNECTABLE_INPUT and e["prim"] == self.LIGHT_PATH
        ]
        assert sci
        assert sci[0]["inputs"].get("shaping:cone:angle") == pytest.approx(30.0)

        # Receiver: ShapingAPI must be applied AND the cone angle must be set.
        dst = Usd.Stage.CreateInMemory()
        apply_events(dst, events)
        dst_prim = dst.GetPrimAtPath(self.LIGHT_PATH)
        assert dst_prim.HasAPI(UsdLux.ShapingAPI)
        assert UsdLux.ShapingAPI(dst_prim).GetShapingConeAngleAttr().Get() == pytest.approx(30.0)

    def test_combined_intensity_and_move_replicate(self):
        """One emit cycle carrying both a light input change and a parent
        Xform move — exercises the full receive-side ordering."""
        src = self._open_src()
        em = NoticeEmitter(src)

        # Move parent Xform.
        em_prim = src.GetPrimAtPath(self.EMITTER_XFORM_PATH)
        op_attr = em_prim.GetAttribute("xformOp:transform")
        old_mat = op_attr.Get()
        new_mat = Gf.Matrix4d(old_mat)
        new_mat[3] = Gf.Vec4d(
            old_mat[3][0] + 3.0, old_mat[3][1], old_mat[3][2] + 1.0, 1.0,
        )
        op_attr.Set(new_mat)

        # Change intensity.
        light = UsdLux.RectLight(src.GetPrimAtPath(self.LIGHT_PATH))
        light.GetIntensityAttr().Set(20.0)

        events = em.build_events_for_dirty(include_matrices=False)
        dst = Usd.Stage.CreateInMemory()
        apply_events(dst, events)

        dst_light = UsdLux.RectLight(dst.GetPrimAtPath(self.LIGHT_PATH))
        assert dst_light.GetIntensityAttr().Get() == pytest.approx(20.0)
        assert dst.GetPrimAtPath(self.LIGHT_PATH).GetTypeName() == "RectLight"
        # Width/height carried as first-encounter inputs alongside intensity.
        assert dst_light.GetWidthAttr().Get() == pytest.approx(14.960001)
        assert dst_light.GetHeightAttr().Get() == pytest.approx(14.960001)

        # Moved parent Xform: source world translation matches destination.
        src_xf = UsdGeom.Xformable(em_prim)
        src_world = src_xf.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        src_t, _, _ = decompose_trs_from_matrix(src_world)
        dst_xf = UsdGeom.Xformable(dst.GetPrimAtPath(self.EMITTER_XFORM_PATH))
        dst_world = dst_xf.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        dst_t, _, _ = decompose_trs_from_matrix(dst_world)
        for axis in (0, 1, 2):
            assert dst_t[axis] == pytest.approx(src_t[axis], abs=1e-3)


class TestRealAssetShapingAPIReplication:
    """E2E against assets/test_assets/References/utils/Environment.usda — a real
    asset whose 3 SphereLights already have ShapingAPI applied (`prepend
    apiSchemas = ["ShapingAPI"]` in the source). Verifies the full
    api_schemas wire path against authored asset content rather than
    synthetic application."""

    SPOT_PATH = "/Environment/Lights/SphereLight"
    SPOT_PARENT_XFORM = "/Environment/Lights"

    def _open_src(self):
        return Usd.Stage.Open(str(REFERENCES_ENVIRONMENT))

    def test_source_lights_have_shaping_api_applied(self):
        s = self._open_src()
        spots = [
            "/Environment/Lights/SphereLight",
            "/Environment/Lights/SphereLight_01",
            "/Environment/Lights/SphereLight_02",
        ]
        for path in spots:
            p = s.GetPrimAtPath(path)
            assert p.IsValid(), path
            assert p.IsA(UsdLux.SphereLight)
            assert p.HasAPI(UsdLux.ShapingAPI), f"{path} missing ShapingAPI"

    def test_shaping_replicates_to_destination(self):
        """Authored ShapingAPI on the source must arrive on the destination
        via the api_schemas wire field — not via the typed-schema built-ins
        path (SphereLight doesn't bring ShapingAPI as a built-in)."""
        src = self._open_src()
        em = NoticeEmitter(src)

        # Drive a notice on one of the existing spots so the emitter sees
        # it on the next build cycle. Bump intensity by 0.1 — small but
        # past the diff epsilon.
        spot_prim = src.GetPrimAtPath(self.SPOT_PATH)
        light = UsdLux.SphereLight(spot_prim)
        original_intensity = light.GetIntensityAttr().Get()
        light.GetIntensityAttr().Set(original_intensity + 0.5)

        events = em.build_events_for_dirty(include_matrices=False)

        # The ensure_prim event for the spot must carry ShapingAPI.
        ensure = [e for e in events if e["k"] == K_ENSURE_PRIM and e["prim"] == self.SPOT_PATH]
        assert ensure
        assert ensure[0]["typeName"] == "SphereLight"
        assert "ShapingAPI" in ensure[0]["api_schemas"]

        # set_connectable_input must carry both the SphereLight inputs
        # (intensity, radius) AND the ShapingAPI inputs (shaping:cone:angle,
        # shaping:focus) — they share the connectable interface.
        sci = [
            e for e in events
            if e["k"] == K_SET_CONNECTABLE_INPUT and e["prim"] == self.SPOT_PATH
        ]
        assert sci
        inputs = sci[0]["inputs"]
        assert "intensity" in inputs
        assert "shaping:cone:angle" in inputs
        assert inputs["shaping:cone:angle"] == pytest.approx(180.0)

        # Receiver gets the typed schema + the applied ShapingAPI + values.
        dst = Usd.Stage.CreateInMemory()
        apply_events(dst, events)
        dst_prim = dst.GetPrimAtPath(self.SPOT_PATH)
        assert dst_prim.GetTypeName() == "SphereLight"
        assert dst_prim.HasAPI(UsdLux.ShapingAPI)
        dst_light = UsdLux.SphereLight(dst_prim)
        dst_shape = UsdLux.ShapingAPI(dst_prim)
        assert dst_light.GetIntensityAttr().Get() == pytest.approx(original_intensity + 0.5)
        assert dst_shape.GetShapingConeAngleAttr().Get() == pytest.approx(180.0)
        assert dst_light.GetRadiusAttr().Get() == pytest.approx(5.0)

    def test_shaping_cone_angle_change_replicates(self):
        """Authoring a tighter cone angle on the source spot — verify the
        shaping value change rides on set_connectable_input and the receiver
        retains the ShapingAPI."""
        src = self._open_src()
        em = NoticeEmitter(src)

        spot_prim = src.GetPrimAtPath(self.SPOT_PATH)
        # Tighten the cone from 180° to 45°.
        UsdLux.ShapingAPI(spot_prim).GetShapingConeAngleAttr().Set(45.0)

        events = em.build_events_for_dirty(include_matrices=False)

        sci = [
            e for e in events
            if e["k"] == K_SET_CONNECTABLE_INPUT and e["prim"] == self.SPOT_PATH
        ]
        assert sci
        assert sci[0]["inputs"].get("shaping:cone:angle") == pytest.approx(45.0)

        dst = Usd.Stage.CreateInMemory()
        apply_events(dst, events)
        dst_prim = dst.GetPrimAtPath(self.SPOT_PATH)
        assert dst_prim.HasAPI(UsdLux.ShapingAPI)
        assert UsdLux.ShapingAPI(dst_prim).GetShapingConeAngleAttr().Get() == pytest.approx(45.0)
