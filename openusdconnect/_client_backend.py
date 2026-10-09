"""Native client-core API used by the Python integration, and its value conversions."""

import logging
import weakref

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
    ReceiverConfig,
    ReceiverDriver,
    ReceiverEndpoint,
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


def driver_callbacks(owner, logger: logging.Logger) -> dict:
    """Driver callbacks that hold *owner* weakly, so collecting it stops its driver thread.

    *owner* supplies ``_connection_token()`` and ``_deliver(notification)``.
    """
    reference = weakref.ref(owner)

    def token() -> str | None:
        alive = reference()
        return None if alive is None else alive._connection_token()

    def deliver(notification) -> None:
        alive = reference()
        if alive is not None:
            alive._deliver(notification)

    def log(level, message: str) -> None:
        logger.log(LOG_LEVELS[level], "%s", message)

    return {"token_provider": token, "notification_sink": deliver, "log": log}


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
    "ReceiverConfig",
    "ReceiverDriver",
    "ReceiverEndpoint",
    "SocketResult",
    "StageMetadata",
    "TcpSocketFactory",
    "TokenIssued",
    "compute_phase",
    "driver_callbacks",
    "rejection_code_name",
    "rejection_disposition",
    "stage_metadata_fields",
]
