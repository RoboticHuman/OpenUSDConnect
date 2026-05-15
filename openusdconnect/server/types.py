"""Server-side dataclasses: connected-client metadata and edit proposals."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from pxr import Sdf


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


@dataclass
class Proposal:
    """Metadata for a cross-department edit proposal."""

    proposal_id: str
    from_client: str
    from_department: str | None
    target_department: str
    description: str
    layer: Sdf.Layer
    status: str = "pending"  # pending, approved, rejected
    created_at: float = field(default_factory=time.time)
    events: list = field(default_factory=list)  # accumulated events for log persistence
