"""Native client-core API used by the Python integration, and its value conversions."""

import logging
import weakref
from collections.abc import Callable

from ._native_client import (  # type: ignore[import-not-found]
    ClientPhase,
    FlushResult,
    LayerMode,
    LogLevel,
    NotificationQueue,
    PlaybackClaimed,
    PlaybackRejected,
    PlaybackState,
    ProducerConfig,
    ProducerDriver,
    ProducerEndpoint,
    ProducerRecoveryDisposition,
    ProducerResult,
    ProducerStatus,
    ReceiverConfig,
    ReceiverDriver,
    ReceiverEndpoint,
    ReceiverStatus,
    SocketResult,
    StageMetadata,
    TcpSocketFactory,
    TokenIssued,
    compute_phase,
    rejection_code_name,
    rejection_disposition,
)
from .protocol_constants import LayerMode as _LayerMode

NATIVE_LAYER_MODES = {
    _LayerMode.MANAGED: LayerMode.MANAGED,
    _LayerMode.SHARED_STAGE: LayerMode.SHARED_STAGE,
}
LAYER_MODES = {native: mode for mode, native in NATIVE_LAYER_MODES.items()}
LOG_LEVELS = {
    LogLevel.DEBUG: logging.DEBUG,
    LogLevel.INFO: logging.INFO,
    LogLevel.WARNING: logging.WARNING,
    LogLevel.ERROR: logging.ERROR,
}


def stage_metadata_fields(metadata) -> dict:
    """The authored fields of native stage metadata, keyed as in a ``set_stage_metadata`` event."""
    fields = {
        "timeCodesPerSecond": metadata.time_codes_per_second,
        "framesPerSecond": metadata.frames_per_second,
        "startTimeCode": metadata.start_time_code,
        "endTimeCode": metadata.end_time_code,
        "metersPerUnit": metadata.meters_per_unit,
        "upAxis": metadata.up_axis,
    }
    return {key: value for key, value in fields.items() if value is not None}


def rejection_reason(rejection) -> str:
    """Why a native handshake rejection refused the connection."""
    if rejection.authentication:
        return rejection.reason
    return rejection.reason or "connection rejected"


def notification_queue(
    notifications: NotificationQueue | None, **callbacks: Callable | None
) -> tuple[NotificationQueue, bool]:
    """The queue an endpoint pushes to, and whether its wrapper delivers it to *callbacks*.

    The owner of a given *notifications* drains it, so none of *callbacks* may be set.
    """
    if notifications is None:
        return NotificationQueue(), True
    named = [name for name, callback in callbacks.items() if callback is not None]
    if named:
        raise ValueError(f"notifications= cannot be combined with {', '.join(named)}")
    return notifications, False


def driver_callbacks(owner, logger: logging.Logger, *, sink: bool) -> dict:
    """Driver callbacks that hold *owner* weakly, so collecting it stops its driver thread.

    *owner* supplies ``_connection_token()``, ``_token_issued(token)``, and with
    *sink* ``_deliver(notification)``.
    """
    reference = weakref.ref(owner)

    def method(name: str) -> Callable:
        def call(*args):
            alive = reference()
            return None if alive is None else getattr(alive, name)(*args)

        return call

    def log(level, message: str) -> None:
        logger.log(LOG_LEVELS[level], "%s", message)

    return {
        "token_provider": method("_connection_token"),
        "token_issued": method("_token_issued"),
        "notification_sink": method("_deliver") if sink else None,
        "log": log,
    }


__all__ = [
    "LAYER_MODES",
    "LOG_LEVELS",
    "NATIVE_LAYER_MODES",
    "ClientPhase",
    "FlushResult",
    "LayerMode",
    "LogLevel",
    "NotificationQueue",
    "PlaybackClaimed",
    "PlaybackRejected",
    "PlaybackState",
    "ProducerConfig",
    "ProducerDriver",
    "ProducerEndpoint",
    "ProducerRecoveryDisposition",
    "ProducerResult",
    "ProducerStatus",
    "ReceiverConfig",
    "ReceiverDriver",
    "ReceiverEndpoint",
    "ReceiverStatus",
    "SocketResult",
    "StageMetadata",
    "TcpSocketFactory",
    "TokenIssued",
    "compute_phase",
    "driver_callbacks",
    "notification_queue",
    "rejection_code_name",
    "rejection_disposition",
    "rejection_reason",
    "stage_metadata_fields",
]
