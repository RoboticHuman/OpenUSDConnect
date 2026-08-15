"""Shared layer-key routing base: mapping, state guards, lifecycle hooks."""

from __future__ import annotations

import pytest
from pxr import Sdf, Usd

from openusdconnect.layer_key_router import LayerKeyRouter
from openusdconnect.logical_layers import LogicalLayerRouter


class _RecordingRouter(LayerKeyRouter):
    def __init__(self, stage=None):
        super().__init__(stage)
        self.applied = []
        self.installs = []
        self.detaches = []

    def _apply_state_inner(self, state):
        self.applied.append(state)
        for item in state.get("layers", ()):
            layer = Sdf.Layer.CreateAnonymous(item["label"])
            self._bind_key(item["layer_key"], layer)

    def _install_layers(self, stage):
        self.installs.append(stage)

    def _detach_layers(self, stage):
        self.detaches.append(stage)


def _state(generation, revision, *keys):
    return {
        "generation": generation,
        "revision": revision,
        "layers": [
            {"layer_key": key, "label": f"layer-{key}"}
            for key in keys
        ],
    }


def test_key_layer_mapping_roundtrip_and_conflicts():
    router = _RecordingRouter()
    assert router.apply_state(_state("g1", 1, "k1"))
    layer = router.layer_for("k1")
    assert layer is not None
    assert router.key_for(layer) == "k1"
    assert router.layer_for("nope") is None
    assert router.key_for(Sdf.Layer.CreateAnonymous("unmapped")) is None

    with pytest.raises(ValueError, match="more than one local layer"):
        router._bind_key("k1", Sdf.Layer.CreateAnonymous("other"))
    other = Sdf.Layer.CreateAnonymous("other")
    router._bind_key("k2", other)
    with pytest.raises(ValueError, match="maps to both"):
        router._bind_key("k3", other)


def test_apply_state_generation_revision_monotonicity():
    router = _RecordingRouter()
    assert router.apply_state(_state("g1", 1, "k1"))
    assert not router.apply_state(_state("g1", 1, "k2"))  # duplicate
    assert not router.apply_state(_state("g1", 0, "k3"))  # stale
    assert router.apply_state(_state("g1", 2, "k4"))      # fresh
    assert router.generation == "g1"
    assert router.revision == 2
    assert router.apply_state(_state("g2", 0, "k5"))      # new generation resets
    assert router.generation == "g2"
    assert router.revision == 0
    assert len(router.applied) == 3

    with pytest.raises(ValueError, match="non-negative"):
        router.apply_state(_state("g2", -1))


def test_failed_state_application_does_not_advance_state():
    class _FailingOnceRouter(_RecordingRouter):
        def __init__(self):
            super().__init__()
            self._should_fail = True

        def _apply_state_inner(self, state):
            if self._should_fail:
                self._should_fail = False
                raise ValueError("boom")
            super()._apply_state_inner(state)

    router = _FailingOnceRouter()
    with pytest.raises(ValueError, match="boom"):
        router.apply_state(_state("g1", 1))
    assert not router.ready
    assert router.generation == ""
    assert router.revision == -1
    assert router.apply_state(_state("g1", 1))  # retry still applies


def test_ready_flag_lifecycle():
    router = _RecordingRouter()
    assert not router.ready
    assert router.apply_state(_state("g1", 1))
    assert router.ready


def test_bind_close_invoke_subclass_hooks():
    router = _RecordingRouter()
    first = Usd.Stage.CreateInMemory()
    second = Usd.Stage.CreateInMemory()

    router.bind(first)
    assert router.stage is first
    assert router.installs == [first]
    router.bind(first)  # same stage is a no-op
    assert router.installs == [first]
    router.bind(second)
    assert router.detaches == [first]
    assert router.installs == [first, second]
    router.close()
    assert router.detaches == [first, second]
    assert router.stage is None

    with pytest.raises(TypeError, match="Usd.Stage"):
        router.bind(None)


def test_logical_layer_router_keeps_strict_layer_for():
    router = LogicalLayerRouter()
    with pytest.raises(RuntimeError, match="state has not been received"):
        router.layer_for("k1")
    router.apply_state(_state("g1", 1, "k1"))
    assert router.layer_for("k1") is not None
    with pytest.raises(ValueError, match="unknown logical layer"):
        router.layer_for("nope")
