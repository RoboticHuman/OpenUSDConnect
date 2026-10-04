"""Native client-core API used by the Python integration."""

from ._native_client import (  # type: ignore[import-not-found]
    AcceptResult,
    ClientPhase,
    ProducerPhase,
    ProducerRecoveryDisposition,
    ProducerResult,
    ProducerSession,
    ReceiverInbox,
    ReceiverMessageKind,
    compute_phase,
    rejection_code_name,
    rejection_disposition,
)

__all__ = [
    "AcceptResult",
    "ClientPhase",
    "ProducerPhase",
    "ProducerRecoveryDisposition",
    "ProducerResult",
    "ProducerSession",
    "ReceiverInbox",
    "ReceiverMessageKind",
    "compute_phase",
    "rejection_code_name",
    "rejection_disposition",
]
