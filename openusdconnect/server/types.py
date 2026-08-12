"""Server-side dataclasses and error types."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class ClientInfo:
    """Metadata for a connected client (emitter or receiver)."""

    role: str
    address: tuple
    client_id: str | None = None
    origin: str | None = None
    department: str | None = None
    connected_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    event_count: int = 0

class VfsWriteRejectedError(RuntimeError):
    """Base class for VFS write fallback rejections."""


class StaleVfsWriteError(VfsWriteRejectedError):
    """Raised when an uploaded VFS snapshot is older than server state."""


class AmbiguousVfsWriteError(VfsWriteRejectedError):
    """Raised when a full-file save looks destructively incomplete."""


class InvalidVfsWriteError(VfsWriteRejectedError):
    """Raised when uploaded VFS bytes are not a readable USD stage."""


class UnsupportedVfsWriteError(VfsWriteRejectedError):
    """Raised when a valid uploaded USD stage cannot be safely translated."""


class ReplayModeConflictError(RuntimeError):
    """Raised when a layer-stack change is incompatible with a flat receiver."""


@dataclass(frozen=True, slots=True)
class TransactionCommit:
    """Server outcome for a durable producer transaction."""

    status: str
    txn_id: int
    records: tuple = ()


class TransactionRejectedError(ValueError):
    """A transaction identity or payload cannot be accepted."""

    def __init__(self, code: str, reason: str, *, expected_txn_id: int = 0):
        super().__init__(reason)
        self.code = code
        self.expected_txn_id = expected_txn_id


@dataclass(frozen=True)
class VfsWriteAnalysis:
    """Summary of a translated full-file VFS save."""

    status: str
    current_epoch: int
    current_seq: int
    uploaded_epoch: int | None = None
    uploaded_seq: int | None = None
    before_prim_count: int = 0
    uploaded_prim_count: int = 0
    created_prims: list[str] = field(default_factory=list)
    removed_prims: list[str] = field(default_factory=list)
    type_changed_prims: list[str] = field(default_factory=list)
    event_counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "current_epoch": self.current_epoch,
            "current_seq": self.current_seq,
            "uploaded_epoch": self.uploaded_epoch,
            "uploaded_seq": self.uploaded_seq,
            "before_prim_count": self.before_prim_count,
            "uploaded_prim_count": self.uploaded_prim_count,
            "created_prims": list(self.created_prims),
            "removed_prims": list(self.removed_prims),
            "type_changed_prims": list(self.type_changed_prims),
            "event_counts": dict(self.event_counts),
            "notes": list(self.notes),
        }
