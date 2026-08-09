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
    encode_message,
    event_to_dict,
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
    MSG_PROPOSAL_CREATED,
    MSG_RATE_LIMITED,
    MSG_RESYNC,
    PROTOCOL_VERSION,
    LayerMode,
)
from ..transport import send_msg
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


def _send_transaction_results(
    sock: socket.socket,
    results: list[dict],
    *,
    measure: bool = False,
) -> int:
    """Send ordered results with one syscall when multiple are ready."""
    if len(results) == 1 and not measure:
        send_msg(sock, results[0])
        return 0
    payload = frame_batch([encode_message(result) for result in results])
    sock.sendall(payload)
    return len(payload)


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

    def handle(self):
        sync_server = self.server.sync_server

        # Socket hardening: disable Nagle (small JSON messages benefit from
        # immediate sends), enable aggressive keepalive (silent-disconnect
        # detection capped at ~60 s — see _set_keepalive), and set a
        # handshake timeout so misbehaving clients don't block handler threads.
        # The timeout is cleared before _read_loop since receivers legitimately
        # sit idle (only consuming broadcasts); keepalive surfaces dead peers
        # as a socket error on the next recv.
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

        # Read hello (length-prefixed FlatBuffers)
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
            _, hello_fb = resolve_payload(env)
        except Exception as e:
            LOG.warning("Failed to parse hello message: %s", e)
            return

        protocol_version = hello_fb.ProtocolVersion()
        if protocol_version != PROTOCOL_VERSION:
            LOG.warning(
                "Rejected client protocol version %s from %s; expected %s",
                protocol_version,
                self.client_address,
                PROTOCOL_VERSION,
            )
            return

        role_raw = hello_fb.Role()
        role = role_raw.decode("utf-8") if isinstance(role_raw, bytes) else role_raw
        client_id_raw = hello_fb.ClientId()
        client_id = (
            client_id_raw.decode("utf-8") if isinstance(client_id_raw, bytes) else client_id_raw
        )
        origin_raw = hello_fb.Origin()
        self._origin = origin_raw.decode("utf-8") if isinstance(origin_raw, bytes) else origin_raw
        dept_raw = hello_fb.Department()
        self._department = dept_raw.decode("utf-8") if isinstance(dept_raw, bytes) else dept_raw
        self._layered_replay = bool(hello_fb.LayeredReplay())
        self._layer_mode = (
            LayerMode.SHARED_STAGE if hello_fb.LayerMode() else LayerMode.MANAGED
        )
        token_raw = hello_fb.Token()
        hello_token = token_raw.decode("utf-8") if isinstance(token_raw, bytes) else token_raw
        self._client_id = client_id
        producer_session_raw = hello_fb.ProducerSessionId()
        self._producer_session_id = (
            producer_session_raw.decode("utf-8")
            if isinstance(producer_session_raw, bytes)
            else producer_session_raw
        ) or ""
        self._addr_key = f"{self.client_address[0]}:{self.client_address[1]}"

        if self._layer_mode is not sync_server.layer_mode:
            send_msg(
                self.request,
                {
                    "type": MSG_HELLO_REJECTED,
                    "code": HelloRejectionCode.LayerModeMismatch,
                    "reason": (
                        f"server uses {sync_server.layer_mode.value!r} layer mode, "
                        f"client requested {self._layer_mode.value!r}"
                    ),
                },
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
            send_msg(
                self.request,
                {
                    "type": MSG_HELLO_REJECTED,
                    "code": HelloRejectionCode.Unspecified,
                    "reason": "emitter hello requires client_id and producer_session_id",
                },
            )
            return

        if self._layer_mode is LayerMode.SHARED_STAGE and (
            self._layered_replay or self._department
        ):
            send_msg(
                self.request,
                {
                    "type": MSG_HELLO_REJECTED,
                    "code": HelloRejectionCode.LayerModeMismatch,
                    "reason": (
                        "shared-stage mode does not use managed layered replay "
                        "or department routing"
                    ),
                },
            )
            return

        if role == "receiver" and self._layer_mode is LayerMode.MANAGED:
            accepted, reason = sync_server.reserve_receiver_replay_mode(
                self._layered_replay,
            )
            if not accepted:
                send_msg(
                    self.request,
                    {
                        "type": MSG_HELLO_REJECTED,
                        "code": HelloRejectionCode.LayeredReplayRequired,
                        "reason": reason,
                    },
                )
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
        hello_ok = {"type": MSG_HELLO_OK}
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
            # Create per-client layer only when department ordering is enabled.
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
                sync_from = hello_fb.SyncFrom() or 1
                try:
                    with sync_server.receiver_replay_window(self) as replay_watermark:
                        replay_end, replay_epoch = replay_watermark
                        # A sequence beyond the captured tail indicates a stale
                        # pre-compaction cursor. Tail + 1 remains a valid live join.
                        if sync_from > replay_end + 1:
                            send_msg(
                                self.request,
                                {"type": MSG_RESYNC, "reason": "seq_overflow"},
                            )
                            sync_from = 1

                        snapshot = sync_server.get_playback_state()
                        send_msg(
                            self.request,
                            {"type": MSG_PLAYBACK_STATE, **snapshot},
                        )
                        if (
                            sync_server.layer_mode is LayerMode.MANAGED
                            and self._layered_replay
                        ):
                            send_msg(
                                self.request,
                                sync_server.get_layer_stack_state(),
                            )
                        sync_server.replay_from(
                            self,
                            sync_from,
                            seq_end=replay_end,
                        )
                        send_msg(
                            self.request,
                            make_replay_complete(replay_end, replay_epoch),
                        )
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

            if pt == PayloadType.CreateProposal:
                if sync_server.layer_mode is LayerMode.SHARED_STAGE:
                    LOG.warning("Shared-stage client requested a department proposal")
                    break
                msg = message_to_dict(buf)
                self._handle_create_proposal(sync_server, msg)
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

            # Decode txn events to dicts for apply_txn — numpy arrays
            # for geometry attrs to avoid per-element Python iteration.
            _, txn_fb = resolve_payload(env)
            events = [
                event_to_dict(txn_fb.Events(i), numpy_arrays=True)
                for i in range(txn_fb.EventsLength())
            ]
            if not events:
                continue

            txn_id = int(txn_fb.TxnId())

            proposal_id = txn_fb.ProposalId()
            if proposal_id:
                if sync_server.layer_mode is LayerMode.SHARED_STAGE:
                    LOG.warning("Shared-stage client targeted a department proposal")
                    break
                sync_server.apply_proposal_txn(proposal_id.decode(), events)
                continue

            if self._rate_bucket is not None:
                wait = self._rate_bucket.try_consume()
                if wait > 0:
                    with self.send_lock:
                        send_msg(
                            self.request,
                            {
                                "type": MSG_RATE_LIMITED,
                                "retry_after": round(wait, 3),
                            },
                        )
                    continue

            if results is None:
                LOG.warning("Receiver connection attempted to submit a transaction")
                break

            result = None
            request = None
            try:
                txn_layer_key = txn_fb.LayerKey()
                if isinstance(txn_layer_key, bytes):
                    txn_layer_key = txn_layer_key.decode("utf-8")
                request = sync_server.submit_idempotent_txn(
                    events,
                    session_id=self._producer_session_id,
                    txn_id=txn_id,
                    client_id=self._client_id,
                    origin=self._origin,
                    client_addr=self._addr_key,
                    layer=self._client_layer,
                    layer_key=txn_layer_key or "",
                )
            except TransactionRejectedError as exc:
                result = make_transaction_result(
                    txn_id,
                    status="rejected",
                    expected_txn_id=exc.expected_txn_id,
                    rejection_code=exc.code,
                    reason=str(exc),
                )
            except (TypeError, ValueError) as exc:
                result = make_transaction_result(
                    txn_id,
                    status="rejected",
                    rejection_code="invalid_transaction",
                    reason=str(exc),
                )
            results.put(
                _PendingTransactionResult(
                    request=request,
                    result=result,
                    txn_id=txn_id,
                    event_count=len(events),
                )
            )

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
                        )
                    except TransactionRejectedError as exc:
                        result = make_transaction_result(
                            pending.txn_id,
                            status="rejected",
                            expected_txn_id=exc.expected_txn_id,
                            rejection_code=exc.code,
                            reason=str(exc),
                        )
                    except (TypeError, ValueError) as exc:
                        result = make_transaction_result(
                            pending.txn_id,
                            status="rejected",
                            rejection_code="invalid_transaction",
                            reason=str(exc),
                        )
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
                            sent_bytes = 0
                        else:
                            sent_bytes = _send_transaction_results(
                                self.request,
                                outgoing,
                                measure=True,
                            )
                    if sent_bytes:
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
            send_msg(
                self.request,
                {
                    "type": MSG_PLAYBACK_REJECTED,
                    "reason": "another client holds the playback leader role",
                    "current_leader_client_id": current_leader,
                },
            )
            return
        send_msg(
            self.request,
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
            send_msg(
                self.request,
                {
                    "type": MSG_PLAYBACK_REJECTED,
                    "reason": str(payload),
                    "current_leader_client_id": current_leader,
                },
            )
            return
        sync_server.broadcast_message({"type": MSG_PLAYBACK_STATE, **payload})

    def _handle_create_proposal(self, sync_server: UsdSyncServer, msg: dict):
        """Handle a create_proposal message from an emitter."""
        target = msg.get("target_department", "")
        desc = msg.get("description", "")
        if not target:
            return
        pid = sync_server.create_proposal(
            self._client_id or "",
            target,
            desc,
        )
        send_msg(
            self.request,
            {
                "type": MSG_PROPOSAL_CREATED,
                "proposal_id": pid,
            },
        )


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
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers or self.MAX_WORKERS,
            thread_name_prefix="conn",
        )
        super().__init__(server_address, handler_class)

    def process_request(self, request, client_address):
        self._pool.submit(self._handle_request, request, client_address)

    def _handle_request(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)

    def server_close(self):
        super().server_close()
        self._pool.shutdown(wait=False)
