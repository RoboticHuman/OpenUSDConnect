"""Shared connection helpers for the USD-native client API."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from .recovery import RecoveryIncident, TransactionFailure
from .token_client import load_token, save_token


class ClientPhase(StrEnum):
    """High-level lifecycle state shared by every client role."""

    OFFLINE = "offline"
    CONNECTING = "connecting"
    REPLAYING = "replaying"
    READY = "ready"
    RECOVERY_REQUIRED = "recovery_required"
    REJECTED = "rejected"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class ClientStatus:
    """Immutable client state suitable for application and UI polling.

    A directional connection is ``None`` when that role is not present, which
    distinguishes a receive-only or send-only client from a disconnected half
    of a bidirectional client.
    """

    phase: ClientPhase
    connected: bool
    synchronized: bool
    receiver_connected: bool | None = None
    sender_connected: bool | None = None
    prepared_events: int = 0
    pending_events: int = 0
    acknowledged_events: int = 0
    failure: TransactionFailure | None = None
    recovery: RecoveryIncident | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SyncUpdate:
    """Work completed by one bidirectional client update call."""

    applied_events: int
    submitted_events: int
    acknowledged_events: int = 0
    pending_events: int = 0
    recovery: RecoveryIncident | None = None


def validate_layered_source(stage) -> None:
    """Raise ValueError if *stage* is a generated live continuation file.

    Managed-mode clients rebuild collaboration layers from sequence 1; a
    generated live snapshot already contains server state and replaying the
    full logical history over that baseline would leave duplicate, stale
    opinions under the managed layers.
    """
    root = stage.GetRootLayer()
    metadata = (root.customLayerData or {}).get("openusdconnect") if root else None
    if metadata and metadata.get("live"):
        raise ValueError(
            "managed clients require the original base stage so they can "
            "rebuild collaboration layers from sequence 1; generated live "
            "files are continuation baselines"
        )


def require_app_name(app_name: str) -> str:
    value = str(app_name).strip()
    if not value:
        raise ValueError("app_name must not be empty")
    return value


def client_origin(app_name: str, role: str) -> str:
    return f"{app_name}-{uuid.uuid4().hex[:8]}-{role}"


def resolve_client_token(
    host: str,
    port: int,
    token: str | None,
    persist: bool,
) -> str | None:
    if token is not None or not persist:
        return token
    return load_token(host, port)


def client_token_callback(
    host: str,
    port: int,
    persist: bool,
) -> Callable[[str], None] | None:
    if not persist:
        return None
    return lambda token: save_token(host, port, token)


def client_token_handlers(
    host: str,
    port: int,
    persist: bool,
    on_token_issued: Callable[[str], None] | None,
) -> Callable[[str], None] | None:
    """Return a callback that chains *on_token_issued* with disk persistence."""
    persist_cb = client_token_callback(host, port, persist)
    if on_token_issued is None or persist_cb is None:
        return on_token_issued or persist_cb

    def _both(token: str) -> None:
        persist_cb(token)
        on_token_issued(token)

    return _both


__all__ = [
    "ClientPhase",
    "ClientStatus",
    "client_origin",
    "client_token_callback",
    "client_token_handlers",
    "require_app_name",
    "resolve_client_token",
    "SyncUpdate",
    "validate_layered_source",
]
