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


__all__ = ["RejectionDisposition", "TransactionFailure"]
