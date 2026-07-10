"""Tests for EventDispatcher callbacks."""

from openusdconnect.adapters import MockAdapter
from openusdconnect.dispatcher import EventDispatcher
from openusdconnect.protocol_constants import K_ENSURE_PRIM, K_SET_VISIBILITY


class _NullReceiver:
    def drain_queue(self):
        return []


def test_on_applied_receives_applied_prim_paths():
    captured = []
    dispatcher = EventDispatcher(
        receiver=_NullReceiver(),
        adapter=MockAdapter(),
        on_applied=captured.append,
    )

    dispatcher._apply(
        [
            {"k": K_ENSURE_PRIM, "prim": "/World/A", "typeName": "Xform"},
            {"k": K_SET_VISIBILITY, "prim": "/World/B", "visible": True},
        ]
    )

    assert captured == [["/World/A", "/World/B"]]


def test_on_applied_optional():
    """A dispatcher without on_applied applies cleanly (no callback)."""
    dispatcher = EventDispatcher(receiver=_NullReceiver(), adapter=MockAdapter())
    assert dispatcher._apply([{"k": K_ENSURE_PRIM, "prim": "/World/A", "typeName": "Xform"}]) == 1
