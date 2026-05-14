"""OpenUSDConnect — DCC-agnostic real-time USD sync framework.

Core library for replicating USD stage edits over a networked event protocol.
"""

from .adapters import DCCAdapter, MockAdapter, UsdStageAdapter
from .codec import (
    decode_envelope,
    encode_message,
    message_to_dict,
    resolve_event,
    resolve_payload,
)
from .emitter import NoticeEmitter
from .event_apply import apply_event, apply_events, atomic_apply
from .events import Event
from .protocol import make_hello, make_quit, make_txn
from .receiver import ReceiverThread
from .server import UsdSyncServer, run_server

__version__ = "0.1.0"

__all__ = [
    "DCCAdapter",
    "Event",
    "MockAdapter",
    "NoticeEmitter",
    "ReceiverThread",
    "UsdStageAdapter",
    "UsdSyncServer",
    "apply_event",
    "apply_events",
    "atomic_apply",
    "decode_envelope",
    "encode_message",
    "make_hello",
    "make_quit",
    "make_txn",
    "message_to_dict",
    "resolve_event",
    "resolve_payload",
    "run_server",
    "__version__",
]
