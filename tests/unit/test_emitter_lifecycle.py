"""Pending notices, diff baselines, and retry-owned batches have distinct lifetimes."""

import pytest
from pxr import Gf, Usd, UsdGeom

from openusdconnect.emitter import NoticeEmitter
from openusdconnect.protocol_constants import K_SET_XFORM_TRS


@pytest.fixture
def emitter_scene():
    stage = Usd.Stage.CreateInMemory()
    prim = stage.DefinePrim("/Thing", "Xform")
    translate = UsdGeom.Xformable(prim).AddTranslateOp()
    emitter = NoticeEmitter(stage)
    try:
        yield stage, translate, emitter
    finally:
        emitter.cleanup()


def test_clear_all_preserves_diff_baseline_and_live_dirty_set(emitter_scene):
    _, translate, emitter = emitter_scene
    translate.Set(Gf.Vec3d(1, 0, 0))
    emitter.build_events_for_dirty()
    dirty = emitter.dirty
    listener = emitter.listener

    translate.Set(Gf.Vec3d(2, 0, 0))
    emitter.clear_all()

    assert emitter.dirty is dirty
    assert not dirty
    assert emitter.listener is listener
    translate.Set(Gf.Vec3d(1, 0, 0))
    assert emitter.build_events_for_dirty() == []


def test_clear_all_retains_prepared_batch_but_discards_later_notices(emitter_scene):
    _, translate, emitter = emitter_scene
    translate.Set(Gf.Vec3d(1, 0, 0))
    prepared = emitter.prepare_events_for_send()
    assert prepared

    translate.Set(Gf.Vec3d(2, 0, 0))
    emitter.clear_all()

    assert emitter.prepare_events_for_send() is prepared
    emitter.mark_prepared_events_sent(prepared)
    assert emitter.prepare_events_for_send() == []


def test_failed_build_retains_authored_target_and_restores_active_target(
    emitter_scene, monkeypatch,
):
    stage, translate, emitter = emitter_scene
    translate.Set(Gf.Vec3d(1, 0, 0))
    emitter.build_events_for_dirty()
    with emitter.suppressed(), Usd.EditContext(stage, stage.GetSessionLayer()):
        translate.Set(Gf.Vec3d(9, 0, 0))

    translate.Set(Gf.Vec3d(3, 0, 0))
    stage.SetEditTarget(Usd.EditTarget(stage.GetSessionLayer()))
    active_target = stage.GetEditTarget()

    def fail_build(eps_trs):
        raise RuntimeError("injected build failure")

    with monkeypatch.context() as patch:
        patch.setattr(emitter, "_build_events_for_dirty_current_target", fail_build)
        with pytest.raises(RuntimeError, match="injected build failure"):
            emitter.build_events_for_dirty()

    assert stage.GetEditTarget() == active_target
    events = emitter.build_events_for_dirty()
    transform = next(event for event in events if event["k"] == K_SET_XFORM_TRS)
    assert transform["t"] == pytest.approx([3, 0, 0])
    assert stage.GetEditTarget() == active_target
    assert translate.Get() == Gf.Vec3d(9, 0, 0)
