"""OpenUSDConnect — DCC-agnostic real-time USD sync framework.

Core library for replicating USD stage edits over a networked event protocol.
"""

from ._client_utils import ClientPhase, ClientStatus, SyncUpdate
from .adapters import (
    DCCAdapter,
    MockAdapter,
    UsdStageAdapter,
)
from .codec import (
    DecodeResult,
    HelloRejectionCode,
    ReceivedEvent,
    decode_envelope,
    decode_messages,
    encode_message,
    message_to_dict,
    resolve_event,
    resolve_payload,
)
from .emitter import NoticeEmitter
from .event_apply import apply_event, apply_events, atomic_apply
from .events import Event
from .layer_key_router import LayerKeyRouter
from .managed_client import ManagedClient, ManagedRecoveryResult
from .plugin_environment import (
    PluginEnvironmentError,
    PluginEnvironmentResult,
    prepare_usd_plugin_environment,
)
from .protocol import make_hello, make_quit, make_txn
from .protocol_constants import LayerMode
from .receiver import ReceiverThread
from .recovery import (
    QuarantinedTransaction,
    RecoveryArtifact,
    RecoveryError,
    RecoveryIncident,
    RecoveryKind,
    RejectionDisposition,
    TransactionFailure,
)
from .sender import EventSender, TransactionRejectedError
from .server import ServerConfig, UsdSyncServer, VfsConfig, run_server
from .shared_stage_client import (
    SharedRecoveryAssessment,
    SharedRecoveryLayer,
    SharedStageClient,
)
from .usd_client import UsdPublisher, UsdReceiver

__version__ = "0.1.0"

__all__ = [
    "DCCAdapter",
    "ClientPhase",
    "ClientStatus",
    "DecodeResult",
    "Event",
    "EventSender",
    "HelloRejectionCode",
    "LayerKeyRouter",
    "LayerMode",
    "ManagedClient",
    "ManagedRecoveryResult",
    "MockAdapter",
    "NoticeEmitter",
    "PluginEnvironmentError",
    "PluginEnvironmentResult",
    "ReceiverThread",
    "QuarantinedTransaction",
    "RecoveryArtifact",
    "RecoveryError",
    "RecoveryIncident",
    "RecoveryKind",
    "RejectionDisposition",
    "ReceivedEvent",
    "ServerConfig",
    "SharedRecoveryAssessment",
    "SharedRecoveryLayer",
    "SharedStageClient",
    "SyncUpdate",
    "TransactionRejectedError",
    "TransactionFailure",
    "UsdPublisher",
    "UsdReceiver",
    "UsdStageAdapter",
    "UsdSyncServer",
    "VfsConfig",
    "apply_event",
    "apply_events",
    "atomic_apply",
    "decode_envelope",
    "decode_messages",
    "encode_message",
    "make_hello",
    "make_quit",
    "make_txn",
    "message_to_dict",
    "prepare_usd_plugin_environment",
    "resolve_event",
    "resolve_payload",
    "run_server",
    "__version__",
]
