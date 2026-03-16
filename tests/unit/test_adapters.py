"""Tests for MockAdapter and UsdStageAdapter."""

import pytest

try:
    from pxr import Usd, UsdGeom  # noqa: E402

    PXR_AVAILABLE = True
except ImportError:
    PXR_AVAILABLE = False

from openusdconnect.adapters import MockAdapter

pytestmark_pxr = pytest.mark.skipif(not PXR_AVAILABLE, reason="pxr not available")


# ---------------------------------------------------------------------------
# MockAdapter
# ---------------------------------------------------------------------------


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
        a.set_xform_trs("/A", {"fields": ["t", "r"], "t": [1, 2, 3], "r": [1, 0, 0, 0]})
        trs = a.get_trs("/A")
        assert trs["t"] == [1, 2, 3]
        assert trs["r"] == [1, 0, 0, 0]

    def test_missing_prim_returns_false(self):
        a = MockAdapter()
        assert not a.set_xform_trs("/missing", {"fields": ["t"], "t": [0, 0, 0]})


class TestMockAdapterMatrices:
    def test_set_xform_matrices(self):
        a = MockAdapter()
        a.ensure_prim("/A")
        local = list(range(16))
        world = list(range(16, 32))
        assert a.set_xform_matrices("/A", {"local_m": local, "world_m": world})
        p = a.get_prim("/A")
        assert p["matrices"]["local"] == local
        assert p["matrices"]["world"] == world

    def test_missing_prim_returns_false(self):
        a = MockAdapter()
        assert not a.set_xform_matrices("/missing", {})


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
        adapter.set_xform_trs("/A", {
            "k": "set_xform_trs", "prim": "/A",
            "fields": ["t"], "t": [5, 10, 0],
        })
        from openusdconnect.event_apply import find_op

        xf = UsdGeom.Xformable(stage.GetPrimAtPath("/A"))
        t_op = find_op(xf, "translate")
        assert list(t_op.Get()) == [5.0, 10.0, 0.0]

    def test_set_xform_matrices_noop(self):
        _, adapter = self._make_stage()
        assert adapter.set_xform_matrices("/A", {})

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
        # Just verify no crash — actual load/unload depends on file existing

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
