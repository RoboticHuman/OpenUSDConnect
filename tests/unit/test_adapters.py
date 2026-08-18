"""Tests for MockAdapter and UsdStageAdapter."""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    from pxr import Sdf, Usd, UsdGeom  # noqa: E402

    PXR_AVAILABLE = True
except ImportError:
    PXR_AVAILABLE = False

from openusdconnect.adapters import MockAdapter, UsdStageAdapter
from openusdconnect.protocol_constants import EVENT_KEYS
from openusdconnect.sdf_spec_delta import serialize_spec_fields

pytestmark_pxr = pytest.mark.skipif(not PXR_AVAILABLE, reason="pxr not available")
INHERIT_SPECIALIZE_ASSET = (
    Path(__file__).parents[2]
    / "assets"
    / "test_assets"
    / "foundation"
    / "stage_composition"
    / "inherit_and_specialize.usda"
)


def _spec_field_event(
    layer: Sdf.Layer,
    spec_path: str,
    spec_kind: str,
    fields: list[str],
) -> dict:
    return {
        "k": "set_sdf_spec_fields",
        "prim": "/" if spec_kind == "layer" else spec_path,
        "spec_path": spec_path,
        "spec_kind": spec_kind,
        "fields": fields,
        "fragment": serialize_spec_fields(
            layer,
            spec_path,
            spec_kind,
            fields,
            stabilize_asset_paths=False,
        ),
        "removed": False,
    }


def _copy_stage(stage: Usd.Stage, identifier: str) -> Usd.Stage:
    layer = Sdf.Layer.CreateAnonymous(identifier)
    layer.TransferContent(stage.GetRootLayer())
    return Usd.Stage.Open(layer)


# ---------------------------------------------------------------------------
# MockAdapter
# ---------------------------------------------------------------------------


def test_batch_false_result_is_an_intentional_noop_not_delivery_failure():
    adapter = MockAdapter()

    assert adapter.apply_events(
        [{"k": "set_visibility", "prim": "/missing", "visible": True}]
    ) == 1


def test_unknown_adapter_event_is_rejected():
    with pytest.raises(ValueError, match="unsupported adapter event kind"):
        MockAdapter().apply_event({"k": "future_event", "prim": "/World"})


class TestMockAdapterEnsurePrim:
    def test_creates_prim(self):
        a = MockAdapter()
        assert a.ensure_prim("/A", "Sphere")
        assert a.get_prim("/A")["typeName"] == "Sphere"

    def test_idempotent(self):
        a = MockAdapter()
        a.ensure_prim("/A", "Cube")
        a.ensure_prim("/A", "Sphere")  # should not overwrite
        assert a.get_prim("/A")["typeName"] == "Cube"


class TestMockAdapterXformOps:
    def test_ensure_xform_ops(self):
        a = MockAdapter()
        a.ensure_prim("/A")
        assert a.ensure_xform_ops("/A")
        assert "translate" in a.get_prim("/A")["ops"]

    def test_missing_prim_returns_false(self):
        a = MockAdapter()
        assert not a.ensure_xform_ops("/missing")


class TestMockAdapterTRS:
    def test_set_xform_trs(self):
        a = MockAdapter()
        a.ensure_prim("/A")
        a.set_xform_trs("/A", t=[1, 2, 3], r=[1, 0, 0, 0])
        trs = a.get_trs("/A")
        assert trs["t"] == [1, 2, 3]
        assert trs["r"] == [1, 0, 0, 0]

    def test_missing_prim_returns_false(self):
        a = MockAdapter()
        assert not a.set_xform_trs("/missing", t=[0, 0, 0])


class TestMockAdapterDelete:
    def test_delete_existing(self):
        a = MockAdapter()
        a.ensure_prim("/A")
        assert a.delete_prim("/A")
        assert a.get_prim("/A") == {}

    def test_delete_missing_returns_false(self):
        a = MockAdapter()
        assert not a.delete_prim("/missing")


class TestMockAdapterDeactivate:
    def test_deactivate(self):
        a = MockAdapter()
        a.ensure_prim("/A")
        assert a.deactivate_prim("/A", active=False)
        assert a.get_prim("/A")["active"] is False

    def test_reactivate(self):
        a = MockAdapter()
        a.ensure_prim("/A")
        a.deactivate_prim("/A", active=False)
        a.deactivate_prim("/A", active=True)
        assert a.get_prim("/A")["active"] is True

    def test_missing_prim_returns_false(self):
        a = MockAdapter()
        assert not a.deactivate_prim("/missing")


class TestMockAdapterRename:
    def test_rename(self):
        a = MockAdapter()
        a.ensure_prim("/World/OldName")
        assert a.rename_prim("/World/OldName", "NewName")
        assert a.get_prim("/World/OldName") == {}
        assert a.get_prim("/World/NewName")["typeName"] == "Xform"

    def test_missing_prim_returns_false(self):
        a = MockAdapter()
        assert not a.rename_prim("/missing", "X")


class TestMockAdapterVisibility:
    def test_set_invisible(self):
        a = MockAdapter()
        a.ensure_prim("/A")
        assert a.set_visibility("/A", visible=False)
        assert a.get_prim("/A")["visible"] is False

    def test_missing_prim_returns_false(self):
        a = MockAdapter()
        assert not a.set_visibility("/missing", visible=True)


class TestMockAdapterGprimAttrs:
    def test_set_attrs(self):
        a = MockAdapter()
        a.ensure_prim("/A", "Sphere")
        assert a.set_gprim_attrs("/A", {"radius": 2.0})
        assert a.get_prim("/A")["gprim_attrs"]["radius"] == 2.0

    def test_merge_attrs(self):
        a = MockAdapter()
        a.ensure_prim("/A", "Cone")
        a.set_gprim_attrs("/A", {"radius": 1.0})
        a.set_gprim_attrs("/A", {"height": 3.0})
        attrs = a.get_prim("/A")["gprim_attrs"]
        assert attrs["radius"] == 1.0
        assert attrs["height"] == 3.0

    def test_missing_prim_returns_false(self):
        a = MockAdapter()
        assert not a.set_gprim_attrs("/missing", {"radius": 1.0})


class TestMockAdapterReference:
    def test_set_reference_auto_creates_prim(self):
        a = MockAdapter()
        assert a.set_reference("/A", [{"asset_path": "chair.usd"}])
        assert a.get_prim("/A")["references"] == [{"asset_path": "chair.usd"}]

    def test_set_reference_on_existing_prim(self):
        a = MockAdapter()
        a.ensure_prim("/A", "Mesh")
        a.set_reference("/A", [{"asset_path": "x.usd"}])
        assert a.get_prim("/A")["references"] == [{"asset_path": "x.usd"}]
        assert a.get_prim("/A")["typeName"] == "Mesh"


class TestMockAdapterPayload:
    def test_set_payload_auto_creates_prim(self):
        a = MockAdapter()
        assert a.set_payload("/A", [{"asset_path": "heavy.usd"}])
        assert a.get_prim("/A")["payloads"] == [{"asset_path": "heavy.usd"}]

    def test_load_unload_payload(self):
        a = MockAdapter()
        a.load_payload("/A")
        a.unload_payload("/A")
        assert a.calls == [("load_payload", "/A"), ("unload_payload", "/A")]


class TestMockAdapterVariantSelections:
    def test_set_variant_selections_auto_creates_prim(self):
        a = MockAdapter()
        assert a.set_variant_selections("/A", {"size": "large"})
        assert a.get_prim("/A")["variant_selections"] == {"size": "large"}

    def test_set_variant_selections_on_existing_prim(self):
        a = MockAdapter()
        a.ensure_prim("/A", "Sphere")
        a.set_variant_selections("/A", {"color": "red"})
        assert a.get_prim("/A")["variant_selections"] == {"color": "red"}
        assert a.get_prim("/A")["typeName"] == "Sphere"


class TestMockAdapterGetTrs:
    def test_missing_prim_returns_empty(self):
        a = MockAdapter()
        assert a.get_trs("/missing") == {}


# ---------------------------------------------------------------------------
# UsdStageAdapter
# ---------------------------------------------------------------------------


@pytestmark_pxr
class TestUsdStageAdapterDirectMethods:
    """Test UsdStageAdapter methods called directly (not via apply_events)."""

    def _make_stage(self):
        from openusdconnect.adapters import UsdStageAdapter

        stage = Usd.Stage.CreateInMemory()
        return stage, UsdStageAdapter(stage)

    def test_type_check(self):
        from openusdconnect.adapters import UsdStageAdapter

        with pytest.raises(TypeError):
            UsdStageAdapter("not a stage")

    def test_every_direct_method_routes_through_the_event_registry(self):
        _stage, adapter = self._make_stage()
        captured = []

        def _capture(event):
            captured.append(event)
            return True

        adapter.apply_event = _capture
        calls = (
            lambda: adapter.ensure_prim("/A", "Xform", ["ModelAPI"]),
            lambda: adapter.ensure_xform_ops("/A"),
            lambda: adapter.set_xform_trs("/A", t=[1, 2, 3], time=1.0),
            lambda: adapter.delete_prim("/A"),
            lambda: adapter.deactivate_prim("/A", active=False),
            lambda: adapter.rename_prim("/A", "B"),
            lambda: adapter.set_visibility("/A", visible=False, time=1.0),
            lambda: adapter.set_gprim_attrs("/A", {"radius": 2.0}, time=1.0),
            lambda: adapter.set_sdf_spec_fields(
                "/A", "/A.value", "attribute", ["default"], "#usda 1.0\n"
            ),
            lambda: adapter.set_sublayers(
                [{"authored_path": "child.usda"}], generation="graph-1", revision=2
            ),
            lambda: adapter.replace_sdf_layer_content("#usda 1.0\n"),
            lambda: adapter.set_reference("/A", [], list_op_authored=True),
            lambda: adapter.set_payload("/A", [], list_op_authored=True),
            lambda: adapter.load_payload("/A"),
            lambda: adapter.unload_payload("/A"),
            lambda: adapter.set_variant_selections("/A", {"look": "red"}),
            lambda: adapter.set_material_binding("/A", "/Looks/Material", "preview"),
            lambda: adapter.set_connectable_input(
                "/A", "UsdPreviewSurface", {"roughness": 0.5}, {"roughness": "float"}
            ),
            lambda: adapter.set_connectable_connection(
                "/A", {"inputs:surface": "/Shader.outputs:surface"}
            ),
            lambda: adapter.set_stage_metadata(upAxis="Y"),
            lambda: adapter.set_instanceable("/A", True),
            lambda: adapter.set_point_instancer("/A", positions=[[0, 0, 0]]),
        )

        assert all(call() for call in calls)
        assert len(captured) == len(EVENT_KEYS)
        assert {event["k"] for event in captured} == EVENT_KEYS

    def test_direct_layer_operations_apply_to_the_edit_target(self, tmp_path):
        child = Sdf.Layer.CreateNew(str(tmp_path / "child.usda"))
        child.Save()
        root = Sdf.Layer.CreateNew(str(tmp_path / "root.usda"))
        stage = Usd.Stage.Open(root)
        adapter = UsdStageAdapter(stage)

        adapter.set_sublayers(
            [{"authored_path": "child.usda", "offset": 2.0, "scale": 3.0}],
            generation="graph-1",
            revision=1,
        )
        assert list(stage.GetRootLayer().subLayerPaths) == ["child.usda"]
        assert stage.GetRootLayer().subLayerOffsets[0].offset == 2.0
        assert stage.GetRootLayer().subLayerOffsets[0].scale == 3.0

        adapter.replace_sdf_layer_content('#usda 1.0\n\ndef Xform "Replacement" {}\n')
        assert stage.GetPrimAtPath("/Replacement").IsValid()
        assert list(stage.GetRootLayer().subLayerPaths) == ["child.usda"]

    def test_direct_class_arcs_follow_liverps_strength_and_namespace_mapping(self):
        """Direct field updates obey Local > Inherits > Specializes.

        OpenUSD specification sections 10.3.2.3, 10.3.2.4, and 10.4 state
        that both class arcs map source descendants below their consumer,
        local opinions beat both arcs, and inherits beat specializes.
        """
        initial = Usd.Stage.CreateInMemory()
        inherited = initial.CreateClassPrim("/Classes/Inherited")
        inherited.CreateAttribute("strength", Sdf.ValueTypeNames.String).Set("inherit")
        inherited.CreateAttribute("localWins", Sdf.ValueTypeNames.String).Set("inherit")
        inherited_child = initial.DefinePrim("/Classes/Inherited/InheritChild", "Sphere")
        inherited_child.CreateAttribute("radius", Sdf.ValueTypeNames.Double).Set(2.0)

        specialized = initial.CreateClassPrim("/Classes/Specialized")
        specialized.CreateAttribute("strength", Sdf.ValueTypeNames.String).Set("specialize")
        specialized.CreateAttribute("localWins", Sdf.ValueTypeNames.String).Set("specialize")
        specialized_child = initial.DefinePrim(
            "/Classes/Specialized/SpecializeChild", "Sphere"
        )
        specialized_child.CreateAttribute("radius", Sdf.ValueTypeNames.Double).Set(3.0)

        model = initial.DefinePrim("/World/Model", "Xform")
        model.CreateAttribute("localWins", Sdf.ValueTypeNames.String).Set("local")

        incoming = _copy_stage(initial, "class-arcs-incoming.usda")
        incoming_model = incoming.GetPrimAtPath("/World/Model")
        incoming_model.GetInherits().AddInherit("/Classes/Inherited")
        incoming_model.GetSpecializes().AddSpecialize("/Classes/Specialized")
        event = _spec_field_event(
            incoming.GetRootLayer(),
            "/World/Model",
            "prim",
            ["inheritPaths", "specializes"],
        )

        direct_stage = _copy_stage(initial, "class-arcs-direct.usda")
        event_stage = _copy_stage(initial, "class-arcs-event.usda")
        direct = UsdStageAdapter(direct_stage)
        direct.set_sdf_spec_fields(
            event["prim"],
            event["spec_path"],
            event["spec_kind"],
            event["fields"],
            event["fragment"],
        )
        UsdStageAdapter(event_stage).apply_event(event)

        for stage in (direct_stage, event_stage):
            composed = stage.GetPrimAtPath("/World/Model")
            assert composed.GetAttribute("localWins").Get() == "local"
            assert composed.GetAttribute("strength").Get() == "inherit"
            assert (
                stage.GetPrimAtPath("/World/Model/InheritChild")
                .GetAttribute("radius")
                .Get()
                == pytest.approx(2.0)
            )
            assert (
                stage.GetPrimAtPath("/World/Model/SpecializeChild")
                .GetAttribute("radius")
                .Get()
                == pytest.approx(3.0)
            )
        assert (
            direct_stage.GetRootLayer().ExportToString()
            == event_stage.GetRootLayer().ExportToString()
        )

    def test_direct_layer_relocates_move_referenced_namespace(self):
        """A direct layer field update relocates composed, not source, opinions.

        OpenUSD specification section 10.3.2.6 defines relocates as mapping a
        source layer stack introduced by another composition arc into the local
        namespace. The old composed path disappears, the target receives its
        opinions and descendants, and the referenced source remains unchanged.
        """
        initial = Usd.Stage.CreateInMemory()
        initial.DefinePrim("/Library", "Xform")
        source = initial.DefinePrim("/Library/Original", "Sphere")
        source.CreateAttribute("radius", Sdf.ValueTypeNames.Double).Set(5.0)
        initial.DefinePrim("/Library/Original/Leaf", "Cube")
        initial.DefinePrim("/World/Asset", "Xform").GetReferences().AddInternalReference(
            "/Library"
        )
        assert initial.GetPrimAtPath("/World/Asset/Original")

        incoming = Sdf.Layer.CreateAnonymous("relocates-incoming.usda")
        incoming.TransferContent(initial.GetRootLayer())
        incoming.relocates = [
            (Sdf.Path("/World/Asset/Original"), Sdf.Path("/World/Asset/Renamed")),
        ]
        event = _spec_field_event(incoming, "/", "layer", ["layerRelocates"])

        direct_stage = _copy_stage(initial, "relocates-direct.usda")
        event_stage = _copy_stage(initial, "relocates-event.usda")
        direct = UsdStageAdapter(direct_stage)
        direct.set_sdf_spec_fields(
            event["prim"],
            event["spec_path"],
            event["spec_kind"],
            event["fields"],
            event["fragment"],
        )
        UsdStageAdapter(event_stage).apply_event(event)

        for stage in (direct_stage, event_stage):
            assert not stage.GetPrimAtPath("/World/Asset/Original")
            relocated = stage.GetPrimAtPath("/World/Asset/Renamed")
            assert relocated
            assert relocated.GetAttribute("radius").Get() == pytest.approx(5.0)
            assert stage.GetPrimAtPath("/World/Asset/Renamed/Leaf")
            assert stage.GetPrimAtPath("/Library/Original")
            assert stage.GetPrimAtPath("/Library/Original/Leaf")
        assert (
            direct_stage.GetRootLayer().ExportToString()
            == event_stage.GetRootLayer().ExportToString()
        )

    @pytest.mark.skipif(
        not INHERIT_SPECIALIZE_ASSET.is_file(),
        reason="USD WG assets submodule not present",
    )
    def test_direct_layer_replacement_composes_real_inherit_specialize_asset(self):
        """The repository fixture preserves implied inherit/specialize behavior."""
        source_layer = Sdf.Layer.FindOrOpen(str(INHERIT_SPECIALIZE_ASSET))
        assert source_layer is not None

        stage, adapter = self._make_stage()
        adapter.replace_sdf_layer_content(source_layer.ExportToString())

        def _display_color(path: str) -> tuple[float, float, float]:
            value = UsdGeom.Gprim(stage.GetPrimAtPath(path)).GetDisplayColorAttr().Get()
            assert value and len(value) == 1
            return tuple(float(component) for component in value[0])

        # Local yellow overrides both source-class colors.
        assert _display_color("/World/cubeScene/specializes") == pytest.approx(
            (0.8, 0.8, 0.0)
        )
        assert _display_color("/World/cubeScene/inherits") == pytest.approx(
            (0.8, 0.8, 0.0)
        )
        # Across the internal reference, the implied inherit sees the stronger
        # green source override. Specializes stays globally weaker than the
        # yellow opinion authored on the specializing prim.
        assert _display_color("/World/cubeSceneReferenced/source") == pytest.approx(
            (0.0, 0.8, 0.0)
        )
        assert _display_color("/World/cubeSceneReferenced/inherits") == pytest.approx(
            (0.0, 0.8, 0.0)
        )
        assert _display_color("/World/cubeSceneReferenced/specializes") == pytest.approx(
            (0.8, 0.8, 0.0)
        )

    def test_ensure_prim(self):
        stage, adapter = self._make_stage()
        assert adapter.ensure_prim("/World/Sphere", "Sphere")
        assert stage.GetPrimAtPath("/World/Sphere").IsValid()

    def test_ensure_xform_ops(self):
        stage, adapter = self._make_stage()
        adapter.ensure_prim("/A")
        assert adapter.ensure_xform_ops("/A")
        xf = UsdGeom.Xformable(stage.GetPrimAtPath("/A"))
        assert len(xf.GetOrderedXformOps()) == 3

    def test_set_xform_trs(self):
        stage, adapter = self._make_stage()
        adapter.ensure_prim("/A")
        adapter.ensure_xform_ops("/A")
        adapter.set_xform_trs("/A", t=[5, 10, 0])
        from openusdconnect.event_apply import find_op

        xf = UsdGeom.Xformable(stage.GetPrimAtPath("/A"))
        t_op = find_op(xf, "translate")
        assert list(t_op.Get()) == [5.0, 10.0, 0.0]

    def test_delete_prim(self):
        stage, adapter = self._make_stage()
        adapter.ensure_prim("/A")
        assert adapter.delete_prim("/A")
        assert not stage.GetPrimAtPath("/A").IsValid()

    def test_deactivate_prim(self):
        stage, adapter = self._make_stage()
        adapter.ensure_prim("/A")
        assert adapter.deactivate_prim("/A", active=False)
        prim = stage.GetPrimAtPath("/A")
        assert not prim.IsActive()

    def test_rename_prim(self):
        stage, adapter = self._make_stage()
        adapter.ensure_prim("/World/Old")
        assert adapter.rename_prim("/World/Old", "New")
        assert stage.GetPrimAtPath("/World/New").IsValid()
        assert not stage.GetPrimAtPath("/World/Old").IsValid()

    def test_set_visibility(self):
        stage, adapter = self._make_stage()
        adapter.ensure_prim("/A")
        adapter.set_visibility("/A", visible=False)
        img = UsdGeom.Imageable(stage.GetPrimAtPath("/A"))
        assert img.GetVisibilityAttr().Get() == "invisible"

    def test_set_gprim_attrs(self):
        stage, adapter = self._make_stage()
        adapter.ensure_prim("/A", "Sphere")
        adapter.set_gprim_attrs("/A", {"radius": 3.0})
        prim = stage.GetPrimAtPath("/A")
        assert prim.GetAttribute("radius").Get() == 3.0

    def test_set_reference(self):
        stage, adapter = self._make_stage()
        adapter.ensure_prim("/A")
        adapter.set_reference("/A", [{"asset_path": "/tmp/test.usd", "prim_path": "/Model"}])
        refs = []
        for ref in stage.GetPrimAtPath("/A").GetPrimStack()[0].referenceList.prependedItems:
            refs.append(ref.assetPath)
        assert len(refs) == 1

    def test_set_payload(self):
        stage, adapter = self._make_stage()
        adapter.ensure_prim("/A")
        adapter.set_payload("/A", [{"asset_path": "/tmp/payload.usd"}])
        prim = stage.GetPrimAtPath("/A")
        assert prim.HasAuthoredPayloads()

    def test_load_unload_payload(self):
        stage, adapter = self._make_stage()
        adapter.ensure_prim("/A")
        adapter.set_payload("/A", [{"asset_path": "/tmp/p.usd"}])
        adapter.load_payload("/A")
        adapter.unload_payload("/A")
        # Just verify no crash actual load/unload depends on file existing

    def test_set_variant_selections(self):
        import os

        fixture = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "fixtures", "variant_sphere.usda"
        )
        from openusdconnect.adapters import UsdStageAdapter

        stage = Usd.Stage.Open(fixture)
        adapter = UsdStageAdapter(stage)
        adapter.set_variant_selections("/World/Sphere", {"size": "large"})
        sel = stage.GetPrimAtPath("/World/Sphere").GetVariantSets().GetVariantSelection("size")
        assert sel == "large"
