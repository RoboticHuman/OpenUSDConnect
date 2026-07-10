"""Compose-from-empty parity test for MaterialX-style scenes.

Both sender and receiver stages start empty. The sender authors a complete
UsdShade network in-process; NoticeEmitter captures every change; events
flow through UsdSyncServer.apply_txn + log; the receiver replays the log
into a fresh in-memory stage. We then assert sender == server.stage ==
receiver stage, including UsdShade connection topology.

Op-coverage target (the protocol kinds we expect to see produced):

  - ensure_prim          (Xform, Scope, Mesh, Material, NodeGraph, Shader)
  - ensure_xform_ops     (canonical translate/orient/scale on the mesh)
  - set_xform_trs        (TRS values)
  - set_gprim_attrs      (mesh topology + primvars:st, primvars:normals)
  - set_shader_input     (typed inputs on Shader / NodeGraph / Material)
  - set_shader_connection (intra-NodeGraph wiring, NodeGraph interface
                           outputs forwarding internal shader outputs,
                           Material terminal output)
  - set_material_binding (mesh -> material direct binding)

The connection comparator follows UsdShade.GetConnectedSources() for every
Material / NodeGraph / Shader on both sides — `_compare_stages` in
test_stage_parity.py does not walk connections, so a green test there
does not prove connection parity. This test does.
"""

import pytest

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, Vt

    PXR_AVAILABLE = True
except ImportError:
    PXR_AVAILABLE = False

pytestmark = pytest.mark.skipif(not PXR_AVAILABLE, reason="pxr not available")

from openusdconnect.codec import message_to_dict
from openusdconnect.emitter import NoticeEmitter
from openusdconnect.event_apply import apply_events, ensure_canonical_ops
from openusdconnect.protocol_constants import (
    K_ENSURE_PRIM,
    K_ENSURE_XFORM_OPS,
    K_SET_GPRIM_ATTRS,
    K_SET_MATERIAL_BINDING,
    K_SET_CONNECTABLE_CONNECTION,
    K_SET_CONNECTABLE_INPUT,
    K_SET_XFORM_TRS,
)
from openusdconnect.server import UsdSyncServer


# ---------------------------------------------------------------------------
# Scene composition (authored on the sender)
# ---------------------------------------------------------------------------


def _compose_materialx_scene(stage: Usd.Stage) -> None:
    """Author a comprehensive MaterialX-style scene on `stage`.

    Mirrors the shape of a real MaterialX vehicle-part asset:
    mesh with primvars, Material with a NodeGraph wrapping a small
    shader graph, Standard Surface as the surface terminal.
    """
    # Hierarchy
    stage.DefinePrim("/World", "Xform")
    stage.DefinePrim("/World/Geom", "Xform")
    stage.DefinePrim("/World/Materials", "Scope")

    # ---- Mesh (topology + primvars) ----
    mesh_path = "/World/Geom/CarBody"
    mesh_prim = stage.DefinePrim(mesh_path, "Mesh")
    mesh = UsdGeom.Mesh(mesh_prim)
    mesh.CreatePointsAttr(
        [
            Gf.Vec3f(0, 0, 0),
            Gf.Vec3f(1, 0, 0),
            Gf.Vec3f(1, 1, 0),
            Gf.Vec3f(0, 1, 0),
        ]
    )
    mesh.CreateFaceVertexCountsAttr([4])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2, 3])

    pvapi = UsdGeom.PrimvarsAPI(mesh_prim)
    st = pvapi.CreatePrimvar("st", Sdf.ValueTypeNames.TexCoord2fArray, "vertex")
    st.Set(Vt.Vec2fArray([(0, 0), (1, 0), (1, 1), (0, 1)]))
    nrm = pvapi.CreatePrimvar("normals", Sdf.ValueTypeNames.Normal3fArray, "vertex")
    nrm.Set(Vt.Vec3fArray([(0, 0, 1)] * 4))

    # ---- Mesh xform ----
    _, _, t, o, s = ensure_canonical_ops(stage, mesh_path)
    t.Set(Gf.Vec3d(2.0, 0.0, 0.0))
    o.Set(Gf.Quatf(1.0, 0.0, 0.0, 0.0))
    s.Set(Gf.Vec3d(1.0, 1.0, 1.0))

    # ---- Material + NodeGraph + Shaders ----
    mat = UsdShade.Material.Define(stage, "/World/Materials/CarPaint")
    ng = UsdShade.NodeGraph.Define(stage, "/World/Materials/CarPaint/NG")

    # Source-of-color node inside the NodeGraph
    base_color_src = UsdShade.Shader.Define(stage, "/World/Materials/CarPaint/NG/BaseColor")
    base_color_src.CreateIdAttr("ND_constant_color3")
    base_color_src.CreateInput("value", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(0.6, 0.2, 0.1))
    base_color_out = base_color_src.CreateOutput("out", Sdf.ValueTypeNames.Color3f)

    # Tint multiplier inside the NodeGraph (exercises an intra-NodeGraph
    # shader-to-shader connection in addition to the interface forwarding)
    tint = UsdShade.Shader.Define(stage, "/World/Materials/CarPaint/NG/Tint")
    tint.CreateIdAttr("ND_multiply_color3")
    tint_in1 = tint.CreateInput("in1", Sdf.ValueTypeNames.Color3f)
    tint_in1.ConnectToSource(base_color_out)
    tint.CreateInput("in2", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(0.9, 0.9, 1.0))
    tint_out = tint.CreateOutput("out", Sdf.ValueTypeNames.Color3f)

    # NodeGraph interface output: forwards an internal shader output up to
    # the NodeGraph boundary. This is the "NodeGraph passthrough" case the
    # recent commit's resolve_nodegraph_connection cares about.
    ng_base_out = ng.CreateOutput("baseColor", Sdf.ValueTypeNames.Color3f)
    ng_base_out.ConnectToSource(tint_out)

    # Standard Surface terminal
    surf = UsdShade.Shader.Define(stage, "/World/Materials/CarPaint/MX_StdSurf")
    surf.CreateIdAttr("ND_standard_surface_surfaceshader")
    surf.CreateInput("base", Sdf.ValueTypeNames.Float).Set(1.0)
    surf.CreateInput("metalness", Sdf.ValueTypeNames.Float).Set(0.5)
    surf.CreateInput("specular_roughness", Sdf.ValueTypeNames.Float).Set(0.35)
    surf_base_color = surf.CreateInput("base_color", Sdf.ValueTypeNames.Color3f)
    surf_base_color.ConnectToSource(ng_base_out)
    surf_out = surf.CreateOutput("surface", Sdf.ValueTypeNames.Token)

    # Material terminal output (MaterialX renderContext)
    mat_out = mat.CreateSurfaceOutput("mtlx")
    mat_out.ConnectToSource(surf_out)

    # ---- Material binding ----
    UsdShade.MaterialBindingAPI.Apply(mesh_prim)
    UsdShade.MaterialBindingAPI(mesh_prim).Bind(mat)


# ---------------------------------------------------------------------------
# Comparators
# ---------------------------------------------------------------------------


def _values_close(a, b, eps=1e-5):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, float) or isinstance(b, float):
        return abs(float(a) - float(b)) < eps
    if hasattr(a, "__len__") and hasattr(b, "__len__"):
        if len(a) != len(b):
            return False
        return all(_values_close(x, y, eps) for x, y in zip(a, b, strict=True))
    return a == b


def _compare_shader_topology(stage_a, stage_b, label_a="A", label_b="B"):
    """Assert UsdShade connection topology parity for every connectable prim.

    Walks every Material / NodeGraph / Shader on stage_a and checks that
    stage_b has the same prim, the same info:id, the same input values
    (when not connected), and the same input and output connection
    sources (path + sourceName + sourceType).
    """
    for prim_a in Usd.PrimRange(stage_a.GetPseudoRoot()):
        if not (
            prim_a.IsA(UsdShade.Material)
            or prim_a.IsA(UsdShade.NodeGraph)
            or prim_a.IsA(UsdShade.Shader)
        ):
            continue
        prim_path = str(prim_a.GetPath())
        prim_b = stage_b.GetPrimAtPath(prim_path)
        assert prim_b.IsValid(), f"{prim_path} missing on {label_b}"
        assert prim_a.GetTypeName() == prim_b.GetTypeName(), (
            f"{prim_path} typeName: {prim_a.GetTypeName()} ({label_a}) vs "
            f"{prim_b.GetTypeName()} ({label_b})"
        )

        if prim_a.IsA(UsdShade.Shader):
            id_a = UsdShade.Shader(prim_a).GetIdAttr().Get()
            id_b = UsdShade.Shader(prim_b).GetIdAttr().Get()
            assert id_a == id_b, f"{prim_path} info:id: {id_a} ({label_a}) vs {id_b} ({label_b})"

        connectable_a = UsdShade.ConnectableAPI(prim_a)
        connectable_b = UsdShade.ConnectableAPI(prim_b)

        # Inputs: connection topology, then values when unconnected
        for inp_a in connectable_a.GetInputs():
            name = inp_a.GetBaseName()
            inp_b = connectable_b.GetInput(name)
            assert inp_b, f"{prim_path}.inputs:{name} missing on {label_b}"

            srcs_a, _ = inp_a.GetConnectedSources()
            srcs_b, _ = inp_b.GetConnectedSources()
            assert len(srcs_a) == len(srcs_b), (
                f"{prim_path}.inputs:{name} source count differs: "
                f"{len(srcs_a)} ({label_a}) vs {len(srcs_b)} ({label_b})"
            )
            for sa, sb in zip(srcs_a, srcs_b, strict=True):
                assert str(sa.source.GetPath()) == str(sb.source.GetPath()), (
                    f"{prim_path}.inputs:{name} source path: "
                    f"{sa.source.GetPath()} ({label_a}) vs "
                    f"{sb.source.GetPath()} ({label_b})"
                )
                assert sa.sourceName == sb.sourceName, (
                    f"{prim_path}.inputs:{name} sourceName: "
                    f"{sa.sourceName} ({label_a}) vs {sb.sourceName} ({label_b})"
                )
                assert sa.sourceType == sb.sourceType, (
                    f"{prim_path}.inputs:{name} sourceType: "
                    f"{sa.sourceType} ({label_a}) vs {sb.sourceType} ({label_b})"
                )

            if not srcs_a:
                va = inp_a.Get()
                vb = inp_b.Get()
                assert _values_close(va, vb), (
                    f"{prim_path}.inputs:{name} value: {va} ({label_a}) vs {vb} ({label_b})"
                )

        # Outputs: connections only (output values are produced, not authored)
        for outp_a in connectable_a.GetOutputs():
            name = outp_a.GetBaseName()
            srcs_a, _ = outp_a.GetConnectedSources()
            if not srcs_a:
                continue
            outp_b = connectable_b.GetOutput(name)
            assert outp_b, (
                f"{prim_path}.outputs:{name} missing on {label_b} (connected on {label_a})"
            )
            srcs_b, _ = outp_b.GetConnectedSources()
            assert len(srcs_a) == len(srcs_b), (
                f"{prim_path}.outputs:{name} source count differs: "
                f"{len(srcs_a)} ({label_a}) vs {len(srcs_b)} ({label_b})"
            )
            for sa, sb in zip(srcs_a, srcs_b, strict=True):
                assert str(sa.source.GetPath()) == str(sb.source.GetPath())
                assert sa.sourceName == sb.sourceName
                assert sa.sourceType == sb.sourceType


def _compare_mesh(stage_a, stage_b, mesh_path):
    mesh_a = UsdGeom.Mesh(stage_a.GetPrimAtPath(mesh_path))
    mesh_b = UsdGeom.Mesh(stage_b.GetPrimAtPath(mesh_path))
    for getter in ("GetPointsAttr", "GetFaceVertexCountsAttr", "GetFaceVertexIndicesAttr"):
        va = list(getattr(mesh_a, getter)().Get() or [])
        vb = list(getattr(mesh_b, getter)().Get() or [])
        assert _values_close(va, vb), f"{mesh_path} {getter} differs: {va} vs {vb}"

    pv_a = {pv.GetBaseName(): pv for pv in UsdGeom.PrimvarsAPI(mesh_a).GetPrimvars()}
    pv_b = {pv.GetBaseName(): pv for pv in UsdGeom.PrimvarsAPI(mesh_b).GetPrimvars()}
    assert set(pv_a) == set(pv_b), f"{mesh_path} primvar names: {set(pv_a)} vs {set(pv_b)}"
    for name, p_a in pv_a.items():
        p_b = pv_b[name]
        assert _values_close(list(p_a.Get() or []), list(p_b.Get() or [])), (
            f"{mesh_path}.primvars:{name} values differ"
        )
        assert p_a.GetInterpolation() == p_b.GetInterpolation(), (
            f"{mesh_path}.primvars:{name} interpolation: "
            f"{p_a.GetInterpolation()} vs {p_b.GetInterpolation()}"
        )


def _compare_material_binding(stage_a, stage_b, mesh_path):
    bind_a = UsdShade.MaterialBindingAPI(stage_a.GetPrimAtPath(mesh_path)).GetDirectBinding()
    bind_b = UsdShade.MaterialBindingAPI(stage_b.GetPrimAtPath(mesh_path)).GetDirectBinding()
    assert str(bind_a.GetMaterialPath()) == str(bind_b.GetMaterialPath()), (
        f"{mesh_path} material binding: {bind_a.GetMaterialPath()} vs {bind_b.GetMaterialPath()}"
    )


def _compare_trs(stage_a, stage_b, prim_path, eps=1e-6):
    m_a = UsdGeom.Xformable(stage_a.GetPrimAtPath(prim_path)).GetLocalTransformation(
        Usd.TimeCode.Default()
    )
    m_b = UsdGeom.Xformable(stage_b.GetPrimAtPath(prim_path)).GetLocalTransformation(
        Usd.TimeCode.Default()
    )
    if isinstance(m_a, tuple):
        m_a = m_a[0]
    if isinstance(m_b, tuple):
        m_b = m_b[0]
    for r in range(4):
        for c in range(4):
            assert abs(m_a[r][c] - m_b[r][c]) < eps, (
                f"{prim_path}[{r}][{c}]: {m_a[r][c]} vs {m_b[r][c]}"
            )


# ---------------------------------------------------------------------------
# Server helper (same shape as test_stage_parity)
# ---------------------------------------------------------------------------


def _server_process_and_replay(srv, events):
    srv.process_txn(events)
    rows = srv.store.get_all_asc()
    return [message_to_dict(r[1])["event"] for r in rows]


@pytest.fixture
def srv(tmp_path):
    db = str(tmp_path / "materialx_parity.db")
    s = UsdSyncServer(log_path=db)
    yield s
    s.store.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMaterialXComposeParity:
    """Both sender and receiver start empty; sender authors a complete
    MaterialX-style scene; receiver must reproduce it from events alone."""

    def test_receiver_matches_sender_after_compose_from_empty(self, srv):
        # Sender: empty in-memory stage, edits land on the session layer
        # so the emitter sees them as authored opinions.
        sender_stage = Usd.Stage.CreateInMemory()
        sender_stage.SetEditTarget(Usd.EditTarget(sender_stage.GetSessionLayer()))
        emitter = NoticeEmitter(sender_stage)

        # Author the whole scene
        _compose_materialx_scene(sender_stage)

        # Mark everything dirty and emit
        events = emitter.snapshot_events()
        assert events, "Emitter produced no events from a freshly authored scene"

        # Server applies + logs; receiver replays the log into a fresh stage
        replayed = _server_process_and_replay(srv, events)
        receiver_stage = Usd.Stage.CreateInMemory()
        apply_events(receiver_stage, replayed)

        mesh_path = "/World/Geom/CarBody"

        # Three-way parity: sender vs server.stage vs receiver
        for label_b, stage_b in [("server", srv.stage), ("receiver", receiver_stage)]:
            _compare_mesh(sender_stage, stage_b, mesh_path)
            _compare_trs(sender_stage, stage_b, mesh_path)
            _compare_material_binding(sender_stage, stage_b, mesh_path)
            _compare_shader_topology(sender_stage, stage_b, "sender", label_b)

    def test_receiver_matches_sender_under_shuffled_event_order(self, srv):
        """apply_events must be order-invariant within a transaction.

        The structural pass in apply_events applies prim-creating kinds first, so
        arbitrary caller-provided order must yield the same receiver stage
        as the natural emitted order. Shuffles deterministically so any
        failure is reproducible from the seed.

        This test is the regression net for the class of bug where a new
        event kind has an implicit ordering dependency that the emitter
        happens to honor but the protocol contract does not guarantee.
        """
        import random

        sender_stage = Usd.Stage.CreateInMemory()
        sender_stage.SetEditTarget(Usd.EditTarget(sender_stage.GetSessionLayer()))
        emitter = NoticeEmitter(sender_stage)
        _compose_materialx_scene(sender_stage)

        events = emitter.snapshot_events()

        rng = random.Random(0x12345678)
        rng.shuffle(events)

        replayed = _server_process_and_replay(srv, events)
        receiver_stage = Usd.Stage.CreateInMemory()
        apply_events(receiver_stage, replayed)

        mesh_path = "/World/Geom/CarBody"
        _compare_mesh(sender_stage, receiver_stage, mesh_path)
        _compare_material_binding(sender_stage, receiver_stage, mesh_path)
        _compare_shader_topology(sender_stage, receiver_stage, "sender", "receiver")

    def test_event_kind_coverage(self):
        """Authoring this scene must produce every key event kind we expect.

        This is a separate test from the parity check so a missing event
        kind reports as a coverage failure rather than masquerading as a
        topology mismatch downstream.
        """
        sender_stage = Usd.Stage.CreateInMemory()
        sender_stage.SetEditTarget(Usd.EditTarget(sender_stage.GetSessionLayer()))
        emitter = NoticeEmitter(sender_stage)

        _compose_materialx_scene(sender_stage)

        events = emitter.snapshot_events()

        kinds = {e["k"] for e in events}
        expected = {
            K_ENSURE_PRIM,
            K_ENSURE_XFORM_OPS,
            K_SET_XFORM_TRS,
            K_SET_GPRIM_ATTRS,
            K_SET_CONNECTABLE_INPUT,
            K_SET_CONNECTABLE_CONNECTION,
            K_SET_MATERIAL_BINDING,
        }
        missing = expected - kinds
        assert not missing, (
            f"Missing expected event kinds: {sorted(missing)}.\nGot: {sorted(kinds)}"
        )

    def test_nodegraph_interface_output_connection_survives_roundtrip(self, srv):
        """Targeted check: the NodeGraph's interface output forwards to the
        internal Tint shader's output on both sides, with matching sourceType.

        This is the exact case that motivated resolve_nodegraph_connection
        and is the most fragile part of the shader-connection wire format.
        """
        sender_stage = Usd.Stage.CreateInMemory()
        sender_stage.SetEditTarget(Usd.EditTarget(sender_stage.GetSessionLayer()))
        emitter = NoticeEmitter(sender_stage)
        _compose_materialx_scene(sender_stage)

        events = emitter.snapshot_events()
        replayed = _server_process_and_replay(srv, events)
        receiver_stage = Usd.Stage.CreateInMemory()
        apply_events(receiver_stage, replayed)

        for label, stage in [
            ("sender", sender_stage),
            ("server", srv.stage),
            ("receiver", receiver_stage),
        ]:
            ng = UsdShade.NodeGraph(stage.GetPrimAtPath("/World/Materials/CarPaint/NG"))
            assert ng, f"NodeGraph missing on {label}"
            ng_out = ng.GetOutput("baseColor")
            assert ng_out, f"NodeGraph baseColor output missing on {label}"
            srcs, _ = ng_out.GetConnectedSources()
            assert len(srcs) == 1, (
                f"NodeGraph baseColor expected 1 source on {label}, got {len(srcs)}"
            )
            src = srcs[0]
            assert str(src.source.GetPath()) == "/World/Materials/CarPaint/NG/Tint", (
                f"NodeGraph baseColor source path on {label}: {src.source.GetPath()}"
            )
            assert src.sourceName == "out", (
                f"NodeGraph baseColor sourceName on {label}: {src.sourceName}"
            )
            assert src.sourceType == UsdShade.AttributeType.Output, (
                f"NodeGraph baseColor sourceType on {label}: {src.sourceType}"
            )

    def test_nested_nodegraphs_round_trip(self, srv):
        """Material -> NG_outer -> NG_inner -> Shader survives the wire.

        Three-level interface forwarding (Material.surface -> NG_outer.surface
        -> NG_inner.surface -> Shader.surface) plus an input value plumbed
        down the same path (Material.tint -> NG_outer.tint -> NG_inner.tint
        -> Shader.base_color). Pins that ConnectableAPI's uniform handling
        actually works at depth > 2.
        """
        sender = Usd.Stage.CreateInMemory()
        material = UsdShade.Material.Define(sender, "/M")
        ng_outer = UsdShade.NodeGraph.Define(sender, "/M/NG_outer")
        ng_inner = UsdShade.NodeGraph.Define(sender, "/M/NG_outer/NG_inner")
        shader = UsdShade.Shader.Define(sender, "/M/NG_outer/NG_inner/Surf")
        shader.CreateIdAttr("ND_standard_surface_surfaceshader")

        # Surface output forwarding: Material <- NG_outer <- NG_inner <- Shader.
        shader_out = shader.CreateOutput("surface", Sdf.ValueTypeNames.Token)
        inner_out = ng_inner.CreateOutput("surface", Sdf.ValueTypeNames.Token)
        outer_out = ng_outer.CreateOutput("surface", Sdf.ValueTypeNames.Token)
        mat_out = material.CreateSurfaceOutput()
        inner_out.ConnectToSource(shader_out)
        outer_out.ConnectToSource(inner_out)
        mat_out.ConnectToSource(outer_out)

        # Value plumbed top-down: Material.tint -> NG_outer.tint
        # -> NG_inner.tint -> Shader.base_color.
        mat_tint = material.CreateInput("tint", Sdf.ValueTypeNames.Color3f)
        mat_tint.Set(Gf.Vec3f(0.7, 0.3, 0.1))
        outer_tint = ng_outer.CreateInput("tint", Sdf.ValueTypeNames.Color3f)
        inner_tint = ng_inner.CreateInput("tint", Sdf.ValueTypeNames.Color3f)
        shader_base = shader.CreateInput("base_color", Sdf.ValueTypeNames.Color3f)
        outer_tint.ConnectToSource(mat_tint)
        inner_tint.ConnectToSource(outer_tint)
        shader_base.ConnectToSource(inner_tint)

        emitter = NoticeEmitter(sender)
        replayed = _server_process_and_replay(srv, emitter.snapshot_events())
        receiver = Usd.Stage.CreateInMemory()
        apply_events(receiver, replayed)

        def _src(stage, path, input_name):
            connectable = UsdShade.ConnectableAPI(stage.GetPrimAtPath(path))
            srcs, _ = connectable.GetInput(input_name).GetConnectedSources()
            return srcs

        def _out_src(stage, path, output_name):
            connectable = UsdShade.ConnectableAPI(stage.GetPrimAtPath(path))
            srcs, _ = connectable.GetOutput(output_name).GetConnectedSources()
            return srcs

        for label, stage in (
            ("sender", sender), ("server", srv.stage), ("receiver", receiver),
        ):
            mat_srcs = _out_src(stage, "/M", "surface")
            assert len(mat_srcs) == 1, label
            assert str(mat_srcs[0].source.GetPath()) == "/M/NG_outer", label
            outer_srcs = _out_src(stage, "/M/NG_outer", "surface")
            assert len(outer_srcs) == 1, label
            assert str(outer_srcs[0].source.GetPath()) == "/M/NG_outer/NG_inner", label
            inner_srcs = _out_src(stage, "/M/NG_outer/NG_inner", "surface")
            assert len(inner_srcs) == 1, label
            assert str(inner_srcs[0].source.GetPath()) == "/M/NG_outer/NG_inner/Surf", label

            outer_in = _src(stage, "/M/NG_outer", "tint")
            assert len(outer_in) == 1 and str(outer_in[0].source.GetPath()) == "/M", label
            inner_in = _src(stage, "/M/NG_outer/NG_inner", "tint")
            assert len(inner_in) == 1, label
            assert str(inner_in[0].source.GetPath()) == "/M/NG_outer", label
            shader_in = _src(stage, "/M/NG_outer/NG_inner/Surf", "base_color")
            assert len(shader_in) == 1, label
            assert str(shader_in[0].source.GetPath()) == "/M/NG_outer/NG_inner", label

            tint_attr = stage.GetPrimAtPath("/M").GetAttribute("inputs:tint")
            assert tint_attr.IsValid() and tint_attr.Get() == Gf.Vec3f(0.7, 0.3, 0.1), label

    def test_time_sampled_connectable_input_round_trip(self, srv):
        """A Shader input authored as time samples must replicate as
        per-(attr, time) set_connectable_input events and apply as time
        samples on the receiving stage."""
        sender = Usd.Stage.CreateInMemory()
        emitter = NoticeEmitter(sender)
        material = UsdShade.Material.Define(sender, "/M")
        shader = UsdShade.Shader.Define(sender, "/M/Surf")
        shader.CreateIdAttr("UsdPreviewSurface")
        shader_out = shader.CreateOutput("surface", Sdf.ValueTypeNames.Token)
        material.CreateSurfaceOutput().ConnectToSource(shader_out)
        diffuse = shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f)
        diffuse.Set(Gf.Vec3f(0.1, 0.2, 0.3), Usd.TimeCode(1.0))
        diffuse.Set(Gf.Vec3f(0.4, 0.5, 0.6), Usd.TimeCode(2.0))
        diffuse.Set(Gf.Vec3f(0.7, 0.8, 0.9), Usd.TimeCode(3.0))

        events = emitter.build_events_for_dirty()
        timed = [
            e for e in events
            if e.get("k") == K_SET_CONNECTABLE_INPUT and "time" in e
        ]
        assert {e["time"] for e in timed} == {1.0, 2.0, 3.0}, (
            f"expected three timed events for the three samples; got {timed}"
        )

        replayed = _server_process_and_replay(srv, events)
        receiver = Usd.Stage.CreateInMemory()
        apply_events(receiver, replayed)

        for label, stage in (
            ("sender", sender), ("server", srv.stage), ("receiver", receiver),
        ):
            attr = stage.GetPrimAtPath("/M/Surf").GetAttribute("inputs:diffuseColor")
            assert attr.IsValid(), f"{label}: shader input missing"
            samples = attr.GetTimeSamples()
            assert samples == [1.0, 2.0, 3.0], f"{label}: samples {samples}"
            assert attr.Get(Usd.TimeCode(2.0)) == Gf.Vec3f(0.4, 0.5, 0.6), label

        # Second emit cycle must be silent (no spurious re-emission).
        assert emitter.build_events_for_dirty() == [], (
            "second emit cycle re-emitted unchanged time samples"
        )

    def test_exotic_sdf_type_inputs_round_trip(self, srv):
        """Less-common Sdf types on shader inputs: matrix4d for transform
        nodes, token (singular), and a token[]/int[] pair. Pins what we
        actually carry across the wire for atypical MaterialX inputs."""
        from pxr import Vt

        sender = Usd.Stage.CreateInMemory()
        emitter = NoticeEmitter(sender)
        material = UsdShade.Material.Define(sender, "/M")
        shader = UsdShade.Shader.Define(sender, "/M/S")
        shader.CreateIdAttr("ND_transformmatrix")
        shader.CreateOutput("surface", Sdf.ValueTypeNames.Token)
        material.CreateSurfaceOutput().ConnectToSource(shader.GetOutput("surface"))

        # matrix4d (e.g. an ND_transformmatrix input)
        xform = Gf.Matrix4d(1.0)
        xform.SetTranslateOnly(Gf.Vec3d(2.0, 3.0, 4.0))
        m_in = shader.CreateInput("xform", Sdf.ValueTypeNames.Matrix4d)
        m_in.Set(xform)

        # singular token
        tok_in = shader.CreateInput("mode", Sdf.ValueTypeNames.Token)
        tok_in.Set("clamp")

        # int[] (rare but appears on procedural / selector nodes)
        ints_in = shader.CreateInput("counts", Sdf.ValueTypeNames.IntArray)
        ints_in.Set(Vt.IntArray([2, 5, 11]))

        # token[] (rare; MaterialX uses string[] for some selectors)
        toks_in = shader.CreateInput("names", Sdf.ValueTypeNames.TokenArray)
        toks_in.Set(Vt.TokenArray(["a", "b", "c"]))

        replayed = _server_process_and_replay(
            srv, emitter.build_events_for_dirty()
        )
        receiver = Usd.Stage.CreateInMemory()
        apply_events(receiver, replayed)

        for label, stage in (
            ("sender", sender), ("server", srv.stage), ("receiver", receiver),
        ):
            shader = stage.GetPrimAtPath("/M/S")
            assert shader.IsValid(), f"{label}: shader missing"

            m_attr = shader.GetAttribute("inputs:xform")
            assert m_attr.IsValid() and m_attr.IsAuthored(), (
                f"{label}: matrix4d input not authored"
            )
            m_val = m_attr.Get()
            assert m_val is not None, f"{label}: matrix4d input returned None"
            for r in range(4):
                for c in range(4):
                    assert abs(m_val[r][c] - xform[r][c]) < 1e-9, (
                        f"{label}: matrix4d[{r}][{c}] mismatch"
                    )

            tok_attr = shader.GetAttribute("inputs:mode")
            assert tok_attr.Get() == "clamp", f"{label}: token mismatch"

            ints_attr = shader.GetAttribute("inputs:counts")
            assert list(ints_attr.Get()) == [2, 5, 11], f"{label}: int[] mismatch"

            toks_attr = shader.GetAttribute("inputs:names")
            assert list(toks_attr.Get()) == ["a", "b", "c"], (
                f"{label}: token[] mismatch"
            )


    def test_variant_scoped_material_binding_follows_selection(self, srv):
        """Material bindings authored inside a variant scope must follow the
        active selection across the wire. A variant flip is a composition
        resync of the variant set's owner path only; the emitter must
        re-walk the subtree so the moved binding and the now-composed
        material content both reach the receiver.
        """
        sender = Usd.Stage.CreateInMemory()
        world = sender.DefinePrim("/World", "Xform")
        mesh = UsdGeom.Cube.Define(sender, "/World/Mesh")
        sender.DefinePrim("/World/Materials", "Scope")
        vset = world.GetVariantSets().AddVariantSet("lookVariant")
        for variant_name, color in (("Red", (1.0, 0.0, 0.0)),
                                    ("Blue", (0.0, 0.0, 1.0))):
            vset.AddVariant(variant_name)
            vset.SetVariantSelection(variant_name)
            with vset.GetVariantEditContext():
                mp = f"/World/Materials/{variant_name}"
                mat = UsdShade.Material.Define(sender, mp)
                shader = UsdShade.Shader.Define(sender, f"{mp}/Surf")
                shader.CreateIdAttr("UsdPreviewSurface")
                shader.CreateInput(
                    "diffuseColor", Sdf.ValueTypeNames.Color3f
                ).Set(color)
                surface_out = shader.CreateOutput(
                    "surface", Sdf.ValueTypeNames.Token
                )
                mat.CreateSurfaceOutput().ConnectToSource(surface_out)
                UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(mat)
        vset.SetVariantSelection("Red")

        emitter = NoticeEmitter(sender)
        events_initial = emitter.snapshot_events()
        replayed_initial = _server_process_and_replay(srv, events_initial)
        receiver = Usd.Stage.CreateInMemory()
        apply_events(receiver, replayed_initial)

        mesh_r = receiver.GetPrimAtPath("/World/Mesh")
        rel = mesh_r.GetRelationship("material:binding")
        assert [str(t) for t in rel.GetTargets()] == ["/World/Materials/Red"]

        vset.SetVariantSelection("Blue")
        events_flip = emitter.build_events_for_dirty()
        replayed_flip = _server_process_and_replay(srv, events_flip)
        apply_events(receiver, replayed_flip)

        rel = receiver.GetPrimAtPath("/World/Mesh").GetRelationship(
            "material:binding"
        )
        assert [str(t) for t in rel.GetTargets()] == ["/World/Materials/Blue"]
        blue_shader = UsdShade.Shader(
            receiver.GetPrimAtPath("/World/Materials/Blue/Surf")
        )
        assert blue_shader.GetIdAttr().Get() == "UsdPreviewSurface"
        diffuse = blue_shader.GetInput("diffuseColor").Get()
        assert tuple(diffuse) == (0.0, 0.0, 1.0)


    def test_material_purpose_bindings_round_trip(self, srv):
        """Per-purpose bindings (allPurpose + preview + full) must each
        replicate through their purpose-suffixed relationship slot. The
        receiver should reconstruct the same purpose-resolution behavior
        via ``ComputeBoundMaterial(purpose)``.
        """
        sender = Usd.Stage.CreateInMemory()
        sphere = sender.DefinePrim("/World/Sphere", "Sphere")
        for name in ("MatAll", "MatPreview", "MatFull"):
            mat = UsdShade.Material.Define(sender, f"/World/Materials/{name}")
            shader = UsdShade.Shader.Define(sender, f"/World/Materials/{name}/S")
            shader.CreateIdAttr("UsdPreviewSurface")
            mat.CreateSurfaceOutput().ConnectToSource(
                shader.CreateOutput("surface", Sdf.ValueTypeNames.Token)
            )
        api = UsdShade.MaterialBindingAPI.Apply(sphere)
        api.Bind(
            UsdShade.Material(sender.GetPrimAtPath("/World/Materials/MatAll")),
        )
        api.Bind(
            UsdShade.Material(sender.GetPrimAtPath("/World/Materials/MatPreview")),
            materialPurpose="preview",
        )
        api.Bind(
            UsdShade.Material(sender.GetPrimAtPath("/World/Materials/MatFull")),
            materialPurpose="full",
        )

        emitter = NoticeEmitter(sender)
        events = emitter.snapshot_events()
        binding_events = [e for e in events if e["k"] == K_SET_MATERIAL_BINDING]
        by_purpose = {
            e.get("material_purpose", ""): e["material_path"] for e in binding_events
        }
        assert by_purpose == {
            "": "/World/Materials/MatAll",
            "preview": "/World/Materials/MatPreview",
            "full": "/World/Materials/MatFull",
        }

        replayed = _server_process_and_replay(srv, events)
        receiver = Usd.Stage.CreateInMemory()
        apply_events(receiver, replayed)

        for stage_label, stage in (("server", srv.stage), ("receiver", receiver)):
            sphere_r = stage.GetPrimAtPath("/World/Sphere")
            api_r = UsdShade.MaterialBindingAPI(sphere_r)
            for purpose, expected in (
                ("", "/World/Materials/MatAll"),
                ("preview", "/World/Materials/MatPreview"),
                ("full", "/World/Materials/MatFull"),
            ):
                bound = api_r.ComputeBoundMaterial(purpose)[0]
                assert bound and str(bound.GetPath()) == expected, (
                    f"{stage_label}: purpose={purpose!r} expected {expected}, "
                    f"got {bound.GetPath() if bound else None}"
                )

        # Rebind preview to a different material; only that one event should fly.
        api.Bind(
            UsdShade.Material(sender.GetPrimAtPath("/World/Materials/MatAll")),
            materialPurpose="preview",
        )
        ev2 = emitter.build_events_for_dirty()
        bind_ev2 = [e for e in ev2 if e["k"] == K_SET_MATERIAL_BINDING]
        assert len(bind_ev2) == 1
        assert bind_ev2[0].get("material_purpose") == "preview"
        assert bind_ev2[0]["material_path"] == "/World/Materials/MatAll"

        replayed2 = _server_process_and_replay(srv, ev2)
        apply_events(receiver, replayed2)
        bound_preview = UsdShade.MaterialBindingAPI(
            receiver.GetPrimAtPath("/World/Sphere")
        ).ComputeBoundMaterial("preview")[0]
        assert str(bound_preview.GetPath()) == "/World/Materials/MatAll"


# ---------------------------------------------------------------------------
# Asset-driven test: open a real flattened MaterialX asset on the sender
# and verify the receiver can reproduce it from events alone.
# ---------------------------------------------------------------------------


def _compare_prim_tree(stage_a, stage_b, label_a="A", label_b="B"):
    """Every active prim on stage_a must exist on stage_b with the same
    typeName (including the untyped `def "Materials"` case)."""
    for prim_a in Usd.PrimRange(stage_a.GetPseudoRoot(), Usd.PrimAllPrimsPredicate):
        prim_path = str(prim_a.GetPath())
        if prim_path == "/":
            continue
        prim_b = stage_b.GetPrimAtPath(prim_path)
        assert prim_b.IsValid(), f"{prim_path} missing on {label_b}"
        assert str(prim_a.GetTypeName()) == str(prim_b.GetTypeName()), (
            f"{prim_path} typeName: "
            f"{prim_a.GetTypeName()!r} ({label_a}) vs "
            f"{prim_b.GetTypeName()!r} ({label_b})"
        )
        assert prim_a.IsActive() == prim_b.IsActive(), (
            f"{prim_path} active: {prim_a.IsActive()} vs {prim_b.IsActive()}"
        )


# Resolve the asset path relative to the repo root regardless of where pytest
# is invoked from.
_REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
_BASIC_MTLX_ASSET = _REPO_ROOT / "assets" / "test_assets" / "MaterialXTest" / "basic_flatten.usda"
_SHADERBALL_ENV_ASSET = (
    _REPO_ROOT / "assets" / "full_assets" / "StandardShaderBall"
    / "layers" / "environment.usda"
)


@pytest.mark.skipif(not _BASIC_MTLX_ASSET.exists(), reason="basic_flatten.usda missing")
class TestAssetReproduce:
    """Open a real flattened MaterialX asset on the sender, replay events
    onto a fresh receiver, and assert the receiver reproduces the asset.

    The asset has:
      - /MaterialX (Scope)
      - /MaterialX/Materials (untyped def — round-trips as Xform, see
        _KNOWN_TYPENAME_DOWNGRADES)
      - /MaterialX/Materials/USD_Plastic (Material) with terminal output
        connection to its nested Shader's `outputs:surface`
      - /MaterialX/Materials/USD_Plastic/ND_UsdPreviewSurface_surfaceshader
        (Shader) whose ~14 inputs each connect upward to the Material's
        interface inputs (the "Material-as-interface" forwarding pattern)
      - /MaterialX/Shaders (untyped def)
      - /MaterialX/Shaders/ND_UsdPreviewSurface_surfaceshader (Shader)
      - /sphere (Sphere) with material binding to USD_Plastic
    """

    def test_basic_flatten_asset_roundtrip(self, srv):
        sender_stage = Usd.Stage.Open(str(_BASIC_MTLX_ASSET))
        emitter = NoticeEmitter(sender_stage)

        events = emitter.snapshot_events()
        assert events, "Emitter produced no events from the asset"

        replayed = _server_process_and_replay(srv, events)
        receiver_stage = Usd.Stage.CreateInMemory()
        apply_events(receiver_stage, replayed)

        for label_b, stage_b in [("server", srv.stage), ("receiver", receiver_stage)]:
            _compare_prim_tree(sender_stage, stage_b, "sender", label_b)
            _compare_shader_topology(sender_stage, stage_b, "sender", label_b)
            _compare_material_binding(sender_stage, stage_b, "/sphere")

    def test_material_interface_forwarding_preserved(self, srv):
        """The Shader's `inputs:diffuseColor.connect` points up to
        `</MaterialX/Materials/USD_Plastic.inputs:diffuseColor>` — an
        Input-typed connection (not Output). This is the dual of the
        NodeGraph-output forwarding case and the sourceType must round-trip.
        """
        sender_stage = Usd.Stage.Open(str(_BASIC_MTLX_ASSET))
        emitter = NoticeEmitter(sender_stage)
        for prim in Usd.PrimRange(sender_stage.GetPseudoRoot()):
            path = str(prim.GetPath())
            if path != "/":
                emitter.mark_dirty(path)
        replayed = _server_process_and_replay(
            srv, emitter.build_events_for_dirty()
        )
        receiver_stage = Usd.Stage.CreateInMemory()
        apply_events(receiver_stage, replayed)

        shader_path = "/MaterialX/Materials/USD_Plastic/ND_UsdPreviewSurface_surfaceshader"
        material_path = "/MaterialX/Materials/USD_Plastic"

        for label, stage in [
            ("sender", sender_stage),
            ("server", srv.stage),
            ("receiver", receiver_stage),
        ]:
            shader = UsdShade.Shader(stage.GetPrimAtPath(shader_path))
            assert shader, f"Shader missing on {label}"
            diffuse_in = shader.GetInput("diffuseColor")
            assert diffuse_in, f"Shader.inputs:diffuseColor missing on {label}"
            srcs, _ = diffuse_in.GetConnectedSources()
            assert len(srcs) == 1, f"diffuseColor sources on {label}: {len(srcs)} (expected 1)"
            src = srcs[0]
            assert str(src.source.GetPath()) == material_path, (
                f"diffuseColor source path on {label}: {src.source.GetPath()}"
            )
            assert src.sourceName == "diffuseColor", (
                f"diffuseColor sourceName on {label}: {src.sourceName}"
            )
            # Input-typed source (not Output) — Material interface forwarding
            assert src.sourceType == UsdShade.AttributeType.Input, (
                f"diffuseColor sourceType on {label}: {src.sourceType} (expected Input)"
            )


@pytest.mark.skipif(
    not _SHADERBALL_ENV_ASSET.exists(),
    reason="StandardShaderBall environment.usda missing",
)
class TestPurposeBindingAsset:
    """Drive the per-purpose binding code paths off a real asset.

    ``StandardShaderBall/layers/environment.usda`` authors
    ``material:binding:full`` (the ``"full"`` material purpose) on every
    wall mesh of the studio enclosure. Opening it on the sender and
    replaying the events must reproduce all five purpose-suffixed bindings
    on the receiver, resolvable via ``ComputeBoundMaterial("full")``.
    """

    _WALL_BINDINGS = {
        "/standard_shader_ball_scene/box/back":
            "/standard_shader_ball_scene/materials/back",
        "/standard_shader_ball_scene/box/right":
            "/standard_shader_ball_scene/materials/right",
        "/standard_shader_ball_scene/box/top":
            "/standard_shader_ball_scene/materials/top",
        "/standard_shader_ball_scene/box/front":
            "/standard_shader_ball_scene/materials/front",
        "/standard_shader_ball_scene/box/left":
            "/standard_shader_ball_scene/materials/left",
    }

    def test_full_purpose_bindings_round_trip(self, srv):
        sender = Usd.Stage.Open(str(_SHADERBALL_ENV_ASSET))
        emitter = NoticeEmitter(sender)
        events = emitter.snapshot_events()

        full_events = [
            e for e in events
            if e["k"] == K_SET_MATERIAL_BINDING
            and e.get("material_purpose") == "full"
        ]
        assert len(full_events) == len(self._WALL_BINDINGS), (
            f"expected {len(self._WALL_BINDINGS)} full-purpose bind events, "
            f"got {len(full_events)}"
        )
        emitted = {e["prim"]: e["material_path"] for e in full_events}
        assert emitted == self._WALL_BINDINGS

        replayed = _server_process_and_replay(srv, events)
        receiver = Usd.Stage.CreateInMemory()
        apply_events(receiver, replayed)

        for stage_label, stage in (("server", srv.stage), ("receiver", receiver)):
            for mesh_path, expected_mat in self._WALL_BINDINGS.items():
                mesh = stage.GetPrimAtPath(mesh_path)
                assert mesh.IsValid(), f"{stage_label}: {mesh_path} missing"
                # The full-purpose rel must exist on the suffixed slot.
                rel = mesh.GetRelationship("material:binding:full")
                assert rel and rel.IsValid() and rel.IsAuthored(), (
                    f"{stage_label}: {mesh_path} missing material:binding:full"
                )
                assert [str(t) for t in rel.GetTargets()] == [expected_mat]
                # And it must resolve via ComputeBoundMaterial("full").
                bound = UsdShade.MaterialBindingAPI(mesh).ComputeBoundMaterial("full")[0]
                assert bound and str(bound.GetPath()) == expected_mat, (
                    f"{stage_label}: ComputeBoundMaterial('full') for {mesh_path} "
                    f"resolved to {bound.GetPath() if bound else None}, "
                    f"expected {expected_mat}"
                )
                # The allPurpose slot must NOT be authored on these meshes.
                allp = mesh.GetRelationship("material:binding")
                assert not (allp and allp.IsAuthored()), (
                    f"{stage_label}: {mesh_path} unexpectedly has allPurpose binding"
                )
