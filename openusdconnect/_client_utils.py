"""Shared connection helpers for the USD-native client API."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from .client_types import ClientPhase, ClientStatus, SyncUpdate
from .token_client import load_token, save_token


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


class ClientCredential:
    """The one token both roles of a client present, persisted when enabled."""

    def __init__(
        self,
        host: str,
        port: int,
        token: str | None,
        persist: bool,
        on_issued: Callable[[str], None] | None = None,
    ):
        self._host = host
        self._port = port
        self._persist = persist
        self._on_issued = on_issued
        self.token = resolve_client_token(host, port, token, persist)

    def current(self) -> str | None:
        """The token for a connection attempt, loading a stored one if none is known."""
        if self.token is None and self._persist:
            stored = load_token(self._host, self._port)
            # A handshake on another connection can issue a token meanwhile.
            if self.token is None:
                self.token = stored
        return self.token

    def issued(self, token: str) -> None:
        """Adopt a server-issued token, persist it, then notify the host."""
        self.token = token
        if self._persist:
            save_token(self._host, self._port, token)
        if self._on_issued is not None:
            self._on_issued(token)

    def endpoint_kwargs(self) -> dict:
        """Keyword arguments that make an endpoint present and report this token."""
        return {
            "token": self.token,
            "token_provider": self.current,
            "on_token_issued": self.issued,
        }


__all__ = [
    "ClientPhase",
    "ClientStatus",
    "ClientCredential",
    "client_origin",
    "require_app_name",
    "resolve_client_token",
    "SyncUpdate",
    "validate_layered_source",
]
