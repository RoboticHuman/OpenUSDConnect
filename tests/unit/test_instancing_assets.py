"""Livelink parity for real instancing assets.

Drives the full pipeline minus TCP, with the repo's production assets:
emitter.snapshot_events -> server.process_txn (apply + log, FlatBuffers
records) -> log replay -> receiver.apply_events. Assertions are scoped to
instancing state so unrelated asset features don't make these brittle.

Assets:
  * OpenChessSet/chess_set.usda: two UsdGeomPointInstancers (8 pawns each)
    with quath orientations, invisibleIds, and child prototypes that carry
    a reference plus a shadingVariant selection.
  * SubdivisionSurfaces/Creases_SpinningPyramids.usda: three instanceable
    prims sharing one referenced asset, differentiated only by a variant
    selection (distinct instancing keys, so multiple prototypes).
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    from pxr import Usd, UsdGeom

    PXR_AVAILABLE = True
except ImportError:
    PXR_AVAILABLE = False

pytestmark = pytest.mark.skipif(not PXR_AVAILABLE, reason="pxr not available")

from openusdconnect.codec import message_to_dict
from openusdconnect.emitter import NoticeEmitter
from openusdconnect.event_apply import apply_events
from openusdconnect.server import UsdSyncServer

ASSETS = Path(__file__).parents[2] / "assets" / "full_assets"
CHESS_SET = ASSETS / "OpenChessSet" / "chess_set.usda"
PYRAMIDS = ASSETS / "SubdivisionSurfaces" / "Creases_SpinningPyramids.usda"


@pytest.fixture
def srv(tmp_path):
    s = UsdSyncServer(log_path=str(tmp_path / "assets.db"))
    yield s
    s.store.close()


def _replay_through_server(srv, src_stage):
    emitter = NoticeEmitter(src_stage)
    srv.process_txn(emitter.snapshot_events())
    rows = srv.store.get_all_asc()
    receiver = Usd.Stage.CreateInMemory()
    apply_events(receiver, [message_to_dict(r[1], numpy_arrays=True)["event"] for r in rows])
    return receiver


@pytest.mark.skipif(not CHESS_SET.exists(), reason="chess_set asset not present")
def test_chess_set_point_instancers_parity(srv):
    src = Usd.Stage.Open(str(CHESS_SET))
    receiver = _replay_through_server(srv, src)

    for pi_path in ("/ChessSet/Black/Pawns", "/ChessSet/White/Pawns"):
        pi_src = UsdGeom.PointInstancer(src.GetPrimAtPath(pi_path))
        pi_dst = UsdGeom.PointInstancer(receiver.GetPrimAtPath(pi_path))
        assert pi_dst, f"{pi_path} did not arrive as a PointInstancer"

        assert pi_dst.GetPrototypesRel().GetTargets() == pi_src.GetPrototypesRel().GetTargets()
        assert list(pi_dst.GetProtoIndicesAttr().Get()) == list(pi_src.GetProtoIndicesAttr().Get())
        assert list(pi_dst.GetInvisibleIdsAttr().Get()) == list(pi_src.GetInvisibleIdsAttr().Get())

        # quath source vs orientationsf receiver: half values convert to
        # float32 exactly, so the resolved instance transforms must match.
        default = Usd.TimeCode.Default()
        xf_src = pi_src.ComputeInstanceTransformsAtTime(default, default)
        xf_dst = pi_dst.ComputeInstanceTransformsAtTime(default, default)
        assert len(xf_dst) == len(xf_src) == 8
        for a, b in zip(xf_src, xf_dst):
            for r in range(4):
                for c in range(4):
                    assert abs(a[r][c] - b[r][c]) < 1e-6, f"{pi_path} transform mismatch"

        # invisibleIds-driven mask parity (spec masking semantics).
        assert list(pi_dst.ComputeMaskAtTime(default)) == list(pi_src.ComputeMaskAtTime(default))

        # The child prototype prim composes its reference + shadingVariant.
        proto_path = str(pi_src.GetPrototypesRel().GetTargets()[0])
        proto_src = src.GetPrimAtPath(proto_path)
        proto_dst = receiver.GetPrimAtPath(proto_path)
        assert proto_dst.IsValid()
        vsel_src = proto_src.GetVariantSets().GetAllVariantSelections()
        vsel_dst = proto_dst.GetVariantSets().GetAllVariantSelections()
        assert dict(vsel_dst) == dict(vsel_src)


@pytest.mark.skipif(not PYRAMIDS.exists(), reason="pyramids asset not present")
def test_pyramids_native_instancing_parity(srv):
    src = Usd.Stage.Open(str(PYRAMIDS))
    receiver = _replay_through_server(srv, src)

    pyramid_paths = [
        "/World/Pyramid_NoCreases",
        "/World/Pyramid_EdgeCreases",
        "/World/Pyramid_CornerCreases",
    ]
    for path in pyramid_paths:
        prim_src = src.GetPrimAtPath(path)
        prim_dst = receiver.GetPrimAtPath(path)
        assert prim_dst.IsValid(), f"{path} missing on receiver"
        assert prim_dst.IsInstanceable()
        assert prim_dst.IsInstance(), f"{path} did not compose as an instance"
        assert (
            dict(prim_dst.GetVariantSets().GetAllVariantSelections())
            == dict(prim_src.GetVariantSets().GetAllVariantSelections())
        )
        # Composed children come from the receiver's own prototype, with
        # the same content as the source's.
        names_src = sorted(c.GetName() for c in prim_src.GetChildren())
        names_dst = sorted(c.GetName() for c in prim_dst.GetChildren())
        assert names_dst == names_src
        for child in prim_dst.GetChildren():
            assert child.IsInstanceProxy()

    # Distinct variant selections give distinct instancing keys; the
    # receiver's composition rebuilds the same prototype partition.
    assert len(receiver.GetPrototypes()) == len(src.GetPrototypes())

    # The pyramids animate via a matrix transform op; its samples replicate
    # as decomposed TRS events, so local transforms match at sampled times.
    probe = pyramid_paths[0]
    op_attr = src.GetPrimAtPath(probe).GetAttribute("xformOp:transform:xform1")
    times = op_attr.GetTimeSamples()
    assert times, "asset expected to carry animated transform samples"
    src_xf = UsdGeom.Xformable(src.GetPrimAtPath(probe))
    dst_xf = UsdGeom.Xformable(receiver.GetPrimAtPath(probe))
    for t in (times[0], times[len(times) // 2], times[-1]):
        m_src = src_xf.GetLocalTransformation(Usd.TimeCode(t))
        m_dst = dst_xf.GetLocalTransformation(Usd.TimeCode(t))
        if isinstance(m_src, tuple):
            m_src = m_src[0]
        if isinstance(m_dst, tuple):
            m_dst = m_dst[0]
        for row in range(4):
            for col in range(4):
                assert abs(m_src[row][col] - m_dst[row][col]) < 1e-5, f"t={t}"
