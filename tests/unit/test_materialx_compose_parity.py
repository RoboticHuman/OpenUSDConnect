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

        The structural pass in apply_events sorts by EVENT_KIND_ORDER, so
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
            srv, emitter.build_events_for_dirty(include_matrices=False)
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
