"""Typed transaction rejection policy shared by client integrations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .codec import TransactionRejectionCode


class RejectionDisposition(StrEnum):
    """What an application should do after a transaction is rejected."""

    SESSION_FATAL = "session_fatal"
    RECOVERABLE_CONFLICT = "recoverable_conflict"
    INVALID_OPERATION = "invalid_operation"


class RecoveryKind(StrEnum):
    """Machine-readable category for a client recovery incident."""

    TRANSACTION_REJECTED = "transaction_rejected"


_CODE_NAMES = {
    TransactionRejectionCode.InvalidIdentity: "invalid_identity",
    TransactionRejectionCode.UnexpectedId: "unexpected_id",
    TransactionRejectionCode.StaleLayerGraph: "stale_layer_graph",
    TransactionRejectionCode.InvalidTransaction: "invalid_transaction",
}

_CODE_DISPOSITIONS = {
    TransactionRejectionCode.StaleLayerGraph: RejectionDisposition.RECOVERABLE_CONFLICT,
    TransactionRejectionCode.InvalidTransaction: RejectionDisposition.INVALID_OPERATION,
}


@dataclass(frozen=True, slots=True)
class TransactionFailure:
    """A server rejection with enough policy information for useful UX."""

    txn_id: int
    code: int
    reason: str
    expected_txn_id: int = 0

    @property
    def code_name(self) -> str:
        return _CODE_NAMES.get(self.code, f"unknown_{self.code}")

    @property
    def disposition(self) -> RejectionDisposition:
        # Unknown rejection codes fail closed for forward compatibility.
        return _CODE_DISPOSITIONS.get(self.code, RejectionDisposition.SESSION_FATAL)

    def __str__(self) -> str:
        expected = f", expected transaction {self.expected_txn_id}" if self.expected_txn_id else ""
        return (
            f"transaction {self.txn_id} rejected ({self.code_name}{expected}): "
            f"{self.reason or 'no reason supplied'}"
        )


@dataclass(frozen=True, slots=True)
class QuarantinedTransaction:
    """Exact encoded transaction retained after a deterministic rejection."""

    txn_id: int
    payload: bytes
    event_count: int
    layer_key: str = ""


@dataclass(frozen=True, slots=True)
class RecoveryArtifact:
    """Transport evidence needed to inspect, export, or rebuild local intent."""

    producer_session_id: str
    failure: TransactionFailure
    transactions: tuple[QuarantinedTransaction, ...]

    @property
    def event_count(self) -> int:
        return sum(transaction.event_count for transaction in self.transactions)

    @property
    def layer_keys(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                transaction.layer_key
                for transaction in self.transactions
                if transaction.layer_key
            )
        )


@dataclass(frozen=True, slots=True)
class RecoveryIncident:
    """Immutable, UI-safe summary of work quarantined by a rejection."""

    incident_id: str
    kind: RecoveryKind
    failure: TransactionFailure
    producer_session_id: str
    transaction_ids: tuple[int, ...]
    event_count: int
    layer_keys: tuple[str, ...]

    @property
    def transaction_count(self) -> int:
        return len(self.transaction_ids)


def make_recovery_incident(artifact: RecoveryArtifact) -> RecoveryIncident:
    """Build a factual public summary for a rejected producer outbox."""

    return RecoveryIncident(
        incident_id=f"{artifact.producer_session_id}:{artifact.failure.txn_id}",
        kind=RecoveryKind.TRANSACTION_REJECTED,
        failure=artifact.failure,
        producer_session_id=artifact.producer_session_id,
        transaction_ids=tuple(
            transaction.txn_id for transaction in artifact.transactions
        ),
        event_count=artifact.event_count,
        layer_keys=artifact.layer_keys,
    )


__all__ = [
    "QuarantinedTransaction",
    "RecoveryArtifact",
    "RecoveryIncident",
    "RecoveryKind",
    "RejectionDisposition",
    "TransactionFailure",
    "make_recovery_incident",
]
