"""TCP connection handler and threaded server for the sync protocol."""

from __future__ import annotations

import concurrent.futures
import logging
import queue
import socket
import socketserver
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..codec import (
    HelloRejectionCode,
    PayloadType,
    decode_envelope,
    decode_hello,
    decode_transaction,
    encode_message,
    message_to_dict,
    resolve_payload,
)
from ..framing import (
    IncompleteRead,
    MessageTooLarge,
    frame_batch,
    recv_framed_rfile,
)
from ..protocol import make_replay_complete, make_transaction_result
from ..protocol_constants import (
    MSG_AUTH_REJECTED,
    MSG_HELLO_OK,
    MSG_HELLO_REJECTED,
    MSG_PLAYBACK_CLAIMED,
    MSG_PLAYBACK_REJECTED,
    MSG_PLAYBACK_STATE,
    MSG_RATE_LIMITED,
    MSG_RESYNC,
    PROTOCOL_VERSION,
    LayerMode,
)
from ..transport import send_msg, send_raw
from ._sock_utils import _set_keepalive, _set_send_timeout
from .rate_limit import TokenBucket

if TYPE_CHECKING:
    from .state import UsdSyncServer
from .types import TransactionRejectedError

LOG = logging.getLogger(__name__)

_SEND_TIMEOUT_S = 10.0  # send-only timeout for receiver sockets (seconds)


@dataclass(slots=True)
class _PendingTransactionResult:
    request: object | None
    result: dict | None
    txn_id: int
    event_count: int


def _transaction_rejection(txn_id: int, error: TypeError | ValueError) -> dict:
    """Use the same public rejection for admission and deferred commit failures."""
    if isinstance(error, TransactionRejectedError):
        return make_transaction_result(
            txn_id,
            status="rejected",
            expected_txn_id=error.expected_txn_id,
            rejection_code=error.code,
            reason=str(error),
        )
    return make_transaction_result(
        txn_id,
        status="rejected",
        rejection_code="invalid_transaction",
        reason=str(error),
    )


def _send_transaction_results(
    sock: socket.socket,
    results: list[dict],
    *,
    measure_bytes: bool = False,
) -> int | None:
    """Send ordered results and optionally return their framed byte count.

    The unmeasured single-result path delegates to :func:`send_msg` so it
    serializes exactly once. Unmeasured sends return ``None``; callers that
    enable wire metrics request and receive the actual framed byte count.
    """
    if len(results) == 1 and not measure_bytes:
        send_msg(sock, results[0])
        return None
    payload = frame_batch([encode_message(result) for result in results])
    sock.sendall(payload)
    return len(payload) if measure_bytes else None


class ConnectionHandler(socketserver.StreamRequestHandler):
    """Handles a single client connection (emitter or receiver)."""

    server: ThreadedTCPServer

    def setup(self):
        super().setup()
        self._receiver_replay_reserved = False
        self._receiver_replay_reservation_lock = threading.Lock()

    def release_receiver_replay_reservation(self) -> None:
        """Release an accepted receiver mode exactly once."""
        with self._receiver_replay_reservation_lock:
            if not self._receiver_replay_reserved:
                return
            self._receiver_replay_reserved = False
            layered_replay = self._layered_replay
        self.server.sync_server.release_receiver_replay_mode(layered_replay)

    def finish(self):
        try:
            self.release_receiver_replay_reservation()
        finally:
            super().finish()

    def _reject_hello(self, code: int, reason: str) -> None:
        send_msg(
            self.request,
            {"type": MSG_HELLO_REJECTED, "code": code, "reason": reason},
        )

    def handle(self):
        sync_server = self.server.sync_server

        # Bound the handshake and disable Nagle for small frames. The timeout is
        # cleared after admission because receivers may legitimately sit idle.
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        _set_keepalive(self.request)
        self.request.settimeout(60.0)

        # Per-receiver send lock: serializes replay and broadcast sends on
        # this socket without holding the global clients_lock during I/O.
        self.send_lock = threading.Lock()
        self._rate_bucket = (
            TokenBucket(sync_server.txn_rate, sync_server.txn_burst)
            if sync_server.txn_rate > 0
            else None
        )

        try:
            hello_buf = recv_framed_rfile(self.rfile)
        except (TimeoutError, IncompleteRead, MessageTooLarge):
            return
        if sync_server.wire_metrics is not None:
            sync_server.wire_metrics.record_transport(
                "client_ingress",
                len(hello_buf) + 4,
            )

        try:
            env = decode_envelope(hello_buf)
            if env.PayloadType() != PayloadType.Hello:
                return
            _, hello_table = resolve_payload(env)
            hello = decode_hello(hello_table)
        except Exception as e:
            LOG.warning("Failed to parse hello message: %s", e)
            return

        protocol_version = hello["protocol_version"]
        if protocol_version != PROTOCOL_VERSION:
            LOG.warning(
                "Rejected client protocol version %s from %s; expected %s",
                protocol_version,
                self.client_address,
                PROTOCOL_VERSION,
            )
            return

        role = hello["role"]
        if role not in ("emitter", "receiver"):
            self._reject_hello(
                HelloRejectionCode.Unspecified,
                "role must be 'emitter' or 'receiver'",
            )
            LOG.warning("Rejected unknown role %r from %s", role, self.client_address)
            return
        client_id = hello.get("client_id")
        self._origin = hello.get("origin")
        self._department = hello.get("department")
        self._layered_replay = hello.get("layered_replay", False)
        self._layer_mode = LayerMode(hello.get("layer_mode", LayerMode.MANAGED.value))
        hello_token = hello.get("token")
        self._client_id = client_id
        self._producer_session_id = hello.get("producer_session_id", "")
        self._addr_key = f"{self.client_address[0]}:{self.client_address[1]}"

        if self._layer_mode is not sync_server.layer_mode:
            self._reject_hello(
                HelloRejectionCode.LayerModeMismatch,
                f"server uses {sync_server.layer_mode.value!r} layer mode, "
                f"client requested {self._layer_mode.value!r}",
            )
            LOG.warning(
                "Rejected %s layer mode from %s; server uses %s",
                self._layer_mode.value,
                self.client_address,
                sync_server.layer_mode.value,
            )
            return

        if role == "emitter" and (
            not client_id
            or not self._producer_session_id
            or len(self._producer_session_id) > 128
        ):
            self._reject_hello(
                HelloRejectionCode.Unspecified,
                "emitter hello requires client_id and producer_session_id",
            )
            return

        if self._layer_mode is LayerMode.SHARED_STAGE and (
            self._layered_replay or self._department
        ):
            self._reject_hello(
                HelloRejectionCode.LayerModeMismatch,
                "shared-stage mode does not use managed layered replay "
                "or department routing",
            )
            return

        if role == "receiver" and self._layer_mode is LayerMode.MANAGED:
            accepted, reason = sync_server.reserve_receiver_replay_mode(
                self._layered_replay,
            )
            if not accepted:
                self._reject_hello(HelloRejectionCode.LayeredReplayRequired, reason)
                LOG.warning(
                    "Rejected flat receiver %s from %s: %s",
                    client_id,
                    self.client_address,
                    reason,
                )
                return
            self._receiver_replay_reserved = True

        # TOFU authentication
        accepted, issued_token = sync_server.authenticate(
            client_id,
            hello_token,
            self._department,
        )
        if not accepted:
            send_msg(
                self.request,
                {
                    "type": MSG_AUTH_REJECTED,
                    "reason": "invalid or missing token",
                },
            )
            LOG.warning("Rejected %s from %s", client_id, self.client_address)
            return

        # Send hello_ok with token (issued on first connect, None on reconnect)
        hello_ok = {"type": MSG_HELLO_OK, "server_instance": sync_server.server_instance}
        if role == "receiver":
            hello_ok["replay_identity"] = True
        if role == "emitter":
            hello_ok["committed_through"] = sync_server.producer_committed_through(
                client_id,
                self._producer_session_id,
            )
        if sync_server.layer_mode is LayerMode.SHARED_STAGE:
            hello_ok["layer_mode"] = LayerMode.SHARED_STAGE.value
        if issued_token:
            hello_ok["token"] = issued_token
        if (
            role == "receiver"
            and sync_server.layer_mode is LayerMode.MANAGED
            and self._layered_replay
        ):
            hello_ok["layered_replay"] = True
        stage_meta = sync_server.get_stage_metadata_payload()
        if stage_meta:
            hello_ok["stage_metadata"] = stage_meta
        if role != "receiver":
            send_msg(self.request, hello_ok)

        LOG.info(
            "Client connected: role=%s origin=%s dept=%s from %s",
            role,
            self._origin,
            self._department,
            self.client_address,
        )
        sync_server.register_client(
            self.client_address,
            role,
            client_id,
            origin=self._origin,
            department=self._department,
        )

        try:
            # Select a department layer only when department ordering is enabled.
            # Without departments, all clients share edit_layer (last-write-wins).
            self._client_layer = None
            if (
                role == "emitter"
                and client_id
                and sync_server.layer_mode is LayerMode.MANAGED
                and sync_server.department_priority
            ):
                self._client_layer = sync_server.get_or_create_client_layer(
                    client_id,
                    department=self._department,
                )

            if role == "receiver":
                try:
                    with sync_server.receiver_replay_window(
                        self,
                        hello.get("sync_from") or 1,
                        replay_server_instance=hello.get("replay_server_instance"),
                        replay_epoch=hello.get("replay_epoch"),
                    ) as replay:
                        hello_ok["replay_epoch"] = replay.epoch
                        send_msg(self.request, hello_ok)
                        if replay.resync_reason is not None:
                            send_msg(
                                self.request,
                                {"type": MSG_RESYNC, "reason": replay.resync_reason},
                            )

                        snapshot = sync_server.get_playback_state()
                        send_msg(
                            self.request,
                            {"type": MSG_PLAYBACK_STATE, **snapshot},
                        )
                        if replay.layer_stack_state is not None:
                            send_raw(self.request, replay.layer_stack_state)
                        sync_server.replay_records(self, replay.records)
                        send_msg(
                            self.request,
                            make_replay_complete(replay.head_seq, replay.epoch),
                        )
                    # Do not retain the captured history for this socket's lifetime.
                    del replay
                except (OSError, TimeoutError):
                    LOG.info(
                        "Receiver disconnected during replay: %s",
                        self.client_address,
                    )
                    return

            # Send-only timeout so the broadcast thread isn't blocked
            # indefinitely by one slow receiver. Uses SO_SNDTIMEO (platform-
            # aware) so recv stays blocking because settimeout() would cause
            # spurious TimeoutError in _read_loop.
            if role == "receiver":
                _set_send_timeout(self.request, _SEND_TIMEOUT_S)
            self.request.settimeout(None)
            if role == "emitter":
                self._run_pipelined_read_loop(sync_server)
            else:
                self._read_loop(sync_server, None)
        finally:
            with sync_server.clients_lock:
                sync_server.receivers.discard(self)
            sync_server.unregister_client(self.client_address)
            # Release the playback-leader role and broadcast a vacant-leader
            # PlaybackState so other clients can claim it.
            if sync_server.release_playback(self._client_id or ""):
                self._broadcast_playback_state(sync_server)
            LOG.info("Client disconnected: %s", self.client_address)

    def _broadcast_playback_state(self, sync_server: UsdSyncServer):
        state = sync_server.get_playback_state()
        sync_server.broadcast_message({"type": MSG_PLAYBACK_STATE, **state})

    def _run_pipelined_read_loop(self, sync_server: UsdSyncServer) -> None:
        """Read producer transactions while an ordered worker delivers results."""
        results: queue.Queue[_PendingTransactionResult | None] = queue.Queue(
            # One producer can fill one durable group, but cannot monopolize
            # the coordinator with an arbitrarily deep per-connection backlog.
            maxsize=max(1, sync_server.txn_batch_size),
        )
        worker = threading.Thread(
            target=self._transaction_result_loop,
            args=(sync_server, results),
            name=f"ouc-results-{self._client_id}",
            daemon=True,
        )
        worker.start()
        try:
            self._read_loop(sync_server, results)
        finally:
            # FIFO placement after the last submitted request makes the result
            # worker drain every commit/barrier even after an abrupt peer close.
            results.put(None)
            worker.join()

    def _read_loop(
        self,
        sync_server: UsdSyncServer,
        results: queue.Queue[_PendingTransactionResult | None] | None,
    ):
        while True:
            try:
                buf = recv_framed_rfile(self.rfile)
            except ConnectionResetError:
                break
            except (IncompleteRead, MessageTooLarge):
                break
            except TimeoutError:
                continue

            if sync_server.wire_metrics is not None:
                sync_server.wire_metrics.record_transport(
                    "client_ingress",
                    len(buf) + 4,
                )

            env = decode_envelope(buf)
            pt = env.PayloadType()

            if pt == PayloadType.Quit:
                break

            if pt == PayloadType.Compact:
                LOG.info("Compact requested by %s", self.client_address)
                sync_server.compact_log()
                continue

            if pt == PayloadType.ClaimPlayback:
                msg = message_to_dict(buf)
                self._handle_claim_playback(sync_server, msg)
                continue

            if pt == PayloadType.PlaybackControl:
                msg = message_to_dict(buf)
                self._handle_playback_control(sync_server, msg)
                continue

            if pt != PayloadType.Txn:
                continue

            # Decode txn events to dicts for apply_txn numpy arrays
            # for geometry attrs to avoid per-element Python iteration.
            _, txn_fb = resolve_payload(env)
            events, txn_id, txn_layer_key = decode_transaction(
                txn_fb,
                numpy_arrays=True,
            )
            if not events:
                continue

            if self._rate_bucket is not None:
                wait = self._rate_bucket.try_consume()
                if wait > 0:
                    self._send_control_response(
                        {
                            "type": MSG_RATE_LIMITED,
                            "retry_after": round(wait, 3),
                        }
                    )
                    continue

            if results is None:
                LOG.warning("Receiver connection attempted to submit a transaction")
                break

            result = None
            request = None
            try:
                request = sync_server.submit_idempotent_txn(
                    events,
                    session_id=self._producer_session_id,
                    txn_id=txn_id,
                    client_id=self._client_id,
                    origin=self._origin,
                    client_addr=self._addr_key,
                    layer=self._client_layer,
                    layer_key=txn_layer_key,
                )
            except (TypeError, ValueError) as exc:
                result = _transaction_rejection(txn_id, exc)
            results.put(
                _PendingTransactionResult(
                    request=request,
                    result=result,
                    txn_id=txn_id,
                    event_count=len(events),
                )
            )

    def _send_control_response(self, message: dict) -> None:
        """Serialize a read-loop response with asynchronous txn results."""
        with self.send_lock:
            send_msg(self.request, message)

    def _transaction_result_loop(
        self,
        sync_server: UsdSyncServer,
        results: queue.Queue[_PendingTransactionResult | None],
    ) -> None:
        delivery_failed = False
        while True:
            pending = results.get()
            if pending is None:
                return
            outgoing: list[dict] = []
            reached_end = False
            processed = 0
            max_batch = max(1, sync_server.txn_batch_size)
            while pending is not None and processed < max_batch:
                processed += 1
                result = pending.result
                if result is None:
                    try:
                        commit = sync_server.wait_for_transaction(pending.request)
                        result = make_transaction_result(
                            commit.txn_id,
                            status="acknowledged",
                            checkpoint=commit.checkpoint,
                        )
                    except (TypeError, ValueError) as exc:
                        result = _transaction_rejection(pending.txn_id, exc)
                    except Exception:
                        LOG.exception(
                            "Transaction %s/%d failed without a protocol result",
                            self._producer_session_id,
                            pending.txn_id,
                        )
                        delivery_failed = True
                        self._shutdown_request_socket()
                        result = None

                if result is not None:
                    if (
                        result.get("status") == "acknowledged"
                        and outgoing
                        and outgoing[-1].get("status") == "acknowledged"
                    ):
                        outgoing[-1] = result
                    else:
                        outgoing.append(result)
                    with sync_server.clients_lock:
                        info = sync_server.clients.get(self._addr_key)
                        if info:
                            info.last_activity = time.time()
                            if result.get("status") == "acknowledged":
                                info.event_count += pending.event_count

                if processed >= max_batch:
                    break
                try:
                    pending = results.get_nowait()
                except queue.Empty:
                    break
                if pending is None:
                    reached_end = True
                    break

            if outgoing and not delivery_failed:
                try:
                    with self.send_lock:
                        if sync_server.wire_metrics is None:
                            _send_transaction_results(self.request, outgoing)
                            sent_bytes = None
                        else:
                            sent_bytes = _send_transaction_results(
                                self.request,
                                outgoing,
                                measure_bytes=True,
                            )
                    if sent_bytes is not None:
                        sync_server.wire_metrics.record_transport(
                            "producer_result_egress",
                            sent_bytes,
                            count=len(outgoing),
                        )
                except OSError:
                    # Commit may be durable; reconnect replays the exact bytes.
                    delivery_failed = True
                    self._shutdown_request_socket()
            if reached_end:
                return

    def _shutdown_request_socket(self) -> None:
        try:
            self.request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _handle_claim_playback(self, sync_server: UsdSyncServer, msg: dict):
        initial_time = msg.get("time")
        granted, current_leader = sync_server.claim_playback(
            self._client_id or "",
            initial_time=initial_time,
        )
        if not granted:
            self._send_control_response(
                {
                    "type": MSG_PLAYBACK_REJECTED,
                    "reason": "another client holds the playback leader role",
                    "current_leader_client_id": current_leader,
                },
            )
            return
        self._send_control_response(
            {"type": MSG_PLAYBACK_CLAIMED, "leader_client_id": current_leader},
        )
        self._broadcast_playback_state(sync_server)

    def _handle_playback_control(self, sync_server: UsdSyncServer, msg: dict):
        ok, payload, current_leader = sync_server.apply_playback_control(
            self._client_id or "",
            msg.get("action", ""),
            float(msg.get("time", 0.0)),
            float(msg.get("rate", 1.0)),
        )
        if not ok:
            self._send_control_response(
                {
                    "type": MSG_PLAYBACK_REJECTED,
                    "reason": str(payload),
                    "current_leader_client_id": current_leader,
                },
            )
            return
        sync_server.broadcast_message({"type": MSG_PLAYBACK_STATE, **payload})

class ThreadedTCPServer(socketserver.TCPServer):
    allow_reuse_address = True
    request_queue_size = 128
    MAX_WORKERS = 256

    def __init__(
        self,
        server_address,
        handler_class,
        sync_server: UsdSyncServer,
        max_workers: int | None = None,
    ):
        self.sync_server = sync_server
        self._requests: set[socket.socket] = set()
        self._requests_lock = threading.Lock()
        self._closing = False
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers or self.MAX_WORKERS,
            thread_name_prefix="conn",
        )
        try:
            super().__init__(server_address, handler_class)
        except BaseException:
            self._pool.shutdown(wait=True)
            raise

    def process_request(self, request, client_address):
        with self._requests_lock:
            if self._closing:
                self.close_request(request)
                return
            self._requests.add(request)
            try:
                self._pool.submit(self._handle_request, request, client_address)
            except BaseException:
                self._requests.discard(request)
                raise

    def _handle_request(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
            with self._requests_lock:
                self._requests.discard(request)

    def server_close(self):
        super().server_close()
        with self._requests_lock:
            self._closing = True
            for request in self._requests:
                try:
                    request.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                # makefile() retains socket references; detach closes the OS
                # handle now, waking Windows readers waiting in select().
                descriptor = request.detach()
                if descriptor != -1:
                    socket.close(descriptor)
        self._pool.shutdown(wait=True)
