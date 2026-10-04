"""Receive wire messages on a native thread for a stage-owning consumer to drain."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable

from . import _client_backend
from .codec import HelloRejectionCode
from .defaults import DEFAULT_HOST, DEFAULT_SYNC_PORT
from .protocol_constants import (
    MSG_PLAYBACK_CLAIMED,
    MSG_PLAYBACK_REJECTED,
    MSG_PLAYBACK_STATE,
    LayerMode,
)

LOG = logging.getLogger(__name__)

_RECONNECT_BASE_DELAY = 1.0
_RECONNECT_MAX_DELAY = 30.0
_SOCKET_TIMEOUT = 30.0
_MAX_QUEUE_DEPTH = 50_000

_NATIVE_LAYER_MODES = {
    LayerMode.MANAGED: _client_backend.LayerMode.MANAGED,
    LayerMode.SHARED_STAGE: _client_backend.LayerMode.SHARED_STAGE,
}
_LAYER_MODES = {native: mode for mode, native in _NATIVE_LAYER_MODES.items()}
_LOG_LEVELS = {
    _client_backend.LogLevel.DEBUG: logging.DEBUG,
    _client_backend.LogLevel.INFO: logging.INFO,
    _client_backend.LogLevel.WARNING: logging.WARNING,
    _client_backend.LogLevel.ERROR: logging.ERROR,
}


def _log(level, message: str) -> None:
    LOG.log(_LOG_LEVELS[level], "%s", message)


def _stage_metadata(metadata) -> dict:
    """The authored fields, keyed as in a ``set_stage_metadata`` event."""
    fields = {
        "timeCodesPerSecond": metadata.time_codes_per_second,
        "framesPerSecond": metadata.frames_per_second,
        "startTimeCode": metadata.start_time_code,
        "endTimeCode": metadata.end_time_code,
        "metersPerUnit": metadata.meters_per_unit,
        "upAxis": metadata.up_axis,
    }
    return {key: value for key, value in fields.items() if value is not None}


def _transport_error(failure) -> OSError:
    if failure.result == _client_backend.SocketResult.TIMEOUT:
        return TimeoutError(failure.description)
    return OSError(failure.system_error, failure.description)


class ReceiverThread:
    """Receive wire messages for a stage-owning consumer to drain.

    A native thread runs the connection and queues scene messages as raw
    FlatBuffers for the consumer thread to decode and apply. Handshake and
    control messages, including their callbacks, are handled on that thread.
    Overflow closes the connection and resumes by replay after the queue
    drains or the drain wait expires.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_SYNC_PORT,
        sync_from: int = 1,
        reconnect: bool = True,
        max_queue: int = _MAX_QUEUE_DEPTH,
        socket_timeout: float = _SOCKET_TIMEOUT,
        client_id: str | None = None,
        reconnect_base_delay: float = _RECONNECT_BASE_DELAY,
        reconnect_max_delay: float = _RECONNECT_MAX_DELAY,
        origin: str | None = None,
        department: str | None = None,
        token: str | None = None,
        token_provider: Callable[[], str | None] | None = None,
        on_token_issued: Callable[[str], None] | None = None,
        on_stage_metadata: Callable[[dict], None] | None = None,
        on_playback_state: Callable[[dict], None] | None = None,
        on_playback_claimed: Callable[[dict], None] | None = None,
        on_playback_rejected: Callable[[dict], None] | None = None,
        layered_replay: bool = True,
        layer_mode: LayerMode | str = LayerMode.MANAGED,
    ):
        self._host = host
        self._port = port
        self._sync_from = sync_from
        self._max_queue = max_queue
        self._socket_timeout = socket_timeout
        self._client_id = client_id
        self._origin = origin
        self._department = department
        self._layered_replay = bool(layered_replay)
        self._layer_mode = LayerMode(layer_mode)
        self._reconnect = bool(reconnect)
        self.token = token
        self._token_provider = token_provider
        self._token_error: Exception | None = None
        self._on_token_issued = on_token_issued
        self._on_stage_metadata = on_stage_metadata
        self._on_playback_state = on_playback_state
        self._on_playback_claimed = on_playback_claimed
        self._on_playback_rejected = on_playback_rejected

        config = _client_backend.ReceiverConfig()
        config.host = host
        config.port = port
        config.client_id = client_id or ""
        config.origin = origin or ""
        config.department = department or ""
        config.layered_replay = self._layered_replay
        config.layer_mode = _NATIVE_LAYER_MODES[self._layer_mode]
        config.sync_from = sync_from
        config.max_queue = max_queue
        config.socket_timeout = socket_timeout
        config.reconnect = self._reconnect
        config.reconnect_base_delay = reconnect_base_delay
        config.reconnect_max_delay = reconnect_max_delay
        self._notifications = _client_backend.NotificationQueue()
        self._endpoint = _client_backend.ReceiverEndpoint(config, self._notifications)
        self._driver = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def sync_from(self) -> int:
        """The first sequence requested; the consumer already holds every earlier one."""
        return self._sync_from

    @property
    def max_queue(self) -> int:
        return self._max_queue

    @property
    def socket_timeout(self) -> float:
        return self._socket_timeout

    @property
    def client_id(self) -> str | None:
        return self._client_id

    @property
    def origin(self) -> str | None:
        return self._origin

    @property
    def department(self) -> str | None:
        return self._department

    @property
    def layered_replay(self) -> bool:
        return self._layered_replay

    @property
    def layer_mode(self) -> LayerMode:
        return self._layer_mode

    @property
    def reconnect(self) -> bool:
        """Whether a lost connection is retried; a change applies when it ends."""
        return self._reconnect

    @reconnect.setter
    def reconnect(self, enabled: bool) -> None:
        self._reconnect = bool(enabled)
        self._endpoint.set_reconnect(self._reconnect)

    @property
    def connected(self) -> bool:
        return self._endpoint.status().connected

    @property
    def synchronized(self) -> bool:
        """Whether replay through the server's advertised head was applied."""
        return self._endpoint.status().synchronized

    @property
    def last_seq(self) -> int:
        return self._endpoint.status().last_sequence

    @property
    def stopped(self) -> bool:
        """Whether the connection thread ran and exited, so it will not reconnect."""
        return self._driver is not None and self._driver.stopped

    @property
    def queued_message_count(self) -> int:
        """Number of received messages waiting for the owning thread to drain."""
        return self._endpoint.status().queued_frames

    @property
    def generation(self) -> int:
        """Connection generation; read it before draining for :meth:`mark_applied_through`."""
        return self._endpoint.generation

    @property
    def layered_replay_active(self) -> bool:
        return self._endpoint.status().layered_replay_active

    @property
    def layer_mode_active(self) -> LayerMode:
        return _LAYER_MODES[self._endpoint.status().layer_mode_active]

    @property
    def auth_rejected(self) -> bool:
        rejection = self._endpoint.status().rejection
        return rejection is not None and rejection.authentication

    @property
    def hello_rejected(self) -> bool:
        rejection = self._endpoint.status().rejection
        return rejection is not None and not rejection.authentication

    @property
    def rejection_code(self) -> int:
        rejection = self._endpoint.status().rejection
        return HelloRejectionCode.Unspecified if rejection is None else rejection.code

    @property
    def rejection_reason(self) -> str:
        rejection = self._endpoint.status().rejection
        return "" if rejection is None else rejection.reason

    @property
    def server_instance(self) -> str:
        """The server whose replay the consumer applied; empty when unproven."""
        return self._endpoint.status().server_instance

    @property
    def replay_epoch(self) -> int:
        return self._endpoint.status().replay_epoch

    @property
    def replay_head_seq(self) -> int:
        return self._endpoint.status().replay_head_sequence

    @property
    def stage_metadata(self) -> dict:
        """The latest stage metadata the server authored, keyed as on the wire."""
        return _stage_metadata(self._endpoint.status().metadata)

    @property
    def connection_error(self) -> Exception | None:
        """Why the latest connection attempt failed, or ``None``."""
        failure = None if self._driver is None else self._driver.last_failure
        return self._token_error if failure is None else _transport_error(failure)

    def mark_applied_through(self, generation: int, sequence: int) -> bool:
        """Advance the applied cursor for frames drained after reading ``generation``.

        A live sequence gap replays from this cursor, so report a batch only
        after its whole apply pipeline succeeded.
        """
        return self._endpoint.mark_applied_through(generation, sequence)

    def reset_applied_progress(self) -> None:
        """Restart the applied cursor once the consumer has applied a queued resync."""
        self._endpoint.reset_applied_progress()

    def freeze_marker(self) -> int:
        """Return a marker covering every message queued now; see :meth:`drained_through`."""
        return self._endpoint.freeze_marker()

    def drained_through(self, marker: int) -> bool:
        """Whether every message queued when ``marker`` was taken has been drained."""
        return self._endpoint.drained_through(marker)

    def wait_connected(self, timeout: float | None = None) -> bool:
        """Wait for the current handshake result, not for replay completion."""
        if self._driver is None:
            return self.connected
        return self._driver.wait_connected(timeout)

    def wait_synchronized(self, timeout: float | None = None) -> bool:
        """Wait for replay to be applied by the stage-owning consumer thread."""
        if self._driver is None:
            return self.synchronized
        return self._driver.wait_synchronized(timeout)

    def mark_replay_applied(self) -> bool:
        """Publish READY after a successful drain applied the replay prefix."""
        applied = self._endpoint.mark_replay_applied()
        if applied:
            self._wake()
        return applied

    def request_replay_from(self, seq_start: int) -> None:
        """Reconnect and request replay beginning at ``seq_start``.

        Consumers call this after a queued frame fails to decode. Frames queued
        after the failed frame are discarded, and the current connection is
        replaced so it cannot queue more stale frames.
        """
        if not self._endpoint.request_replay_from(int(seq_start)):
            raise ValueError("replay sequence must be at least 1")
        self._wake()

    def drain_queue(self, max_messages: int | None = None) -> deque:
        """Drain queued wire messages, optionally limiting work for this tick."""
        if max_messages is not None and (
            isinstance(max_messages, bool) or not isinstance(max_messages, int) or max_messages < 1
        ):
            raise ValueError("max_messages must be a positive integer or None")
        return deque(self._endpoint.drain_frames(max_messages))

    def start(self) -> None:
        """Start connecting on a native thread; a receiver starts once."""
        if self._driver is not None:
            raise RuntimeError("a receiver can only be started once")
        self._driver = _client_backend.ReceiverDriver(
            self._endpoint,
            self._notifications,
            _client_backend.TcpSocketFactory(),
            token_provider=self._connection_token,
            notification_sink=self._deliver,
            log=_log,
        )
        if not self._driver.start():
            raise RuntimeError("could not start the receiver thread")

    def stop(self) -> None:
        """Request a clean shutdown without waiting; :meth:`join` waits."""
        if self._driver is None:
            self._endpoint.stop()
        else:
            self._driver.stop()

    def join(self, timeout: float | None = None) -> None:
        """Wait until the connection thread exits; returns at once before :meth:`start`."""
        if self._driver is not None:
            self._driver.join(timeout)

    def is_alive(self) -> bool:
        return self._driver is not None and self._driver.running

    @property
    def ident(self) -> int | None:
        """Identifier of the connection thread, or ``None`` before :meth:`start`."""
        return None if self._driver is None else self._driver.ident

    def _wake(self) -> None:
        if self._driver is not None:
            self._driver.wake()

    def _connection_token(self) -> str | None:
        """The token for the next handshake; ``None`` abandons the attempt."""
        self._token_error = None
        if self._token_provider is not None:
            try:
                self.token = self._token_provider()
            except Exception as exc:
                LOG.exception("ReceiverThread: token provider failed")
                self._token_error = exc
                return None
        return self.token or ""

    def _deliver(self, notification) -> None:
        """Run the callback for a notification on the connection thread."""
        if isinstance(notification, _client_backend.TokenIssued):
            self.token = notification.token
            self._notify(self._on_token_issued, notification.token, "on_token_issued")
        elif isinstance(notification, _client_backend.StageMetadata):
            self._notify(
                self._on_stage_metadata, _stage_metadata(notification), "on_stage_metadata"
            )
        elif isinstance(notification, _client_backend.PlaybackState):
            message = {
                "type": MSG_PLAYBACK_STATE,
                "time": notification.time,
                "playing": notification.playing,
                "rate": notification.rate,
                "leader_client_id": notification.leader_client_id,
            }
            self._notify(self._on_playback_state, message, "playback")
        elif isinstance(notification, _client_backend.PlaybackClaimed):
            message = {
                "type": MSG_PLAYBACK_CLAIMED,
                "leader_client_id": notification.leader_client_id,
            }
            self._notify(self._on_playback_claimed, message, "playback")
        elif isinstance(notification, _client_backend.PlaybackRejected):
            message = {
                "type": MSG_PLAYBACK_REJECTED,
                "reason": notification.reason,
                "current_leader_client_id": notification.current_leader_client_id,
            }
            self._notify(self._on_playback_rejected, message, "playback")

    @staticmethod
    def _notify(callback: Callable | None, value, name: str) -> None:
        if callback is None:
            return
        try:
            callback(value)
        except Exception:
            LOG.exception("ReceiverThread: %s callback failed", name)
