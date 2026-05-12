"""Stage parity tests — verify emitter, server, and receiver stages converge.

Exercises the real server event processing (apply_txn, sequence assignment,
event log) without TCP. Events flow: emitter → server.apply_txn → replay
from server log → receiver.apply_events. All three stages are compared.
"""

import os

import pytest

from openusdconnect.codec import message_to_dict

try:
    from pxr import Gf, Usd, UsdGeom

    PXR_AVAILABLE = True
except ImportError:
    PXR_AVAILABLE = False

pytestmark = pytest.mark.skipif(not PXR_AVAILABLE, reason="pxr not available")

from openusdconnect.emitter import NoticeEmitter
from openusdconnect.event_apply import apply_events, ensure_canonical_ops, find_op
from openusdconnect.server import UsdSyncServer


# ---------------------------------------------------------------------------
# Comparison helpers
# ---------------------------------------------------------------------------


def _compare_attr(stage_a, stage_b, prim_path, attr_name, eps=1e-6):
    """Assert that a single attribute has the same composed value on both stages."""
    val_a = stage_a.GetPrimAtPath(prim_path).GetAttribute(attr_name).Get()
    val_b = stage_b.GetPrimAtPath(prim_path).GetAttribute(attr_name).Get()
    if isinstance(val_a, float):
        assert abs(val_a - val_b) < eps, f"{prim_path}.{attr_name}: {val_a} != {val_b}"
    else:
        assert val_a == val_b, f"{prim_path}.{attr_name}: {val_a} != {val_b}"


def _compare_trs(stage_a, stage_b, prim_path, eps=1e-6):
    """Assert that local transforms match between two stages."""
    for label, stage in [("A", stage_a), ("B", stage_b)]:
        prim = stage.GetPrimAtPath(prim_path)
        assert prim.IsValid(), f"{prim_path} missing on stage {label}"

    xf_a = UsdGeom.Xformable(stage_a.GetPrimAtPath(prim_path))
    xf_b = UsdGeom.Xformable(stage_b.GetPrimAtPath(prim_path))

    m_a = xf_a.GetLocalTransformation(Usd.TimeCode.Default())
    m_b = xf_b.GetLocalTransformation(Usd.TimeCode.Default())
    if isinstance(m_a, tuple):
        m_a = m_a[0]
    if isinstance(m_b, tuple):
        m_b = m_b[0]

    for r in range(4):
        for c in range(4):
            assert abs(m_a[r][c] - m_b[r][c]) < eps, (
                f"{prim_path} transform [{r}][{c}]: {m_a[r][c]} vs {m_b[r][c]}"
            )


def _compare_visibility(stage_a, stage_b, prim_path):
    """Assert that visibility matches between two stages."""
    vis_a = UsdGeom.Imageable(stage_a.GetPrimAtPath(prim_path)).GetVisibilityAttr().Get()
    vis_b = UsdGeom.Imageable(stage_b.GetPrimAtPath(prim_path)).GetVisibilityAttr().Get()
    assert vis_a == vis_b, f"{prim_path} visibility: {vis_a} != {vis_b}"


def _compare_stages(stage_a, stage_b, label_a="A", label_b="B"):
    """Compare all replicated content between two stages.

    Iterates prims on stage_a and verifies each exists with matching
    type, active state, transform, visibility, and authored attrs on stage_b.
    """
    for prim_a in Usd.PrimRange(stage_a.GetPseudoRoot(), Usd.PrimAllPrimsPredicate):
        prim_path = str(prim_a.GetPath())
        if prim_path == "/":
            continue

        prim_b = stage_b.GetPrimAtPath(prim_path)
        assert prim_b.IsValid(), f"{prim_path} missing on {label_b}"
        assert prim_a.GetTypeName() == prim_b.GetTypeName(), (
            f"{prim_path} type: {prim_a.GetTypeName()} vs {prim_b.GetTypeName()}"
        )
        assert prim_a.IsActive() == prim_b.IsActive(), (
            f"{prim_path} active: {prim_a.IsActive()} vs {prim_b.IsActive()}"
        )

        if not prim_a.IsActive():
            continue

        _compare_trs(stage_a, stage_b, prim_path)
        _compare_visibility(stage_a, stage_b, prim_path)

        for attr in prim_a.GetAttributes():
            name = attr.GetName()
            if (
                not name.startswith("xformOp:")
                and not name.startswith("primvars:")
                and name not in ("visibility", "xformOpOrder", "extent")
                and attr.IsAuthored()
            ):
                target_attr = prim_b.GetAttribute(name)
                if target_attr and target_attr.IsValid():
                    _compare_attr(stage_a, stage_b, prim_path, name)


# ---------------------------------------------------------------------------
# Scene builder
# ---------------------------------------------------------------------------


def _build_emitter_stage():
    """Build a rich emitter stage with multiple prim types and features."""
    stage = Usd.Stage.CreateInMemory()
    session = stage.GetSessionLayer()
    stage.SetEditTarget(Usd.EditTarget(session))

    stage.DefinePrim("/World", "Xform")

    sphere = stage.DefinePrim("/World/Sphere", "Sphere")
    sphere.GetAttribute("radius").Set(2.5)
    _, _, t, o, s = ensure_canonical_ops(stage, "/World/Sphere")
    t.Set(Gf.Vec3d(3.0, 0.0, 0.0))
    o.Set(Gf.Quatf(0.707, 0.707, 0.0, 0.0))
    s.Set(Gf.Vec3d(1.0, 1.0, 1.0))

    cyl = stage.DefinePrim("/World/Cylinder", "Cylinder")
    cyl.GetAttribute("radius").Set(0.5)
    cyl.GetAttribute("height").Set(3.0)
    _, _, t2, o2, s2 = ensure_canonical_ops(stage, "/World/Cylinder")
    t2.Set(Gf.Vec3d(-2.0, 1.0, 0.0))
    o2.Set(Gf.Quatf(1.0, 0.0, 0.0, 0.0))
    s2.Set(Gf.Vec3d(2.0, 2.0, 2.0))

    cone = stage.DefinePrim("/World/Cone", "Cone")
    cone.GetAttribute("radius").Set(1.0)
    cone.GetAttribute("height").Set(4.0)
    _, _, t3, o3, s3 = ensure_canonical_ops(stage, "/World/Cone")
    t3.Set(Gf.Vec3d(0.0, 5.0, -1.0))
    o3.Set(Gf.Quatf(0.5, 0.5, 0.5, 0.5))
    s3.Set(Gf.Vec3d(1.0, 2.0, 0.5))

    stage.DefinePrim("/World/Cube", "Cube")
    _, _, t4, o4, s4 = ensure_canonical_ops(stage, "/World/Cube")
    t4.Set(Gf.Vec3d(10.0, 0.0, 10.0))
    o4.Set(Gf.Quatf(1.0, 0.0, 0.0, 0.0))
    s4.Set(Gf.Vec3d(3.0, 3.0, 3.0))

    hidden = stage.DefinePrim("/World/Hidden", "Xform")
    _, _, t5, o5, s5 = ensure_canonical_ops(stage, "/World/Hidden")
    t5.Set(Gf.Vec3d(0.0, 0.0, 0.0))
    o5.Set(Gf.Quatf(1.0, 0.0, 0.0, 0.0))
    s5.Set(Gf.Vec3d(1.0, 1.0, 1.0))
    UsdGeom.Imageable(hidden).GetVisibilityAttr().Set("invisible")

    stage.DefinePrim("/World/Parent", "Xform")
    _, _, tp, op, sp = ensure_canonical_ops(stage, "/World/Parent")
    tp.Set(Gf.Vec3d(0.0, 0.0, 5.0))
    op.Set(Gf.Quatf(1.0, 0.0, 0.0, 0.0))
    sp.Set(Gf.Vec3d(1.0, 1.0, 1.0))

    child = stage.DefinePrim("/World/Parent/Child", "Sphere")
    child.GetAttribute("radius").Set(0.3)
    _, _, tc, oc, sc = ensure_canonical_ops(stage, "/World/Parent/Child")
    tc.Set(Gf.Vec3d(1.0, 0.0, 0.0))
    oc.Set(Gf.Quatf(1.0, 0.0, 0.0, 0.0))
    sc.Set(Gf.Vec3d(1.0, 1.0, 1.0))

    emitter = NoticeEmitter(stage)
    return stage, emitter


# ---------------------------------------------------------------------------
# Server simulation helper
# ---------------------------------------------------------------------------


def _server_process_and_replay(srv, events):
    """Simulate what the server does: apply_txn, assign seqs, log, then replay.

    Returns the list of event dicts as a receiver would see them.
    """
    srv.process_txn(events)

    # Replay full log (what a new receiver would get)
    rows = srv.store.get_all_asc()
    return [message_to_dict(r[1])["event"] for r in rows]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def srv(tmp_path):
    """Create a UsdSyncServer with a temp SQLite DB."""
    db = str(tmp_path / "parity.db")
    s = UsdSyncServer(log_path=db)
    yield s
    s.store.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStageParity:
    """True parity tests: emitter → server.apply_txn → log replay → receiver.apply_events."""

    def test_full_scene_three_way_parity(self, srv):
        """Full scene: emitter, server, and receiver stages have identical content."""
        emitter_stage, emitter = _build_emitter_stage()

        for prim in Usd.PrimRange(emitter_stage.GetPseudoRoot()):
            path = str(prim.GetPath())
            if path != "/":
                emitter.mark_dirty(path)
        events = emitter.build_events_for_dirty(include_matrices=False)

        replayed = _server_process_and_replay(srv, events)

        receiver_stage = Usd.Stage.CreateInMemory()
        apply_events(receiver_stage, replayed)

        _compare_stages(emitter_stage, srv.stage, "emitter", "server")
        _compare_stages(emitter_stage, receiver_stage, "emitter", "receiver")

    def test_incremental_updates_maintain_parity(self, srv):
        """After initial sync, incremental changes keep stages in sync."""
        emitter_stage, emitter = _build_emitter_stage()

        # Initial sync
        for prim in Usd.PrimRange(emitter_stage.GetPseudoRoot()):
            path = str(prim.GetPath())
            if path != "/":
                emitter.mark_dirty(path)
        initial_events = emitter.build_events_for_dirty(include_matrices=False)
        _server_process_and_replay(srv, initial_events)

        # Incremental: move sphere, change radius, hide cylinder
        xf = UsdGeom.Xformable(emitter_stage.GetPrimAtPath("/World/Sphere"))
        find_op(xf, "translate").Set(Gf.Vec3d(99.0, 0.0, 0.0))
        emitter_stage.GetPrimAtPath("/World/Sphere").GetAttribute("radius").Set(7.77)
        UsdGeom.Imageable(emitter_stage.GetPrimAtPath("/World/Cylinder")).GetVisibilityAttr().Set(
            "invisible"
        )

        update_events = emitter.build_events_for_dirty(include_matrices=False)
        replayed = _server_process_and_replay(srv, update_events)

        # Receiver gets full log replay (initial + update)
        receiver_stage = Usd.Stage.CreateInMemory()
        apply_events(receiver_stage, replayed)

        _compare_stages(emitter_stage, srv.stage, "emitter", "server")
        _compare_stages(emitter_stage, receiver_stage, "emitter", "receiver")

    def test_deactivation_parity(self, srv):
        """Deactivated prim is inactive on all three stages."""
        emitter_stage, emitter = _build_emitter_stage()

        for prim in Usd.PrimRange(emitter_stage.GetPseudoRoot()):
            path = str(prim.GetPath())
            if path != "/":
                emitter.mark_dirty(path)
        initial_events = emitter.build_events_for_dirty(include_matrices=False)
        _server_process_and_replay(srv, initial_events)

        emitter_stage.GetPrimAtPath("/World/Cone").SetActive(False)
        deact_events = emitter.build_events_for_dirty(include_matrices=False)
        replayed = _server_process_and_replay(srv, deact_events)

        receiver_stage = Usd.Stage.CreateInMemory()
        apply_events(receiver_stage, replayed)

        for label, stage in [("server", srv.stage), ("receiver", receiver_stage)]:
            prim = stage.GetPrimAtPath("/World/Cone")
            assert prim.IsValid(), f"Cone missing on {label}"
            assert not prim.IsActive(), f"Cone still active on {label}"

    def test_rename_parity(self, srv):
        """Renamed prim appears at new path on all three stages."""
        emitter_stage, emitter = _build_emitter_stage()

        for prim in Usd.PrimRange(emitter_stage.GetPseudoRoot()):
            path = str(prim.GetPath())
            if path != "/":
                emitter.mark_dirty(path)
        initial_events = emitter.build_events_for_dirty(include_matrices=False)
        _server_process_and_replay(srv, initial_events)

        prim = emitter_stage.GetPrimAtPath("/World/Cube")
        editor = Usd.NamespaceEditor(emitter_stage)
        editor.RenamePrim(prim, "BigCube")
        editor.ApplyEdits()

        rename_events = emitter.build_events_for_dirty(include_matrices=False)
        replayed = _server_process_and_replay(srv, rename_events)

        receiver_stage = Usd.Stage.CreateInMemory()
        apply_events(receiver_stage, replayed)

        for label, stage in [("server", srv.stage), ("receiver", receiver_stage)]:
            assert not stage.GetPrimAtPath("/World/Cube").IsValid(), (
                f"Old path still exists on {label}"
            )
            renamed = stage.GetPrimAtPath("/World/BigCube")
            assert renamed.IsValid(), f"New path missing on {label}"
            assert renamed.GetTypeName() == "Cube", f"Type wrong on {label}"

    def test_reference_parity(self, srv):
        """Reference arc and composed children replicate."""
        emitter_stage = Usd.Stage.CreateInMemory()
        session = emitter_stage.GetSessionLayer()
        emitter_stage.SetEditTarget(Usd.EditTarget(session))

        ref_stage = Usd.Stage.CreateInMemory()
        ref_stage.DefinePrim("/Model", "Xform")
        ref_stage.DefinePrim("/Model/Geom", "Cube")
        ref_id = ref_stage.GetRootLayer().identifier

        emitter_stage.DefinePrim("/World", "Xform")
        prim = emitter_stage.DefinePrim("/World/Furniture", "Xform")
        prim.GetReferences().AddReference(ref_id, "/Model")
        _, _, t, o, s = ensure_canonical_ops(emitter_stage, "/World/Furniture")
        t.Set(Gf.Vec3d(5.0, 0.0, 0.0))
        o.Set(Gf.Quatf(1.0, 0.0, 0.0, 0.0))
        s.Set(Gf.Vec3d(1.0, 1.0, 1.0))

        emitter = NoticeEmitter(emitter_stage)
        for p in Usd.PrimRange(emitter_stage.GetPseudoRoot()):
            path = str(p.GetPath())
            if path != "/":
                emitter.mark_dirty(path)
        events = emitter.build_events_for_dirty(include_matrices=False)

        replayed = _server_process_and_replay(srv, events)

        receiver_stage = Usd.Stage.CreateInMemory()
        apply_events(receiver_stage, replayed)

        for label, stage in [("server", srv.stage), ("receiver", receiver_stage)]:
            furniture = stage.GetPrimAtPath("/World/Furniture")
            assert furniture.IsValid(), f"Furniture missing on {label}"
            assert furniture.HasAuthoredReferences(), f"No refs on {label}"
            child = stage.GetPrimAtPath("/World/Furniture/Geom")
            assert child.IsValid(), f"Composed child missing on {label}"
            assert child.GetTypeName() == "Cube", f"Child type wrong on {label}"
            _compare_trs(emitter_stage, stage, "/World/Furniture")

    def test_variant_switch_parity(self, tmp_path):
        """Variant switch replicates selection and composed attr values."""
        fixture = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "fixtures", "variant_sphere.usda"
        )

        # Server loads the same base fixture
        db_path = str(tmp_path / "variant_parity.db")
        variant_srv = UsdSyncServer(base_usd_path=fixture, log_path=db_path)

        try:
            emitter_stage = Usd.Stage.Open(fixture)
            session = emitter_stage.GetSessionLayer()
            emitter_stage.SetEditTarget(Usd.EditTarget(session))
            emitter = NoticeEmitter(emitter_stage)

            emitter.mark_dirty("/World/Sphere")
            initial_events = emitter.build_events_for_dirty(include_matrices=False)
            _server_process_and_replay(variant_srv, initial_events)

            # Switch to "large"
            emitter_stage.GetPrimAtPath("/World/Sphere").GetVariantSets().GetVariantSet(
                "size"
            ).SetVariantSelection("large")
            switch_events = emitter.build_events_for_dirty(include_matrices=False)
            replayed = _server_process_and_replay(variant_srv, switch_events)

            # Receiver opens the same fixture
            receiver_stage = Usd.Stage.Open(fixture)
            apply_events(receiver_stage, replayed)

            for label, stage in [
                ("emitter", emitter_stage),
                ("server", variant_srv.stage),
                ("receiver", receiver_stage),
            ]:
                prim = stage.GetPrimAtPath("/World/Sphere")
                sel = prim.GetVariantSets().GetVariantSelection("size")
                assert sel == "large", f"{label} variant selection: {sel}"
        finally:
            variant_srv.store.close()

    def test_compacted_log_maintains_parity(self, srv):
        """After log compaction, a new receiver still gets correct state."""
        emitter_stage, emitter = _build_emitter_stage()

        # Initial sync
        for prim in Usd.PrimRange(emitter_stage.GetPseudoRoot()):
            path = str(prim.GetPath())
            if path != "/":
                emitter.mark_dirty(path)
        initial_events = emitter.build_events_for_dirty(include_matrices=False)
        _server_process_and_replay(srv, initial_events)

        # Several incremental updates
        xf = UsdGeom.Xformable(emitter_stage.GetPrimAtPath("/World/Sphere"))
        find_op(xf, "translate").Set(Gf.Vec3d(10.0, 0.0, 0.0))
        events1 = emitter.build_events_for_dirty(include_matrices=False)
        _server_process_and_replay(srv, events1)

        find_op(xf, "translate").Set(Gf.Vec3d(20.0, 0.0, 0.0))
        emitter_stage.GetPrimAtPath("/World/Sphere").GetAttribute("radius").Set(9.0)
        events2 = emitter.build_events_for_dirty(include_matrices=False)
        _server_process_and_replay(srv, events2)

        # Compact
        srv.compact_log()

        # New receiver replays compacted log
        rows = srv.store.get_all_asc()
        compacted_events = [message_to_dict(r[1])["event"] for r in rows]

        receiver_stage = Usd.Stage.CreateInMemory()
        apply_events(receiver_stage, compacted_events)

        _compare_stages(emitter_stage, srv.stage, "emitter", "server")
        _compare_stages(emitter_stage, receiver_stage, "emitter", "receiver")

    def test_shader_input_parity(self, srv):
        """Shader inputs on emitter, server, and receiver stages match."""
        from pxr import Sdf, UsdShade

        stage = Usd.Stage.CreateInMemory()
        session = stage.GetSessionLayer()
        stage.SetEditTarget(Usd.EditTarget(session))

        stage.DefinePrim("/World", "Xform")
        mat_prim = stage.DefinePrim("/World/Mat", "Material")
        shader_prim = stage.DefinePrim("/World/Mat/Surface", "Shader")
        shader = UsdShade.Shader(shader_prim)
        shader.CreateIdAttr("UsdPreviewSurface")
        shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(
            Gf.Vec3f(0.2, 0.8, 0.3),
        )
        shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(0.75)
        shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.4)

        mesh = stage.DefinePrim("/World/Mesh", "Sphere")
        UsdShade.MaterialBindingAPI.Apply(mesh)
        UsdShade.MaterialBindingAPI(mesh).Bind(UsdShade.Material(mat_prim))

        _, _, t, o, s = ensure_canonical_ops(stage, "/World/Mesh")
        t.Set(Gf.Vec3d(0, 0, 0))
        o.Set(Gf.Quatf(1, 0, 0, 0))
        s.Set(Gf.Vec3d(1, 1, 1))

        emitter = NoticeEmitter(stage)
        for prim in Usd.PrimRange(stage.GetPseudoRoot()):
            p = str(prim.GetPath())
            if p != "/":
                emitter.mark_dirty(p)
        events = emitter.build_events_for_dirty(include_matrices=False)

        shader_evts = [e for e in events if e.get("k") == "set_shader_input"]
        binding_evts = [e for e in events if e.get("k") == "set_material_binding"]
        assert shader_evts, "No shader events generated"
        assert binding_evts, "No binding events generated"
        surface_ev = next(e for e in shader_evts if e["prim"] == "/World/Mat/Surface")
        assert surface_ev["shader_id"] == "UsdPreviewSurface"
        assert "metallic" in surface_ev["inputs"]

        replayed = _server_process_and_replay(srv, events)
        receiver_stage = Usd.Stage.CreateInMemory()
        apply_events(receiver_stage, replayed)

        for label, target in [("server", srv.stage), ("receiver", receiver_stage)]:
            sh = UsdShade.Shader(target.GetPrimAtPath("/World/Mat/Surface"))
            assert sh.GetIdAttr().Get() == "UsdPreviewSurface"
            assert abs(sh.GetInput("metallic").Get() - 0.75) < 1e-6, f"metallic on {label}"
            assert abs(sh.GetInput("roughness").Get() - 0.4) < 1e-6, f"roughness on {label}"
            binding = UsdShade.MaterialBindingAPI(
                target.GetPrimAtPath("/World/Mesh"),
            ).GetDirectBinding()
            assert str(binding.GetMaterialPath()) == "/World/Mat", f"binding on {label}"

    def test_shader_input_update_parity(self, srv):
        """Incremental shader input changes maintain parity."""
        from pxr import Sdf, UsdShade

        stage = Usd.Stage.CreateInMemory()
        session = stage.GetSessionLayer()
        stage.SetEditTarget(Usd.EditTarget(session))

        stage.DefinePrim("/World", "Xform")
        stage.DefinePrim("/World/Mat", "Material")
        shader_prim = stage.DefinePrim("/World/Mat/Surface", "Shader")
        shader = UsdShade.Shader(shader_prim)
        shader.CreateIdAttr("UsdPreviewSurface")
        shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(0.0)
        shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.5)

        emitter = NoticeEmitter(stage)
        for prim in Usd.PrimRange(stage.GetPseudoRoot()):
            p = str(prim.GetPath())
            if p != "/":
                emitter.mark_dirty(p)
        _server_process_and_replay(srv, emitter.build_events_for_dirty(include_matrices=False))

        # Incremental: change metallic only
        shader.GetInput("metallic").Set(0.9)
        emitter.mark_dirty("/World/Mat/Surface")
        update = emitter.build_events_for_dirty(include_matrices=False)

        shader_updates = [e for e in update if e.get("k") == "set_shader_input"]
        assert len(shader_updates) == 1
        assert "metallic" in shader_updates[0]["inputs"]
        assert "roughness" not in shader_updates[0]["inputs"]

        replayed = _server_process_and_replay(srv, update)
        receiver_stage = Usd.Stage.CreateInMemory()
        apply_events(receiver_stage, replayed)

        for label, target in [
            ("emitter", stage),
            ("server", srv.stage),
            ("receiver", receiver_stage),
        ]:
            sh = UsdShade.Shader(target.GetPrimAtPath("/World/Mat/Surface"))
            assert abs(sh.GetInput("metallic").Get() - 0.9) < 1e-6, f"metallic on {label}"
            assert abs(sh.GetInput("roughness").Get() - 0.5) < 1e-6, f"roughness on {label}"
