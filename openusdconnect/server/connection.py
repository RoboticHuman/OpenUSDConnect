"""TCP connection handler and threaded server for the sync protocol."""

from __future__ import annotations

import concurrent.futures
import logging
import socket
import socketserver
import threading
import time
from typing import TYPE_CHECKING

from ..codec import (
    PayloadType,
    decode_envelope,
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
from ..protocol_constants import (
    K_LOAD_PAYLOAD,
    MSG_AUTH_REJECTED,
    MSG_EVENT,
    MSG_HELLO_OK,
    MSG_PLAYBACK_CLAIMED,
    MSG_PLAYBACK_REJECTED,
    MSG_PLAYBACK_STATE,
    MSG_PROPOSAL_CREATED,
    MSG_RATE_LIMITED,
    MSG_RESYNC,
)
from ..transport import send_msg
from ._sock_utils import _set_keepalive, _set_send_timeout
from .rate_limit import TokenBucket

if TYPE_CHECKING:
    from .state import UsdSyncServer

LOG = logging.getLogger(__name__)

_SEND_TIMEOUT_S = 10.0  # send-only timeout for receiver sockets (seconds)


class ConnectionHandler(socketserver.StreamRequestHandler):
    """Handles a single client connection (emitter or receiver)."""

    server: ThreadedTCPServer

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

        try:
            env = decode_envelope(hello_buf)
            if env.PayloadType() != PayloadType.Hello:
                return
            _, hello_fb = resolve_payload(env)
        except Exception as e:
            LOG.warning("Failed to parse hello message: %s", e)
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
        token_raw = hello_fb.Token()
        hello_token = token_raw.decode("utf-8") if isinstance(token_raw, bytes) else token_raw
        self._client_id = client_id
        self._addr_key = f"{self.client_address[0]}:{self.client_address[1]}"

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
        if issued_token:
            hello_ok["token"] = issued_token
        stage_meta = sync_server.get_stage_metadata_payload()
        if stage_meta:
            hello_ok["stage_metadata"] = stage_meta
        send_msg(self.request, hello_ok)

        # Receivers get a PlaybackState snapshot post-handshake so fresh
        # clients see the current leader + timecode without waiting for
        # the next control. Emitters skip it — they don't read past
        # hello_ok and would just fill their socket buffer.
        if role == "receiver":
            snapshot = sync_server.get_playback_state()
            send_msg(self.request, {"type": MSG_PLAYBACK_STATE, **snapshot})

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

        # Create per-client layer only when department ordering is enabled.
        # Without departments, all clients share edit_layer (last-write-wins).
        self._client_layer = None
        if role == "emitter" and client_id and sync_server.department_priority:
            self._client_layer = sync_server.get_or_create_client_layer(
                client_id,
                department=self._department,
            )

        if role == "receiver":
            sync_from = hello_fb.SyncFrom() or 1

            # If sync_from is beyond the current log (e.g., after compaction
            # reset seq numbers), send resync so the receiver resets its
            # sequence counter, then replay the full log.
            max_seq = sync_server.store.get_max_seq()
            if sync_from > max_seq > 0:
                send_msg(self.request, {"type": MSG_RESYNC, "reason": "seq_overflow"})
                sync_from = 1

            # Acquire send_lock first, then add to broadcast set and replay.
            # The send_lock prevents broadcasts from reaching this receiver
            # until replay is complete (preserving event ordering). The
            # broadcast thread never holds both locks simultaneously (it
            # snapshots under clients_lock, releases it, then acquires
            # send_lock per target), so no deadlock is possible.
            with self.send_lock:
                with sync_server.clients_lock:
                    sync_server.receivers.add(self)
                sync_server.replay_from(self, sync_from)

        # Send-only timeout so the broadcast thread isn't blocked
        # indefinitely by one slow receiver. Uses SO_SNDTIMEO (platform-
        # aware) so recv stays blocking — settimeout() can't be used
        # because it would cause spurious TimeoutError in _read_loop.
        if role == "receiver":
            _set_send_timeout(self.request, _SEND_TIMEOUT_S)
        self.request.settimeout(None)
        try:
            self._read_loop(sync_server)
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

    def _read_loop(self, sync_server: UsdSyncServer):
        while True:
            try:
                buf = recv_framed_rfile(self.rfile)
            except ConnectionResetError:
                break
            except (IncompleteRead, MessageTooLarge):
                break
            except TimeoutError:
                continue

            env = decode_envelope(buf)
            pt = env.PayloadType()

            if pt == PayloadType.Quit:
                break

            if pt == PayloadType.Compact:
                LOG.info("Compact requested by %s", self.client_address)
                sync_server.compact_log()
                continue

            if pt == PayloadType.CreateProposal:
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

            proposal_id = txn_fb.ProposalId()
            if proposal_id:
                sync_server.apply_proposal_txn(proposal_id.decode(), events)
                continue

            if self._rate_bucket is not None:
                wait = self._rate_bucket.try_consume()
                if wait > 0:
                    send_msg(
                        self.request,
                        {
                            "type": MSG_RATE_LIMITED,
                            "retry_after": round(wait, 3),
                        },
                    )
                    continue

            sync_server.txn_barrier.acquire_shared()
            try:
                records, changed_set = sync_server.process_txn(
                    events,
                    client_id=self._client_id,
                    origin=self._origin,
                    client_addr=self._addr_key,
                    layer=self._client_layer,
                )

                # Broadcast changed events; send corrections for overridden ones.
                changed_records = []
                changed_bins = []
                for i, (rec, rec_bin) in enumerate(records):
                    if i in changed_set:
                        changed_records.append(rec)
                        changed_bins.append(rec_bin)
                    else:
                        correction = sync_server.build_correction(events[i])
                        if correction:
                            correction_rec = {
                                "type": MSG_EVENT,
                                "seq": sync_server.assign_seq(),
                                "event": correction,
                            }
                            sync_server.send_to_origin(
                                correction_rec,
                                self._origin,
                            )
                if changed_records:
                    payload = frame_batch(changed_bins)
                    sync_server.broadcast_bytes(
                        payload,
                        changed_records,
                        exclude_origin=self._origin,
                    )

                # After load_payload, re-broadcast latest child state so
                # receivers re-apply authoritative TRS after re-import. Kept
                # inside the shared txn barrier so its extra seq/append_log/
                # broadcast can't interleave with a concurrent compaction/purge.
                for ev in events:
                    if ev.get("k") == K_LOAD_PAYLOAD:
                        sync_server.replay_children_after_load(ev["prim"])
            finally:
                sync_server.txn_barrier.release_shared()

            # Update client activity tracking.
            with sync_server.clients_lock:
                info = sync_server.clients.get(self._addr_key)
                if info:
                    info.last_activity = time.time()
                    info.event_count += len(events)

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
