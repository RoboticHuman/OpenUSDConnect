"""Named replay checkpoints used by durable writes and mirror confirmation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TransactionCheckpoint:
    """A durable position in one server replay sequence domain."""

    epoch: int
    head_seq: int


@dataclass(frozen=True, slots=True)
class MirrorCheckpoint:
    """A transaction checkpoint qualified by the server process that issued it."""

    server_instance: str
    epoch: int
    head_seq: int
