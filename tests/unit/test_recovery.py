"""Rejection codes map to stable names and recovery dispositions."""

import pytest

from openusdconnect import RejectionDisposition, TransactionFailure


@pytest.mark.parametrize(
    ("code", "name", "disposition"),
    [
        (0, "unknown_0", RejectionDisposition.SESSION_FATAL),
        (1, "invalid_identity", RejectionDisposition.SESSION_FATAL),
        (2, "unexpected_id", RejectionDisposition.SESSION_FATAL),
        (3, "stale_layer_graph", RejectionDisposition.RECOVERABLE_CONFLICT),
        (4, "invalid_transaction", RejectionDisposition.INVALID_OPERATION),
        (9, "unknown_9", RejectionDisposition.SESSION_FATAL),
    ],
)
def test_rejection_code_name_and_disposition(code, name, disposition):
    failure = TransactionFailure(txn_id=1, code=code, reason="")

    assert failure.code_name == name
    assert failure.disposition is disposition
